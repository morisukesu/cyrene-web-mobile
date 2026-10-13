# -*- coding: utf-8 -*-
"""cyrene_net —— 网络工具层（`cyrene_web` 拆出来的第七块）。

它原来是什么
------------
三个联网工具的**通道与解析**那一层：

  通道          `_net_open` 统一出口（超时、UA、体积上限、截断标记），
                `NetError` 是它向上抛的唯一异常
  解码          `_pick_charset` / `_decode_body` 认 charset（含 GBK 这类
                非 UTF-8 中文站点），返回 (text, 备注)
  HTML → 文本   `_HTMLToMarkdown`（HTMLParser 子类）+ `_html_to_markdown`，
                解析失败退化成「去标签 + 还原实体」
  下载辅助      `_clean_filename`（洗文件名）/ `_guess_ext_from_ctype`（猜扩展名）
                / `_download_dir`（FS_ROOT/downloads，经 guard_path）
  两个工具      `fetch_url`（抓网页转 Markdown）/ `download_file`（落盘到 downloads）

它依赖宿主什么
--------------
由宿主经 `bind_deps()` 注入：

  · `outcome_success` / `outcome_failure`   工具结果常量
  · `int_arg` / `agent_cfg`                 参数归一、超时读配置
  · `guard_path` / `path_guard_error`       路径守卫（来自 cyrene_fs）
  · `rel` / `fs_root` / `human_size`        回显相对路径、体积显示

⚠ `guard_path` / `path_guard_error` / `rel` / `fs_root` / `human_size` 缺了会静默走偏
  （下载路径不受守卫、回执显示不对），`deps_ready()` 把它们列为必需项。

⚠ 同时它还是**内核 `net` 服务的实现本体**：宿主把 `_net_open` / `_decode_body` /
  `NetError` / `NET_UA` / `_net_timeout` 垫进内核，search / vision 那些模块用的是
  服务，不是这份代码 —— 所以这里搬走了，它们照旧。

红线同前：**绝不 import cyrene_web**，方向只有「宿主 → 内核 → 子系统」。
"""

import html as _html_mod
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser as _HTMLParser
from pathlib import Path
from urllib.parse import urlparse


__all__ = [
    "NET_UA", "NET_FETCH_MAX_BYTES", "NET_DOWNLOAD_MAX_BYTES",
    "NET_DEFAULT_RESULTS", "NET_MAX_RESULTS", "NET_SEARCH_BACKENDS",
    "DOWNLOAD_DIRNAME",
    "NetError", "_net_timeout", "_pick_charset", "_decode_body", "_net_open",
    "_HTMLToMarkdown", "_html_to_markdown",
    "_clean_filename", "_guess_ext_from_ctype", "_download_dir",
    "_h_fetch_url", "_h_download_file",
    "bind_deps", "deps_ready", "DEPS", "PLUGIN",
]


def _def_settings():
    """配置还没注入时的空配置。"""
    return {}


def _def_int_arg(v, lo=0, hi=0, fb=0):
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return fb
    if lo and n < lo:
        n = lo
    if hi and n > hi:
        n = hi
    return n


def _def_agent_cfg(key, fb=None):
    return fb


class _Deps:
    """宿主注入的资源袋。"""

    __slots__ = ("settings", "outcome_success", "outcome_failure",
                 "int_arg", "agent_cfg",
                 "guard_path", "path_guard_error", "rel", "fs_root", "human_size")

    def __init__(self):
        self.settings = _def_settings
        self.outcome_success = "success"
        self.outcome_failure = "failure"
        self.int_arg = _def_int_arg
        self.agent_cfg = _def_agent_cfg
        # 这五个缺了会静默走偏（下载不受守卫 / 回执显示不对），不给兜底
        self.guard_path = None
        self.path_guard_error = None
        self.rel = None
        self.fs_root = None
        self.human_size = None

    def missing(self):
        need = ("guard_path", "path_guard_error", "rel", "fs_root", "human_size")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()
_FRESH_DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入。未知的键直接报错，不静默吞掉。"""
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_net.bind_deps 不认识这些参数：%s"
                         % ", ".join(sorted(unknown)))
    for k, v in kw.items():
        setattr(DEPS, k, v)
    return DEPS


def deps_ready():
    """宿主自检用：返回还缺哪些注入项（空列表 = 齐了）。"""
    return DEPS.missing()


def _reset_deps():
    for k in _Deps.__slots__:
        setattr(DEPS, k, getattr(_FRESH_DEPS, k))


def _plugin_setup(ctx):
    """内核在 config / tools / fs 三个服务齐了之后调这里。"""
    cfg = ctx.require("config")
    tools = ctx.require("tools")
    host = ctx.require("fs")

    bind_deps(
        settings=cfg,
        outcome_success=tools.outcome_success,
        outcome_failure=tools.outcome_failure,
        int_arg=tools.int_arg,
        agent_cfg=tools.agent_cfg,
        guard_path=host.guard_path,
        path_guard_error=host.path_guard_error,
        rel=host.rel,
        fs_root=host.root,
        human_size=host.human_size,
    )
    ctx.effect(_reset_deps, desc="net:deps")
    ctx.log("已接到内核（依赖 config / tools / fs）")


PLUGIN = {
    "id": "net",
    "requires": ["config", "tools", "fs"],
    "setup": _plugin_setup,
}


# ===========================================================================
# 以下正文由 _make_net_module.py 从 cyrene_web.py 逐字抽出
# ===========================================================================


# ========== 网络工具实现层（阶段 2b）==========
# 与文件工具同属 handler 型：进程内直接跑 Python，不 fork 子进程，因此
# 不受 termux-api 广播熔断器牵连、无孤儿进程。但网络调用有文件工具没有的
# 两个风险，必须在这里自己兜住：
#   ① 挂死。handler 在 run_agent_loop 里**同步**执行，没有 run_tool 给命令行
#      工具套的 stepTimeout 子进程超时。所以 urllib 的 timeout 是防挂死的唯一
#      屏障，每个网络请求都必须显式带（见 _net_timeout / _net_open）。
#   ② SSRF / 本地文件泄露。urllib.request.urlopen 认 file://、ftp:// 等协议，
#      `urlopen("file:///etc/passwd")` 会真的读出本地文件 —— 那 guard_path 就
#      白做了。所以 _net_open 只放行 http/https，其余协议一律拒。
#
# 三个工具都是**只读网络**（fetch/search 不落副作用，download 只往 downloads
# 目录写新文件），但 risk 统一标 "network"：让前端能按类过滤，也让 loop 的
# halted 判定把它们排除在「幂等 safe」之外（网络失败返回 FAILURE 而非 UNKNOWN，
# 因为读操作可安全重试；只有超时才可能是 UNKNOWN）。
NET_UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
NET_FETCH_MAX_BYTES = 8 * 1024 * 1024      # fetch_url 单次抓取上限 8 MiB，防 OOM
NET_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024  # download_file 上限 64 MiB（对齐桌面端）
NET_DEFAULT_RESULTS = 6                     # web_search 默认返回条数
NET_MAX_RESULTS = 15                        # web_search 上限
DOWNLOAD_DIRNAME = "downloads"              # 相对 FS_ROOT 的下载目录名

# 搜索引擎降级链。探测（_probe_search.py，2026-10-07）四家全 200：
# bing/cn.bing 结果结构最干净（真实 URL + snippet），ddg-html snippet 命中质量
# 最好，baidu 给的是 /link?url= 跳转链（URL 不干净）故不入首选。任一后端
# 抓取或解析出 0 条就自动降级到下一个，全挂才报错。
NET_SEARCH_BACKENDS = ("bing", "cn.bing", "ddg")


class NetError(Exception):
    """网络请求失败。消息直接回给模型，所以要说清是哪一步、什么原因。"""


def _net_timeout():
    """网络请求超时（秒）。取 agent stepTimeout，夹到 [5,60]。

    handler 型工具在 loop 里同步跑、没有子进程级超时兜底，urllib 的 timeout
    是防挂死的唯一屏障，必须有。夹上限是因为 stepTimeout 可到 120s，而整条
    loop 的 totalTimeout 默认才 180s —— 一个网络请求不该独吞掉大半预算。
    """
    try:
        base = int(DEPS.agent_cfg("stepTimeout") or 30)
    except (TypeError, ValueError):
        base = 30
    return max(5, min(60, base))


def _pick_charset(content_type, head):
    """从 Content-Type 头或 HTML meta 里猜字符集，猜不出返回 None。"""
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        return m.group(1).strip("\"' ")
    m = re.search(rb"charset=[\"']?([\w\-]+)", head[:4096], re.I)
    if m:
        try:
            return m.group(1).decode("ascii", "ignore")
        except Exception:
            return None
    return None


def _decode_body(body, content_type):
    """bytes → (text, note)。按 header/meta 的 charset 优先，退 utf-8，再退 replace。"""
    cs = _pick_charset(content_type, body)
    if cs:
        try:
            return body.decode(cs), ""
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return body.decode("utf-8"), ""
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace"), "（含非 UTF-8 字节，已用替代字符显示）"


def _net_open(url, max_bytes, timeout=None):
    """打开一个 http(s) URL，返回 (status, headers_dict, body_bytes, truncated)。

    - 只放行 http/https，其余协议（file/ftp/…）直接拒，防本地文件泄露。
    - 读到 max_bytes 就停并置 truncated=True，不整包吞进内存。
    - 4xx/5xx 抛 NetError（带状态码），网络异常抛 NetError（带原因）。
    """
    if not isinstance(url, str) or not url.strip():
        raise NetError("(URL 为空)")
    url = url.strip()
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise NetError(f"(只支持 http/https 网址，拒绝 {scheme or '(无协议)'}:// —— "
                       f"file/ftp 等协议可能读取本地文件)")
    timeout = timeout or _net_timeout()
    req = urllib.request.Request(url, headers={
        "User-Agent": NET_UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.getcode()
            headers = {k.lower(): v for k, v in (r.headers.items() if r.headers else [])}
            body = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise NetError(f"(HTTP {e.code} 错误：{e.reason})")
    except urllib.error.URLError as e:
        raise NetError(f"(连不上目标：{getattr(e, 'reason', e)})")
    except Exception as e:
        raise NetError(f"(请求失败：{type(e).__name__}: {e})")
    truncated = len(body) > max_bytes
    if truncated:
        body = body[:max_bytes]
    return status, headers, body, truncated


class _HTMLToMarkdown(_HTMLParser):
    """把 HTML 转成结构近似的 Markdown 纯文本（stdlib HTMLParser，不引三方库）。

    - script/style/noscript/head/svg/template 里的内容整段丢弃
    - h1~h6 → #~######；p/div/li/blockquote → 块级换行；li 前缀 "- "；br → 换行
    - <a href> → [文字](href)；pre/code 内容原样保留
    - convert_charrefs=True 让 &amp; 之类自动还原，无需再 unescape
    目标是「可读、带链接、去掉导航噪音」，不追求像素级还原排版。
    """
    # ⚠ head 不在 _SKIP 里：<title> 就在 head 内，跳过 head 会连 title 一起丢。
    #    head 里其余标签要么是无文本的 void 元素（meta/link/base），要么
    #    script/style/noscript 已各自在下面被跳过，所以不 skip head 也不会漏噪音。
    _SKIP = {"script", "style", "noscript", "svg", "template"}
    _HEADING = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    _BLOCK = {"p", "div", "section", "article", "header", "footer", "main",
              "aside", "ul", "ol", "table", "tr", "blockquote", "figure",
              "figcaption", "hr", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.title = ""
        self._skip = 0          # >0 表示正处在要丢弃的子树里
        self._in_title = False
        self._href = None       # 当前 <a> 的 href
        self._link_buf = []     # 当前 <a> 的文字

    def _emit(self, s):
        if self._skip:
            return
        if self._in_title:
            self.title += s
        elif self._href is not None:
            self._link_buf.append(s)
        else:
            self.out.append(s)

    def _newline(self, n=2):
        if self._skip:
            return
        self.out.append("\n" * n)

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        ad = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in self._HEADING:
            self._newline(2)
            self._emit("#" * self._HEADING[tag] + " ")
        elif tag == "li":
            self._newline(1)
            self._emit("- ")
        elif tag == "br":
            self._newline(1)
        elif tag == "a":
            href = ad.get("href")
            self._href = href if (href and href.startswith("http")) else ""
            self._link_buf = []
        elif tag in self._BLOCK:
            self._newline(2)

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            text = "".join(self._link_buf).strip()
            href = self._href
            self._href = None
            self._link_buf = []
            if text and href:
                self._emit(f"[{text}]({href})")
            elif text:
                self._emit(text)
        elif tag in self._HEADING or tag in self._BLOCK:
            self._newline(2)

    def handle_data(self, data):
        if data:
            self._emit(data)

    def text(self):
        raw = "".join(self.out)
        # 逐行去尾空格，3+ 连续空行压成 1 个空行，整体 strip
        lines = [ln.rstrip() for ln in raw.split("\n")]
        cleaned, blank = [], 0
        for ln in lines:
            if ln.strip():
                cleaned.append(ln)
                blank = 0
            else:
                blank += 1
                if blank <= 1:
                    cleaned.append("")
        return self.title.strip(), "\n".join(cleaned).strip()


def _html_to_markdown(body, content_type):
    """HTML bytes → (title, markdown_text)。解析失败退回纯文本抽字。"""
    text, _ = _decode_body(body, content_type)
    try:
        p = _HTMLToMarkdown()
        p.feed(text)
        title, md = p.text()
        if md.strip():
            return title, md
    except Exception:
        pass
    # 兜底：HTMLParser 崩了或没抽到内容，退化成「去标签 + 还原实体」
    stripped = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", text)
    stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
    stripped = _html_mod.unescape(stripped)
    stripped = re.sub(r"[ \t]+", " ", stripped)
    stripped = re.sub(r"\n\s*\n\s*\n+", "\n\n", stripped)
    return "", stripped.strip()


def _clean_filename(name):
    """把模型/URL 给的文件名洗成安全的单层 basename：去路径分隔、去 ..、去控制符。"""
    name = (name or "").replace("\x00", "").strip()
    # 只取最后一段，防 a/b 或 a\\b 落到子目录
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\r\n\t]", "", name).strip().strip(".")
    # 去掉危险前缀，避免覆盖 .config.json 之类
    return name[:120]


def _guess_ext_from_ctype(ctype):
    """从 Content-Type 猜扩展名，猜不出返回空串。"""
    ctype = (ctype or "").split(";")[0].strip().lower()
    table = {
        "text/html": ".html", "text/plain": ".txt", "text/css": ".css",
        "text/csv": ".csv", "application/json": ".json",
        "application/javascript": ".js", "text/javascript": ".js",
        "application/xml": ".xml", "text/xml": ".xml",
        "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg",
        "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
        "application/zip": ".zip", "application/gzip": ".gz",
        "application/x-tar": ".tar", "audio/mpeg": ".mp3", "video/mp4": ".mp4",
        "application/octet-stream": "",
    }
    return table.get(ctype, "")


def _download_dir():
    """下载目录（FS_ROOT/downloads），不存在则创建。经 guard_path 保证在根内。"""
    root = DEPS.fs_root()
    d = DEPS.guard_path(DOWNLOAD_DIRNAME, want="any")   # 相对 FS_ROOT 解析
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DEPS.path_guard_error(f"(下载目录创建失败: {e})")
    # 万一 root 本身不可写，mkdir 会抛，上面已接住
    _ = root
    return d


def _h_fetch_url(a):
    """fetch_url：抓一个网页，HTML 转 Markdown 纯文本返回，超长截断标注。"""
    url = (a.get("url") or "").strip()
    if not url:
        return DEPS.outcome_failure, "(缺少 url)"
    max_chars = DEPS.int_arg(a.get("max_chars"), 0, 0, 200000)  # 0=用 maxOutputChars
    try:
        status, headers, body, truncated = _net_open(url, NET_FETCH_MAX_BYTES)
    except NetError as e:
        return DEPS.outcome_failure, str(e)

    ctype = headers.get("content-type", "")
    is_html = "html" in ctype.lower() or (not ctype and body[:512].lstrip()[:1] == b"<")

    notes = [f"URL: {url}", f"HTTP {status} · {DEPS.human_size(len(body))}"
             + (f" · {ctype}" if ctype else "")]
    if truncated:
        notes.append(f"（原始响应超过 {DEPS.human_size(NET_FETCH_MAX_BYTES)} 抓取上限，已截断）")

    if is_html:
        title, md = _html_to_markdown(body, ctype)
        if title:
            notes.insert(1, f"标题: {title}")
        content = md or "(页面正文为空，可能是纯 JS 渲染或需要登录)"
    else:
        # 非 HTML（JSON/纯文本/CSS…）：直接解码返回
        text, enc_note = _decode_body(body, ctype)
        if enc_note:
            notes.append(enc_note)
        content = text

    head = "\n".join(notes)
    body_out = head + "\n\n" + content
    # 二次截断到 max_chars（若模型指定了更小的预算）；最终 run_tool 还会过
    # _truncate_output 到 maxOutputChars，这里先按 max_chars 收一道省 token。
    if max_chars and len(body_out) > max_chars:
        dropped = len(body_out) - max_chars
        body_out = body_out[:max_chars] + f"\n…（按 max_chars 截断，丢弃 {dropped} 字）"
    return DEPS.outcome_success, body_out


def _h_download_file(a):
    """download_file：下载文件存到 FS_ROOT/downloads，返回落地路径。"""
    url = (a.get("url") or "").strip()
    if not url:
        return DEPS.outcome_failure, "(缺少 url)"

    try:
        status, headers, body, truncated = _net_open(url, NET_DOWNLOAD_MAX_BYTES)
    except NetError as e:
        return DEPS.outcome_failure, str(e)
    if truncated:
        return DEPS.outcome_failure, (
            f"(文件超过下载上限 {DEPS.human_size(NET_DOWNLOAD_MAX_BYTES)}，已中止。"
            f"请确认这是不是你要的完整文件)")

    ctype = headers.get("content-type", "")
    # 文件名优先级：模型显式 filename > Content-Disposition > URL basename > download+ext
    fname = _clean_filename(a.get("filename"))
    if not fname:
        cd = headers.get("content-disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';]+)", cd, re.I)
        if m:
            fname = _clean_filename(urllib.parse.unquote(m.group(1)))
    if not fname:
        base = _clean_filename(urlparse(url).path.rsplit("/", 1)[-1])
        fname = base
    if not fname:
        fname = "download" + (_guess_ext_from_ctype(ctype) or ".bin")
    # 若猜出的名字没有扩展名，而 content-type 能给出，补上
    if "." not in fname:
        ext = _guess_ext_from_ctype(ctype)
        if ext:
            fname += ext

    try:
        d = _download_dir()
        target = DEPS.guard_path(str(d / fname), want="any", protect_write=True)
    except DEPS.path_guard_error as e:
        return DEPS.outcome_failure, str(e)

    # 原子落盘：先写唯一临时名，再 os.replace，避免并行下载半成品被读走
    tmp = Path(str(target) + f".tmp{uuid.uuid4().hex[:8]}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(body)
        os.replace(str(tmp), str(target))
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return DEPS.outcome_failure, f"(写入失败: {e})"

    return DEPS.outcome_success, (
        f"已下载 HTTP {status} → {DEPS.rel(target, DEPS.fs_root())}"
        f"（{DEPS.human_size(len(body))}" + (f" · {ctype}" if ctype else "") + "）")


