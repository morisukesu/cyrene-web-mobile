#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
昔涟 · 手机版 Web Agent 运行时 v8

对齐桌面端（github.com/Playa-Cyrene/Cyrene-Agent）的移植范围：
  外观      双主题 charcoal-pink / pearl-white + 回复排版四滑块
            （token 取自 src/renderer/ui/tokens.css 与 themes/*.css）
  模型      api_base / api_key / model + 采样参数 + 超时 + 历史上限
  工具      内置 26 个（12 termux-api + shell + 6 文件 + 3 网络 + 5 元工具）
            逐个开关，Runtime 级生效；另有 Agent Loop 多轮工具调用
  插件      桌面端 Cyrene Plugin API v1 的 Node 插件（子进程桥 + NDJSON RPC），
            市场插件不改一行即可跑；见「插件体系」段与 runtime/plugin_host.cjs
  技能      skills/*/SKILL.md 的 front matter 开关，注入系统提示（清单式）
  语音      termux-tts-speak 自动朗读 + 语速/音调/语言 + 试听
  用量      token / 请求 / 工具调用累计，按模型分组
  服务      端口 / 监听地址 / 工具超时 / Markdown 与高亮开关

不在移植范围（Termux 无对应运行时，做了就是空壳）：
  ASR、BrowserControl、Channels、KnowledgeBase/RAG、MCP、Memory/DMAE、
  Music、Subagent、Sticker、AppUpdate

相对 v7 的修复：
  - main() 内 `import socket` 造成的 UnboundLocalError（原版根本起不来）
  - /sessions 回传全部 messages；send() 末尾未 await 的全量重拉
  - 全局锁覆盖整条 LLM 链路，一次慢请求冻结整个服务
  - 会话文件非原子写入；解析失败静默丢弃历史
  - 移动端：100vh 遮挡输入框、删除按钮靠 hover 永不可见、无手势 focus()
"""
import os, sys, json, re, time, subprocess, urllib.request, urllib.error, urllib.parse
import threading, uuid, socket, concurrent.futures, signal, shutil, fnmatch, tempfile
import atexit, hashlib, zipfile, stat, base64, io, queue
import html as _html_mod
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser as _HTMLParser
from urllib.parse import urlparse

# 修复 Termux/代理环境下 IPv6 挂起
socket.has_ipv6 = False

# 控制台编码兜底：Windows 默认 GBK，print("✓") 会抛 UnicodeEncodeError。
# Termux 是 UTF-8 本不需要，但 errors="replace" 保证任何环境下都不会
# 因为打印一个字符而让整个服务崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

# ========== 路径 ==========
BASE_DIR      = Path(__file__).parent.parent
RUNTIME_DIR   = Path(__file__).parent
PROMPTS_DIR   = BASE_DIR / "prompts"
SKILLS_DIR    = BASE_DIR / "skills"
DATA_DIR      = BASE_DIR / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"
CONFIG_FILE   = BASE_DIR / ".config.json"
USAGE_FILE    = DATA_DIR / "usage.json"
STATIC_DIR    = RUNTIME_DIR / "static"

# ========== 静态资源白名单 ==========
# 用 dict 精确匹配文件名做白名单：`../` 之类的穿越串永远命中不了 key，
# 因此不存在「先拼路径再判断是否越界」的窗口。
STATIC_FILES = {
    "marked.min.js":    "application/javascript; charset=utf-8",
    "highlight.min.js": "application/javascript; charset=utf-8",
    "hljs-theme.css":   "text/css; charset=utf-8",
    "app.css":          "text/css; charset=utf-8",
    "settings.css":     "text/css; charset=utf-8",
    "app.js":           "application/javascript; charset=utf-8",
}
# 素材子目录：按前缀开放的静态目录（表情包之类的图片素材）。
# 仍是白名单思路 —— 只放行「一层子目录 + 安全文件名 + 图片扩展名」，
# `..`、斜杠、绝对路径一律不收，也不存在「先拼路径再判越界」的窗口。
STATIC_PREFIX_DIRS = {
    "stickers/": {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                  "png": "image/png", "gif": "image/gif", "webp": "image/webp"},
}
STATIC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def static_ctype(name):
    """把请求名解析成 MIME；任何规则都命中不了就返回 None（调用方回 404）。"""
    ctype = STATIC_FILES.get(name)
    if ctype is not None:
        return ctype
    for prefix, exts in STATIC_PREFIX_DIRS.items():
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        if "/" in rest or "\\" in rest or ".." in rest:
            return None
        if not STATIC_NAME_RE.match(rest):
            return None
        return exts.get(rest.rsplit(".", 1)[-1].lower() if "." in rest else "")
    return None


# 自产文件改动频繁，不缓存；第三方库可长缓存
STATIC_NOCACHE = {"app.css", "settings.css", "app.js"}
STATIC_CACHE = {}
STATIC_CACHE_LOCK = threading.Lock()


def load_static(name):
    # 白名单里的自产文件、以及素材子目录下的图，都不进内存缓存：
    # 前者改动频繁；后者数量会涨（表情包几十张），常驻内存不划算，读盘也够快。
    if name in STATIC_NOCACHE or name not in STATIC_FILES:
        try:
            return (STATIC_DIR / name).read_bytes()
        except OSError:
            return None
    with STATIC_CACHE_LOCK:
        if name in STATIC_CACHE:
            return STATIC_CACHE[name]
    try:
        data = (STATIC_DIR / name).read_bytes() if (STATIC_DIR / name).is_file() else None
    except OSError:
        data = None
    with STATIC_CACHE_LOCK:
        STATIC_CACHE[name] = data
    return data


# ========== 工具定义 ==========
# ========== 文件工具实现层（阶段 2a）==========
# 与 termux-api 工具的根本差异：这些工具不 fork 子进程，直接在服务进程内跑
# Python。所以 run_tool 需要一个 `handler` 分支（见下方 TOOLS 注释与 run_tool）。
#
# handler 约定：handler(args) -> (outcome, result_text)
#   outcome 取 OUTCOME_SUCCESS / OUTCOME_FAILURE / OUTCOME_UNKNOWN
#   args 是**已按 params 类型归一**的 dict（int 还是 int，bool 还是 bool），
#   与走命令行的 resolve_args（全转 str）不同，见 resolve_typed()。
#
# 路径守卫（对齐桌面端 Code 模式「绑定可信工作目录」）：
#   · 只允许 FS_ROOT（默认 $HOME，可用 CYRENE_FS_ROOT 覆盖）之内
#   · abspath 消 `..` 判一次，realpath 解符号链接再判一次 —— 两道都过才放行。
#     只做 realpath 会让「root 自己是符号链接」时比较错位；只做 abspath 则
#     挡不住 `ln -s /` 这类逃逸。
#   · 写操作另有一份硬保护名单（配置/会话/运行时自身），越权直接拒。

FS_ROOT_DEFAULT = Path.home()


def _fs_root():
    """允许访问的根目录。每次调用现算，方便单元测试改环境变量或改模块全局。"""
    raw = os.environ.get("CYRENE_FS_ROOT")
    try:
        return Path(raw).resolve() if raw else Path(FS_ROOT_DEFAULT).resolve()
    except (OSError, ValueError):
        return Path(FS_ROOT_DEFAULT).resolve()


# 写入硬保护名单：服务自身的命脉文件。读不受限，写一律拒。
# 改运行时代码必须走部署流程（deploy_web_v8.py + _clean_restart.py），
# 让模型直接 write_file 覆盖正在运行的 .py 等于把服务往死里改。
def _fs_protected():
    out = set()
    for p in (CONFIG_FILE, SESSIONS_FILE, USAGE_FILE):
        try:
            out.add(Path(p).resolve())
        except (OSError, ValueError):
            pass
    try:
        out.add(Path(__file__).resolve())
    except (OSError, ValueError, NameError):
        pass
    return out


# 扫描类工具的性能护栏：手机 CPU 弱，$HOME 下可能有几千个文件。
FS_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                ".mypy_cache", ".pytest_cache", ".tox", "site-packages"}
# 扫描类工具跳过的文件名。write_file/edit_file 每次改文件都会留 .bak，
# 写入过程还会短暂存在 .tmpXXXX —— 让 grep 同时命中原文和备份，
# 模型看到的匹配数会翻倍，纯属噪音。要查备份就直接 read_file 那个 .bak。
FS_SKIP_SUFFIXES = (".bak", ".tmp", ".swp", ".orig", ".rej")


def _skip_file(name):
    """扫描时是否跳过这个文件名（备份/临时/残留）。"""
    low = name.lower()
    if low.endswith(FS_SKIP_SUFFIXES):
        return True
    # 本工具自己写的临时文件形如 foo.txt.tmp1a2b3c4d
    if ".tmp" in low:
        return True
    return name.endswith("~")


FS_MAX_WALK = 20000        # 单次 glob/grep 最多遍历多少个条目
FS_GLOB_LIMIT = 100        # glob_files 返回上限
FS_GREP_LIMIT = 250        # grep_files 匹配行上限
FS_READ_DEFAULT_LINES = 2000
FS_READ_MAX_LINES = 20000
FS_MAX_FILE_BYTES = 4 * 1024 * 1024     # 读取上限 4 MiB，防 OOM
FS_WRITE_MAX_BYTES = 8 * 1024 * 1024    # 写入上限 8 MiB
FS_LIST_LIMIT = 500        # list_dir 返回上限


class PathGuardError(Exception):
    """路径越界或不可写。消息直接回给模型，所以要说清为什么被拒。"""


def guard_path(raw, want="any", protect_write=False):
    """把模型给的路径解析成受控的绝对 Path；越界或违规抛 PathGuardError。

    want: "any" 不校验存在性 / "file" 必须是已存在文件 / "dir" 必须是已存在目录
    protect_write: True 时额外过写入保护名单
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathGuardError("(路径为空。请给出文件路径，相对路径按根目录解析)")
    raw = raw.strip().replace("\x00", "")
    root = _fs_root()

    # ~ 展开只认根目录自己，别的用户家目录不给碰
    if raw == "~":
        cand = root
    elif raw.startswith("~/") or raw.startswith("~" + os.sep):
        cand = root / raw[2:]
    else:
        p = Path(raw)
        cand = p if p.is_absolute() else (root / p)

    # 第一道：abspath 消掉 .. 后必须仍在 root 内
    try:
        ap = Path(os.path.abspath(str(cand)))
    except (OSError, ValueError) as e:
        raise PathGuardError(f"(路径无法解析: {e})")
    if ap != root and root not in ap.parents:
        raise PathGuardError(
            f"(路径越界：只允许访问 {root} 之内的文件，拒绝 {ap})")

    # 第二道：解开符号链接后再判一次，防 `ln -s /` 逃逸
    try:
        rp = Path(os.path.realpath(str(ap)))
    except (OSError, ValueError) as e:
        raise PathGuardError(f"(路径无法解析: {e})")
    real_root = Path(os.path.realpath(str(root)))
    if rp != real_root and real_root not in rp.parents:
        raise PathGuardError(
            f"(路径经符号链接指向 {root} 之外，已拒绝: {rp})")

    if protect_write and rp in _fs_protected():
        raise PathGuardError(
            f"(该文件受写入保护，不允许通过工具修改: {rp.name}。"
            f"服务配置与会话数据只能在设置面板里改)")

    if want == "file":
        if not ap.is_file():
            raise PathGuardError(f"(文件不存在或不是普通文件: {ap})")
    elif want == "dir":
        if not ap.is_dir():
            raise PathGuardError(f"(目录不存在: {ap})")
    return ap


def _int_arg(v, default, lo, hi):
    """把参数夹成 [lo, hi] 内的 int。非法值回落 default，绝不抛。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if n != n:      # NaN（int() 不会产生，防 float 传进来）
        return default
    return max(lo, min(hi, n))


def _bool_arg(v, default=False):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off"):
            return False
    return default


def _decode_bytes(data):
    """bytes → (text, encoding_note)。UTF-8 优先，失败退化 replace 并明示。"""
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "（含非 UTF-8 字节，已用替代字符显示）"


def _scan_base(root_raw):
    """解析扫描起点目录（glob/grep 用）。缺省 = FS_ROOT。"""
    if not root_raw or not str(root_raw).strip():
        return _fs_root()
    return guard_path(root_raw, want="dir")


def _walk_files(base):
    """生成 base 下的文件 Path，跳过噪音目录与备份文件，带总条目上限防卡死。"""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames[:] = [d for d in dirnames if d not in FS_SKIP_DIRS]
        for fn in filenames:
            if _skip_file(fn):
                continue
            seen += 1
            if seen > FS_MAX_WALK:
                return
            yield Path(dirpath) / fn


def _rel(p, base):
    try:
        return str(Path(p).relative_to(base)).replace(os.sep, "/")
    except (ValueError, OSError):
        return str(p)


def _human_size(n):
    """字节数转人类可读。手机屏幕窄，保持 5 字以内。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


# ---------- 六个 handler ----------

def _h_read_file(a):
    """read_file：按行分段读文本文件，带行号。"""
    try:
        p = guard_path(a.get("path"), want="file")
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)

    try:
        size = p.stat().st_size
    except OSError as e:
        return OUTCOME_FAILURE, f"(读不到文件信息: {e})"
    if size > FS_MAX_FILE_BYTES:
        return OUTCOME_FAILURE, (
            f"(文件 {size} 字节，超过读取上限 {FS_MAX_FILE_BYTES} 字节，"
            f"请改用 grep_files 定位后再按 offset/limit 分段读)")

    try:
        data = p.read_bytes()
    except OSError as e:
        return OUTCOME_FAILURE, f"(读取失败: {e})"
    if b"\x00" in data[:8192]:
        return OUTCOME_FAILURE, (
            f"(这是二进制文件（{_human_size(size)}），不能当文本读)")

    text, enc_note = _decode_bytes(data)
    lines = text.split("\n")
    total = len(lines)
    if lines and lines[-1] == "":
        total -= 1          # 末尾换行不算一行
        lines = lines[:-1]

    offset = _int_arg(a.get("offset"), 0, 0, 10 ** 9)
    limit = _int_arg(a.get("limit"), FS_READ_DEFAULT_LINES, 1, FS_READ_MAX_LINES)
    if offset >= max(total, 1) and total > 0:
        return OUTCOME_FAILURE, (
            f"(offset={offset} 超出文件末尾。该文件共 {total} 行)")

    chunk = lines[offset:offset + limit]
    width = len(str(offset + len(chunk))) if chunk else 1
    body = "\n".join(f"{offset + i + 1:>{width}} | {ln}" for i, ln in enumerate(chunk))

    head = f"文件: {_rel(p, _fs_root())}  共 {total} 行 / {_human_size(size)}"
    if offset or len(chunk) < total:
        head += f"  · 本次显示第 {offset + 1}-{offset + len(chunk)} 行"
    notes = [head]
    if enc_note:
        notes.append(enc_note)
    if offset + len(chunk) < total:
        notes.append(f"（还有 {total - offset - len(chunk)} 行未显示，"
                     f"需要就传 offset={offset + len(chunk)} 继续读）")
    notes.append("（左侧数字是行号，仅供定位。edit_file 的 old_string 要用文件原文，"
                 "不要把行号一起复制进去）")
    return OUTCOME_SUCCESS, "\n".join(notes) + "\n" + body


def _h_write_file(a):
    """write_file：整文件覆写。已存在则先备份 .bak，落盘走临时文件 + os.replace。"""
    try:
        p = guard_path(a.get("path"), want="any", protect_write=True)
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)

    content = a.get("content")
    if content is None:
        return OUTCOME_FAILURE, "(缺少 content 参数)"
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)

    raw = content.encode("utf-8")
    if len(raw) > FS_WRITE_MAX_BYTES:
        return OUTCOME_FAILURE, (
            f"(内容 {len(raw)} 字节，超过写入上限 {FS_WRITE_MAX_BYTES} 字节，请分批写)")

    existed = p.exists()
    if existed and p.is_dir():
        return OUTCOME_FAILURE, f"(目标是个目录，不能当文件写: {p})"

    bak_note = ""
    # 临时名必须唯一：dispatch_tools 并行跑，两个线程同时写同一文件时
    # 用 pid 会撞名，导致一方 replace 掉另一方写了一半的内容。
    tmp = Path(str(p) + f".tmp{uuid.uuid4().hex[:8]}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            # 写前备份：只留最近一份，避免 .bak 无限堆积
            shutil.copy2(str(p), str(p) + ".bak")
            bak_note = f"，原文件已备份为 {p.name}.bak"
        tmp.write_bytes(raw)
        os.replace(str(tmp), str(p))       # 原子替换，中途崩不会留半截文件
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return OUTCOME_FAILURE, f"(写入失败: {e})"

    nlines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return OUTCOME_SUCCESS, (
        f"已写入 {_rel(p, _fs_root())}（{len(raw)} 字节，{nlines} 行{bak_note}）")


def _h_edit_file(a):
    """edit_file：精确字符串替换。匹配数不等于预期就报错，绝不猜。"""
    try:
        p = guard_path(a.get("path"), want="file", protect_write=True)
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)

    old = a.get("old_string")
    new = a.get("new_string")
    if not isinstance(old, str) or old == "":
        return OUTCOME_FAILURE, "(old_string 为空。要整文件覆写请用 write_file)"
    if not isinstance(new, str):
        return OUTCOME_FAILURE, "(new_string 缺失或类型不对)"
    if old == new:
        return OUTCOME_FAILURE, "(old_string 与 new_string 相同，无需修改)"

    try:
        data = p.read_bytes()
    except OSError as e:
        return OUTCOME_FAILURE, f"(读取失败: {e})"
    if len(data) > FS_MAX_FILE_BYTES:
        return OUTCOME_FAILURE, (
            f"(文件 {len(data)} 字节过大，超过 {FS_MAX_FILE_BYTES} 字节上限，"
            f"请改用 write_file 分段重写)")
    if b"\x00" in data[:8192]:
        return OUTCOME_FAILURE, "(二进制文件，不能做文本替换)"

    text, _ = _decode_bytes(data)
    n = text.count(old)
    replace_all = _bool_arg(a.get("replace_all"), False)

    if n == 0:
        hint = ""
        # 最常见的原因是空白/缩进对不上，给个可操作提示而不是干巴巴报错
        probe = old.strip()
        if probe and probe in text:
            hint = "（提示：去掉首尾空白后能匹配到，说明你给的 old_string 缩进或换行与文件不一致，请先 read_file 核对原文）"
        return OUTCOME_FAILURE, f"(在文件里找不到 old_string，未做任何修改{hint})"
    if n > 1 and not replace_all:
        return OUTCOME_FAILURE, (
            f"(old_string 在文件里出现 {n} 次，无法确定改哪一处，未做任何修改。"
            f"请把上下文加长到唯一，或显式传 replace_all=true 全部替换)")

    result = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    raw = result.encode("utf-8")
    tmp = Path(str(p) + f".tmp{uuid.uuid4().hex[:8]}")   # 并行安全，同 write_file
    try:
        shutil.copy2(str(p), str(p) + ".bak")
        tmp.write_bytes(raw)
        os.replace(str(tmp), str(p))
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return OUTCOME_FAILURE, f"(写入失败: {e})"

    return OUTCOME_SUCCESS, (
        f"已替换 {n if replace_all else 1} 处 → {_rel(p, _fs_root())}"
        f"（原文件备份为 {p.name}.bak）")


def _h_glob_files(a):
    """glob_files：按通配模式找文件，返回相对路径列表。"""
    pattern = a.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return OUTCOME_FAILURE, "(缺少 pattern，例如 **/*.py 或 *.md)"
    pattern = pattern.strip()
    try:
        base = _scan_base(a.get("root"))
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)

    hits = []
    truncated = False
    for f in _walk_files(base):
        rel = _rel(f, base)
        name = f.name
        # 支持三种写法：纯文件名模式 / 带目录的模式 / ** 递归模式
        if (fnmatch.fnmatch(name, pattern)
                or fnmatch.fnmatch(rel, pattern)
                or ("/" not in pattern and fnmatch.fnmatch(rel, "*/" + pattern))):
            hits.append(rel)
            if len(hits) >= FS_GLOB_LIMIT:
                truncated = True
                break

    if not hits:
        return OUTCOME_SUCCESS, f"(在 {_rel(base, _fs_root()) or '.'} 下没有匹配 {pattern} 的文件)"
    hits.sort()
    head = f"匹配 {pattern}：{len(hits)} 个文件" + ("（已到上限，可能还有更多）" if truncated else "")
    return OUTCOME_SUCCESS, head + "\n" + "\n".join(hits)


def _h_grep_files(a):
    """grep_files：正则搜文件内容，带行号与可选上下文。"""
    pattern = a.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return OUTCOME_FAILURE, "(缺少 pattern)"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return OUTCOME_FAILURE, f"(正则表达式不合法: {e})"

    try:
        base = _scan_base(a.get("root"))
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)

    fileglob = (a.get("glob") or "").strip() if isinstance(a.get("glob"), str) else ""
    ctx = _int_arg(a.get("context"), 0, 0, 10)
    only_names = _bool_arg(a.get("files_with_matches"), False)

    out = []
    nmatch = 0        # 真正匹配上的行数（不含 context 行）
    nline = 0         # 输出行数（含 context 行），受 FS_GREP_LIMIT 约束
    nfile = 0
    truncated = False
    for f in _walk_files(base):
        rel = _rel(f, base)
        if fileglob and not (fnmatch.fnmatch(f.name, fileglob)
                             or fnmatch.fnmatch(rel, fileglob)):
            continue
        try:
            if f.stat().st_size > FS_MAX_FILE_BYTES:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue                      # 跳过二进制
        text, _ = _decode_bytes(data)
        lines = text.split("\n")
        idxs = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not idxs:
            continue
        nfile += 1
        nmatch += len(idxs)
        if only_names:
            out.append(f"{rel}（{len(idxs)} 处）")
            nline += 1
            if nline >= FS_GREP_LIMIT:
                truncated = True
                break
            continue

        shown = set()
        hitset = set(idxs)
        groups = []
        for i in idxs:
            lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
            if groups and lo <= groups[-1][1]:
                groups[-1] = (groups[-1][0], max(groups[-1][1], hi))
            else:
                groups.append((lo, hi))
        for lo, hi in groups:
            for j in range(lo, hi):
                if j in shown:
                    continue
                shown.add(j)
                mark = ":" if j in hitset else "-"
                out.append(f"{rel}{mark}{j + 1}{mark} {lines[j]}")
                nline += 1
                if nline >= FS_GREP_LIMIT:
                    truncated = True
                    break
            if truncated:
                break
            if ctx:
                out.append("--")
        if truncated:
            break

    if not out:
        return OUTCOME_SUCCESS, f"(在 {_rel(base, _fs_root()) or '.'} 下没有匹配 /{pattern}/ 的内容)"
    head = (f"匹配 /{pattern}/：{nmatch} 处，分布在 {nfile} 个文件"
            + (f"（已到 {FS_GREP_LIMIT} 行输出上限，结果被截断，请缩小 root 或加 glob 过滤）"
               if truncated else ""))
    return OUTCOME_SUCCESS, head + "\n" + "\n".join(out)


def _h_list_dir(a):
    """list_dir：列目录内容，目录在前、文件在后，带大小。"""
    try:
        p = guard_path(a.get("path"), want="dir") if str(a.get("path") or "").strip() \
            else _fs_root()
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)

    try:
        entries = sorted(os.scandir(str(p)), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return OUTCOME_FAILURE, f"(列目录失败: {e})"

    dirs, files = [], []
    for e in entries[:FS_LIST_LIMIT]:
        try:
            if e.is_dir():
                dirs.append(f"{e.name}/")
            else:
                files.append(f"{e.name}  ({_human_size(e.stat().st_size)})")
        except OSError:
            files.append(f"{e.name}  (?)")

    total = len(entries)
    lines = dirs + files
    if total > FS_LIST_LIMIT:
        lines.append(f"…（还有 {total - FS_LIST_LIMIT} 项未列出）")
    head = (f"目录: {_rel(p, _fs_root()) or '.'}  ·  "
            f"{len(dirs)} 个子目录 / {len(files)} 个文件"
            + ("（已截断）" if total > FS_LIST_LIMIT else ""))
    if not lines:
        return OUTCOME_SUCCESS, head + "\n(空目录)"
    return OUTCOME_SUCCESS, head + "\n" + "\n".join(lines)


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
        base = int(agent_cfg("stepTimeout") or 30)
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


# ---------- web_search 后端解析 ----------

def _search_parse_bing(html_text):
    """Bing：结果块 <li class="b_algo"> 内 <h2><a href>title</a> + <p>snippet。"""
    items = []
    for blk in re.findall(r'(?s)<li class="b_algo".*?</li>', html_text):
        m = re.search(r'(?s)<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk)
        if not m:
            continue
        url = _html_mod.unescape(m.group(1)).strip()
        title = _html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        sn = re.search(r"(?s)<p[^>]*>(.*?)</p>", blk)
        snippet = _html_mod.unescape(re.sub(r"<[^>]+>", "", sn.group(1))).strip() if sn else ""
        if url.startswith("http") and title:
            items.append((title, url, snippet))
    return items


def _search_parse_ddg(html_text):
    """DuckDuckGo HTML 版：<a class="result__a" href>title</a> + result__snippet。"""
    items = []
    links = re.findall(r'(?s)<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                       html_text)
    snips = re.findall(r'(?s)class="result__snippet"[^>]*>(.*?)</a>', html_text)
    for i, (url, title) in enumerate(links):
        title = _html_mod.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snippet = (_html_mod.unescape(re.sub(r"<[^>]+>", "", snips[i])).strip()
                   if i < len(snips) else "")
        url = _html_mod.unescape(url).strip()
        # DDG 常给 /l/?uddg=<encoded> 跳转包裹，解出真实 URL
        if "/l/?" in url or url.startswith("//duckduckgo.com/l/"):
            m = re.search(r"uddg=([^&]+)", url)
            if m:
                url = urllib.parse.unquote(m.group(1))
        if url.startswith("http") and title:
            items.append((title, url, snippet))
    return items


# 后端名 → (URL 模板, 解析函数)。{q} 由已 quote 的查询串替换。
_NET_SEARCH_SOURCES = {
    "bing":    ("https://www.bing.com/search?q={q}&setlang=zh-CN", _search_parse_bing),
    "cn.bing": ("https://cn.bing.com/search?q={q}&setlang=zh-CN", _search_parse_bing),
    "ddg":     ("https://html.duckduckgo.com/html/?q={q}", _search_parse_ddg),
}


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
    root = _fs_root()
    d = guard_path(DOWNLOAD_DIRNAME, want="any")   # 相对 FS_ROOT 解析
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PathGuardError(f"(下载目录创建失败: {e})")
    # 万一 root 本身不可写，mkdir 会抛，上面已接住
    _ = root
    return d


# ---------- 三个 handler ----------

def _h_web_search(a):
    """web_search：抓搜索引擎结果页，返回标题/链接/摘要列表。多后端降级。"""
    query = (a.get("query") or "").strip()
    if not query:
        return OUTCOME_FAILURE, "(缺少 query)"
    n = _int_arg(a.get("max_results"), NET_DEFAULT_RESULTS, 1, NET_MAX_RESULTS)

    q = urllib.parse.quote(query)
    timeout = _net_timeout()
    errors = []
    for backend in NET_SEARCH_BACKENDS:
        tmpl, parser = _NET_SEARCH_SOURCES[backend]
        url = tmpl.replace("{q}", q)
        try:
            _status, _headers, body, _trunc = _net_open(
                url, NET_FETCH_MAX_BYTES, timeout)
        except NetError as e:
            errors.append(f"{backend}: {e}")
            continue
        ctype = _headers.get("content-type", "")
        text, _ = _decode_body(body, ctype)
        try:
            items = parser(text)
        except Exception as e:
            errors.append(f"{backend}: 解析失败 {type(e).__name__}")
            continue
        if not items:
            errors.append(f"{backend}: 解析出 0 条（页面结构可能变了）")
            continue
        # 去重（按 URL）后截到 n 条
        seen, uniq = set(), []
        for title, uurl, snippet in items:
            if uurl in seen:
                continue
            seen.add(uurl)
            uniq.append((title, uurl, snippet))
            if len(uniq) >= n:
                break
        lines = [f"搜索「{query}」· 后端 {backend} · {len(uniq)} 条结果"]
        for i, (title, uurl, snippet) in enumerate(uniq, 1):
            lines.append(f"{i}. {title}")
            lines.append(f"   {uurl}")
            if snippet:
                sn = snippet if len(snippet) <= 200 else snippet[:200] + "…"
                lines.append(f"   {sn}")
        return OUTCOME_SUCCESS, "\n".join(lines)

    return OUTCOME_FAILURE, ("(所有搜索后端都失败了。" + " | ".join(errors) +
                             "。可稍后重试，或用 fetch_url 直接抓已知网址)")


def _h_fetch_url(a):
    """fetch_url：抓一个网页，HTML 转 Markdown 纯文本返回，超长截断标注。"""
    url = (a.get("url") or "").strip()
    if not url:
        return OUTCOME_FAILURE, "(缺少 url)"
    max_chars = _int_arg(a.get("max_chars"), 0, 0, 200000)  # 0=用 maxOutputChars
    try:
        status, headers, body, truncated = _net_open(url, NET_FETCH_MAX_BYTES)
    except NetError as e:
        return OUTCOME_FAILURE, str(e)

    ctype = headers.get("content-type", "")
    is_html = "html" in ctype.lower() or (not ctype and body[:512].lstrip()[:1] == b"<")

    notes = [f"URL: {url}", f"HTTP {status} · {_human_size(len(body))}"
             + (f" · {ctype}" if ctype else "")]
    if truncated:
        notes.append(f"（原始响应超过 {_human_size(NET_FETCH_MAX_BYTES)} 抓取上限，已截断）")

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
    return OUTCOME_SUCCESS, body_out


def _h_download_file(a):
    """download_file：下载文件存到 FS_ROOT/downloads，返回落地路径。"""
    url = (a.get("url") or "").strip()
    if not url:
        return OUTCOME_FAILURE, "(缺少 url)"

    try:
        status, headers, body, truncated = _net_open(url, NET_DOWNLOAD_MAX_BYTES)
    except NetError as e:
        return OUTCOME_FAILURE, str(e)
    if truncated:
        return OUTCOME_FAILURE, (
            f"(文件超过下载上限 {_human_size(NET_DOWNLOAD_MAX_BYTES)}，已中止。"
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
        target = guard_path(str(d / fname), want="any", protect_write=True)
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)

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
        return OUTCOME_FAILURE, f"(写入失败: {e})"

    return OUTCOME_SUCCESS, (
        f"已下载 HTTP {status} → {_rel(target, _fs_root())}"
        f"（{_human_size(len(body))}" + (f" · {ctype}" if ctype else "") + "）")


# ========== Agent 元工具实现层（阶段 2c）==========
# 元工具 = 不碰手机硬件/文件/网络，只操作 **Agent 自身状态** 的工具：
#   update_todo           会话级工作笔记（落 session.todos，前端渲染进度）
#   ask_user              向用户提问，**排他**：命中即终止本轮 loop
#   invoke_skill          按需读 skills/{id}/SKILL.md 正文
#   read_skill_reference  按需读 skills/{id}/references/*
#   shell_job             查/停 run_in_background 起出的后台命令
#
# 与文件/网络 handler 的根本差异：update_todo 必须知道「当前是哪个会话」。
# handler 签名是 handler(args)，没有会话上下文；而 dispatch_tools 用线程池并发
# 跑工具 —— threading.local 在子线程里读不到主线程设的值，contextvars 同理
# （ThreadPoolExecutor 不自动传播上下文）。
# 解法：run_tool 收第三个可选参数 ctx，TOOLS 里标 needs_ctx=True 的 handler
# 调用时传 handler(args, ctx)，其余仍传 handler(args)。已有 9 个 handler 一行不改。
META_TODO_MAX = 40            # update_todo 单次最多多少条
META_TODO_TEXT = 300          # 单条 content / activeForm 截断长度
TODO_STATUSES = ("pending", "in_progress", "completed", "cancelled")
ASK_MAX_QUESTIONS = 3         # 一次最多问几个问题（对齐桌面端 ask_user）
ASK_MAX_OPTIONS = 12
ASK_TEXT_LIMIT = 500          # 问题文本截断长度
ASK_OPT_LIMIT = 200           # 选项文本截断长度

# 排他工具：命中即结束本轮 loop，同轮其余调用一律合成 not_executed 闭合槽位。
# 对齐桌面端「Ask 与其他工具互斥」：一边问用户一边偷偷把文件改了，
# 用户答完才发现事情已经做完，提问就失去了意义。
EXCLUSIVE_TOOLS = ("ask_user",)

# ---------- 后台 shell 任务 ----------
# 登记簿：jobId → {id, pid, pgid, cmd, log, startedAt, status, exitCode, bytes}
# 只记 pid/pgid，不长期持有 Popen 句柄；stop 时按进程组 killpg —— 与 run_tool
# 超时杀树同一套做法，否则 `sh -c` 死了它 fork 出来的孙子进程还在跑。
SHELL_JOBS = {}
SHELL_JOBS_LOCK = threading.Lock()
SHELL_JOB_DIRNAME = "shell_jobs"        # 相对 FS_ROOT 的日志目录
SHELL_JOB_TAIL = 8192                   # status 回读的尾部字节数
SHELL_JOB_KEEP = 20                     # 登记簿最多留多少条（含已结束），超了丢最旧
SHELL_JOB_MAX_SEC = 1800                # 后台任务最长 30 分钟，到点由 reaper 杀
# per-call 超时覆盖区间（毫秒）。上限与 SHELL_JOB_MAX_SEC 对齐；
# 下限 1s 防止模型传个 0 让命令刚 fork 就被杀。
SHELL_TIMEOUT_MS_RANGE = (1000, 1800000)


def normalize_todos(raw):
    """把模型给的 todo 数组洗成合法结构。返回 (items, notes, error)。

    error 非空表示整批拒绝（结构不对，落库了也是脏数据，让模型重来）。
    notes 是「自动修正了什么」的说明 —— 能修正就修正而不是拒绝，todo 只是
    工作笔记，格式偏差没必要打断任务；但修正必须明示，否则模型下一轮
    会以为自己写的状态生效了。
    """
    if not isinstance(raw, list):
        return [], [], "(todos 必须是数组，每项含 id/content/status)"
    if len(raw) > META_TODO_MAX:
        return [], [], f"(一次最多 {META_TODO_MAX} 条 todo，收到 {len(raw)} 条。请合并后再提交)"

    items, notes = [], []
    seen_ids = set()
    running = None            # 当前唯一的 in_progress id
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            return [], [], f"(第 {i + 1} 项不是对象，todo 必须是 {{id, content, status}} 形式)"
        tid = str(it.get("id") or "").strip()
        content = str(it.get("content") or "").strip()
        status = str(it.get("status") or "").strip()
        active = str(it.get("activeForm") or "").strip()

        if not tid:
            return [], [], "(有一项缺 id。id 用于跨轮次追踪同一条任务，必须给)"
        if tid in seen_ids:
            return [], [], f"(id 重复: {tid}。每条 todo 的 id 必须唯一)"
        if not content:
            return [], [], f"(第 {i + 1} 项（id={tid}）缺 content)"
        if status not in TODO_STATUSES:
            return [], [], (f"(id={tid} 的 status 非法: {status!r}。"
                            f"只能是 {'/'.join(TODO_STATUSES)})")
        seen_ids.add(tid)

        # 同时只允许一个 in_progress：多个并存等于没有「当前在做哪一步」，
        # 前端进度条也无从渲染。保留第一个，其余降回 pending 并告知。
        if status == "in_progress":
            if running is None:
                running = tid
            else:
                status = "pending"
                notes.append(f"id={tid} 的 in_progress 被降为 pending"
                             f"（同一时刻只允许一条进行中，当前是 {running}）")

        items.append({
            "id": tid[:80],
            "content": content[:META_TODO_TEXT],
            "status": status,
            "activeForm": active[:META_TODO_TEXT],
        })
    return items, notes, None


def _session_get(sid):
    """按 sid 取会话对象（持 STORE_LOCK）。不存在返回 None。"""
    if not sid:
        return None
    with STORE_LOCK:
        return SESSIONS.get(sid)


def _session_set_todos(sid, items):
    """把 todo 写进会话并落盘。返回 True/False。

    持锁时间只有一次 dict 赋值 + 一次原子写，毫秒级 —— 与 _handle_send
    的锁拆分原则一致，绝不在锁里跑 LLM 或工具。
    """
    if not sid:
        return False
    with STORE_LOCK:
        s = SESSIONS.get(sid)
        if s is None:
            return False
        s["todos"] = items
        return save_sessions(SESSIONS)


def _h_update_todo(a, ctx=None):
    """update_todo：整表替换当前会话的工作笔记。

    语义对齐桌面端 mutable working notebook：传进来的是**完整新表**，
    不是增量。模型每轮把全量 todos 重写一遍，Runtime 负责校验与落盘。
    """
    sid = (ctx or {}).get("sid")
    items, notes, err = normalize_todos(a.get("todos"))
    if err:
        return OUTCOME_FAILURE, err

    s = _session_get(sid)
    if s is None:
        # 会话不在内存里（单元测试直接调 handler、或会话刚被删）。
        # 不能静默成功 —— 模型会以为笔记存下来了，下一轮接着引用。
        return OUTCOME_FAILURE, (
            "(当前没有可用会话上下文，todo 未能保存。"
            "这通常发生在脱离对话流程直接调用工具时)")

    if not _session_set_todos(sid, items):
        return OUTCOME_FAILURE, "(todo 写入会话失败：会话不存在或落盘失败)"

    done = sum(1 for x in items if x["status"] == "completed")
    cancel = sum(1 for x in items if x["status"] == "cancelled")
    prog = sum(1 for x in items if x["status"] == "in_progress")
    lines = [f"待办已更新：共 {len(items)} 条 · 完成 {done} · 进行中 {prog}"
             f" · 取消 {cancel} · 待办 {len(items) - done - cancel - prog}"]
    for x in items:
        mark = {"pending": "[ ]", "in_progress": "[~]",
                "completed": "[x]", "cancelled": "[-]"}[x["status"]]
        lines.append(f"  {mark} {x['content']}")
    if notes:
        lines.append("已自动修正：" + "；".join(notes))
    return OUTCOME_SUCCESS, "\n".join(lines)


def _normalize_ask(questions):
    """把模型给的问题数组洗成前端能直接渲染的结构。返回 (qs, notes, error)。"""
    if not isinstance(questions, list) or not questions:
        return [], [], "(questions 必须是非空数组)"
    if len(questions) > ASK_MAX_QUESTIONS:
        return [], [], (f"(一次最多问 {ASK_MAX_QUESTIONS} 个问题，收到 {len(questions)} 个。"
                        f"请挑最关键的，或合并成一个问题)")

    qs, notes = [], []
    seen = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            return [], [], f"(第 {i + 1} 个问题不是对象)"
        qid = str(q.get("id") or "").strip() or f"q{i + 1}"
        if qid in seen:
            qid = f"{qid}_{i + 1}"
            notes.append(f"问题 id 重复，已改为 {qid}")
        seen.add(qid)
        text = str(q.get("question") or "").strip()
        if not text:
            return [], [], f"(第 {i + 1} 个问题缺 question 文本)"
        qtype = str(q.get("type") or "").strip()
        raw_opts = q.get("options")

        if qtype == "text":
            opts = None                     # 自由填写不需要选项
        elif not isinstance(raw_opts, list) or not raw_opts:
            if qtype in ("single_select", "multi_select"):
                return [], [], (f"(问题 {qid} 声明为 {qtype} 但没给 options。"
                                f"选择题必须带可选项)")
            # 没声明类型又没选项 → 当自由填写处理，比报错打断更合理
            qtype, opts = "text", None
        else:
            qtype = qtype if qtype in ("single_select", "multi_select") else "single_select"
            opts = raw_opts[:ASK_MAX_OPTIONS]
            if len(raw_opts) > ASK_MAX_OPTIONS:
                notes.append(f"问题 {qid} 的选项超过 {ASK_MAX_OPTIONS} 个，已截断")

        clean_opts = []
        for o in (opts or []):
            if isinstance(o, dict):
                label = str(o.get("label") or o.get("value") or "").strip()
                value = str(o.get("value") or o.get("label") or "").strip()
                desc = str(o.get("description") or "").strip()
            else:
                label = value = str(o or "").strip()
                desc = ""
            if not label:
                continue
            clean_opts.append({"label": label[:ASK_OPT_LIMIT],
                               "value": (value or label)[:ASK_OPT_LIMIT],
                               "description": desc[:ASK_OPT_LIMIT]})
        if opts and not clean_opts:
            # 选项全是空串 → 退化成自由填写，别给用户渲染一排空按钮
            qtype, clean_opts = "text", []

        qs.append({"id": qid[:80], "question": text[:ASK_TEXT_LIMIT],
                   "type": qtype, "options": clean_opts})
    return qs, notes, None


def _h_ask_user(a, ctx=None):
    """ask_user：把问题交给用户。

    这个 handler 本身只做校验与格式化 —— 真正的「终止本轮 loop」由
    run_agent_loop 的排他分支负责（见 EXCLUSIVE_TOOLS）。职责分开，
    否则 break 逻辑散在两处，单元测试也没法单独验 handler。
    """
    qs, notes, err = _normalize_ask(a.get("questions"))
    if err:
        return OUTCOME_FAILURE, err
    kinds = {"single_select": "单选", "multi_select": "多选", "text": "自由填写"}
    lines = ["（问题已提交给用户，本轮工具循环终止，等用户回答后再继续）"]
    for q in qs:
        lines.append(f"- [{kinds.get(q['type'], q['type'])}] {q['question']}")
        for o in q["options"] or []:
            lines.append(f"    · {o['label']}")
    if notes:
        lines.append("格式修正：" + "；".join(notes))
    return OUTCOME_SUCCESS, "\n".join(lines)


# ---------- 技能按需加载 ----------
def _skills_root():
    """技能根目录。每次现算，方便单元测试改模块全局。"""
    return Path(SKILLS_DIR)


def _guard_skill_path(rel, want="file"):
    """技能目录专用路径守卫，入参是相对 SKILLS_DIR 的路径。

    为什么不复用 guard_path：后者的 root 是 FS_ROOT，可被 CYRENE_FS_ROOT
    改写。本机跑验证脚本时沙箱把 FS_ROOT 指到 tempfile 目录，技能目录立刻
    整体「越界」，工具假失败。技能是只读资源，守卫职责只有两条：
    必须落在 SKILLS_DIR 内、必须真是文件/目录。
    """
    root = _skills_root()
    if not isinstance(rel, str) or not rel.strip():
        raise PathGuardError("(路径为空)")
    rel = rel.strip().replace("\x00", "")
    p = Path(rel)
    if p.is_absolute():
        # 绝对路径一律剥成相对技能根，免得模型给个 /etc/passwd 绕过守卫
        try:
            p = p.relative_to(root)
        except ValueError:
            p = Path(p.name)
    cand = root / p
    try:
        ap = Path(os.path.abspath(str(cand)))
    except (OSError, ValueError) as e:
        raise PathGuardError(f"(路径无法解析: {e})")
    if ap != root and root not in ap.parents:
        raise PathGuardError(f"(路径越界：只允许访问技能目录 {root} 之内，拒绝 {ap})")
    try:
        rp = Path(os.path.realpath(str(ap)))
        real_root = Path(os.path.realpath(str(root)))
    except (OSError, ValueError) as e:
        raise PathGuardError(f"(路径无法解析: {e})")
    if rp != real_root and real_root not in rp.parents:
        raise PathGuardError(f"(路径经符号链接指向技能目录之外，已拒绝: {rp})")
    if want == "file" and not ap.is_file():
        raise PathGuardError(f"(文件不存在或不是普通文件: {ap})")
    if want == "dir" and not ap.is_dir():
        raise PathGuardError(f"(目录不存在: {ap})")
    return ap


def _find_skill_dir(skill_id):
    """按 id 定位技能目录。返回 (Path|None, 错误串|None)。

    目录名与 manifest.id 可能不一致（discover_skills 里 id 取自 manifest），
    所以两条路都试：先按目录名，再扫一遍 manifest.id 匹配。
    """
    sid = str(skill_id or "").strip()
    if not sid:
        return None, "(缺少 skill_id)"
    if "/" in sid or "\\" in sid or sid in (".", ".."):
        return None, f"(skill_id 非法: {sid}。只接受技能 id，不接受路径)"
    root = _skills_root()
    direct = root / sid
    if direct.is_dir():
        return direct, None
    for info in get_skills():
        if info.get("id") == sid:
            d = root / sid
            if d.is_dir():
                return d, None
    known = [i["id"] for i in get_skills()]
    return None, (f"(没有这个技能: {sid}。"
                  + (f"当前可用：{', '.join(known)}" if known else "技能目录为空")
                  + ")")


def _list_skill_refs(d):
    """列出技能 references/ 下的文件名（只列文件，不递归）。"""
    rd = d / "references"
    if not rd.is_dir():
        return []
    try:
        return sorted(x.name for x in rd.iterdir() if x.is_file())
    except OSError:
        return []


def _h_invoke_skill(a):
    """invoke_skill：读技能正文（SKILL.md 去掉 front matter 的部分）。

    对齐桌面端「技能正文不进上下文，需要时才 load」—— 手机上上下文预算更紧，
    9 个技能全量注入会白烧几千 token，按需读才是正解。
    """
    d, err = _find_skill_dir(a.get("skill_id"))
    if err:
        return OUTCOME_FAILURE, err
    sk = d / "SKILL.md"
    refs = _list_skill_refs(d)
    if not sk.is_file():
        return OUTCOME_FAILURE, (
            f"(技能 {d.name} 没有 SKILL.md"
            + (f"，但有附件：{', '.join(refs)}" if refs else "") + ")")
    try:
        text = sk.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return OUTCOME_FAILURE, f"(读取技能失败: {e})"

    meta, body = parse_front_matter(text)
    body = body.strip()
    if not body:
        return OUTCOME_FAILURE, f"(技能 {d.name} 的 SKILL.md 正文为空)"

    head = [f"=== Skill: {meta.get('name') or d.name} ==="]
    if meta.get("description"):
        head.append(str(meta["description"]))
    if meta.get("version"):
        head.append(f"version: {meta['version']}")
    head.append(f"正文 {len(body)} 字" + (f" · 附件 {len(refs)} 个" if refs else ""))
    if refs:
        head.append("可用附件（用 read_skill_reference 读）：" + ", ".join(refs))
    out = "\n".join(head) + "\n\n" + body
    max_chars = _int_arg(a.get("max_chars"), 0, 0, 200000)
    if max_chars and len(out) > max_chars:
        dropped = len(out) - max_chars
        out = out[:max_chars] + f"\n…（按 max_chars 截断，丢弃 {dropped} 字）"
    return OUTCOME_SUCCESS, out


def _h_read_skill_reference(a):
    """read_skill_reference：读技能 references/ 下的附件，按行返回带行号。"""
    d, err = _find_skill_dir(a.get("skill_id"))
    if err:
        return OUTCOME_FAILURE, err
    ref = str(a.get("ref") or "").strip()
    if not ref:
        refs = _list_skill_refs(d)
        return OUTCOME_FAILURE, (
            "(缺少 ref 文件名。"
            + (f"该技能可用附件：{', '.join(refs)}" if refs else "该技能没有附件")
            + ")")
    try:
        # 守卫按「技能目录名/references/ref」整体校验，ref 里的 ../ 被 abspath 拦下
        p = _guard_skill_path(str(Path(d.name) / "references" / ref), want="file")
    except PathGuardError as e:
        return OUTCOME_FAILURE, str(e)
    try:
        size = p.stat().st_size
        if size > FS_MAX_FILE_BYTES:
            return OUTCOME_FAILURE, f"(附件太大 {size} 字节，上限 {FS_MAX_FILE_BYTES})"
        data = p.read_bytes()
    except OSError as e:
        return OUTCOME_FAILURE, f"(读取失败: {e})"
    if b"\x00" in data[:8192]:
        return OUTCOME_FAILURE, "(这是二进制文件，不能当文本读)"

    text, enc_note = _decode_bytes(data)
    lines = text.split("\n")
    total = len(lines)
    # offset **不夹到 total**：夹了的话模型传 offset=999 会静默拿到最后一行，
    # 它会以为读到了第 999 行附近的内容 —— 静默错位比报错危险得多。
    # 与 _h_read_file 同一套语义（越界直接 failure，让模型改 offset 重读）。
    offset = _int_arg(a.get("offset"), 1, 1, 10 ** 9)
    limit = _int_arg(a.get("limit"), 400, 1, 20000)
    if offset > total:
        return OUTCOME_FAILURE, (
            f"(offset={offset} 超出范围：附件共 {total} 行。"
            f"要续读请用 offset<={total})")
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return OUTCOME_FAILURE, f"(offset={offset} 超出范围，文件共 {total} 行)"
    width = len(str(offset - 1 + len(chunk)))
    body = "\n".join(f"{offset + i:>{width}} | {ln}" for i, ln in enumerate(chunk))
    left = total - (offset - 1 + len(chunk))
    head = (f"附件: {d.name}/references/{p.name}  共 {total} 行"
            f"  本次 {offset}-{offset + len(chunk) - 1}"
            + (f"  （还有 {left} 行，可加大 offset 续读）" if left > 0 else "")
            + (enc_note or ""))
    return OUTCOME_SUCCESS, head + "\n" + body


# ---------- 后台 shell 任务 ----------
def _shell_job_dir():
    """后台任务日志目录（FS_ROOT/shell_jobs），不存在则创建。"""
    d = guard_path(SHELL_JOB_DIRNAME, want="any")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _job_snapshot(j, tail=True):
    """把 job 记录整成给模型看的 dict。tail=True 时带上日志尾部与累计字节数。"""
    out = {
        "job_id": j["id"], "cmd": j["cmd"], "status": j["status"],
        "startedAt": j["startedAt"], "exitCode": j.get("exitCode"),
        "logFile": str(j["log"]),
    }
    out["elapsedSec"] = int(time.time() - j["startedAt"])
    try:
        with open(j["log"], "rb") as f:
            f.seek(0, os.SEEK_END)
            sz = f.tell()
            f.seek(max(0, sz - SHELL_JOB_TAIL))
            raw = f.read()
        txt, _ = _decode_bytes(raw)
        out["totalBytes"] = sz
        out["tail"] = txt
        out["tailTruncated"] = sz > SHELL_JOB_TAIL
    except OSError as e:
        out["totalBytes"] = 0
        out["tail"] = f"(读日志失败: {e})"
        out["tailTruncated"] = False
    return out


def _job_kill(j):
    """杀整个进程组并回收僵尸。绝不把异常抛给上层。

    杀完必须回收（proc.wait / waitpid）：SIGKILL 后子进程会短暂变僵尸，
    父进程不 wait 的话它一直占着进程表项，os.kill(pid,0) 仍报「活着」，
    shell_job 的 stopped 状态就验不出来（真机 B153 抓到的）。
    SIGKILL 已发，wait 会立刻返回，不会阻塞。
    """
    pgid, pid, proc = j.get("pgid"), j.get("pid"), j.get("proc")
    killed = False
    if os.name == "posix" and pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed = True
        except (OSError, ProcessLookupError, ValueError):
            pass
    if not killed and pid:
        try:
            os.kill(pid, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
            killed = True
        except (OSError, ProcessLookupError, ValueError):
            pass
    # 收尸：有 Popen 句柄用 wait（内部 reap），否则退回 waitpid
    if proc is not None:
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
    elif pid and os.name == "posix":
        try:
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError, ValueError):
            pass
    return killed


def _job_reap(j):
    """刷新 job 状态。必须在持 SHELL_JOBS_LOCK 时调用。

    用 Popen.poll() 探活 —— 它内部是 waitpid(pid, WNOHANG)，既回收僵尸又拿退出码。
    为什么不能用 os.kill(pid, 0)：后台子进程退出后若父进程不 wait，它就变僵尸，
    而 kill(pid,0) 对僵尸**仍返回成功**（进程表项还在），于是状态永远卡在
    running，shell_job status 彻底失效。这是真机 B153/B155 抓出来的 bug。
    poll() 返回 None=还在跑；返回 int=已退出的退出码，顺手回收了僵尸。
    """
    if j["status"] != "running":
        return j
    proc = j.get("proc")
    rc = None
    if proc is not None:
        try:
            rc = proc.poll()
        except (OSError, ValueError, subprocess.SubprocessError):
            rc = None
    else:
        # 没有句柄（例如服务重启后从别处恢复的记录）：退回 kill(pid,0) 探活，
        # 判不出僵尸，但至少不会因为拿不到 proc 就崩。
        pid = j.get("pid")
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (OSError, ProcessLookupError, ValueError):
                alive = False
        if alive:
            if time.time() - j["startedAt"] > SHELL_JOB_MAX_SEC:
                _job_kill(j)
                j["status"] = "timed_out"
            return j
        j["status"] = "exited"
        return j

    if rc is None:
        # 仍在运行：超过最长时间才强杀（防后台任务无限跑）
        if time.time() - j["startedAt"] > SHELL_JOB_MAX_SEC:
            _job_kill(j)
            j["status"] = "timed_out"
        return j
    # 已退出：记下退出码，状态给 exited（被 stop 显式改过的不动）
    j["exitCode"] = rc
    if j["status"] == "running":
        j["status"] = "exited"
    return j



def _jobs_prune():
    """登记簿瘦身：只留最近 SHELL_JOB_KEEP 条。必须在持锁时调用。"""
    if len(SHELL_JOBS) <= SHELL_JOB_KEEP:
        return
    ordered = sorted(SHELL_JOBS.values(), key=lambda x: x["startedAt"])
    for j in ordered[:len(SHELL_JOBS) - SHELL_JOB_KEEP]:
        SHELL_JOBS.pop(j["id"], None)


def _shell_timeout(a):
    """本次 shell 调用的超时秒数。

    timeout_ms>0 时用它（夹到 SHELL_TIMEOUT_MS_RANGE），否则回落 agent 的 stepTimeout。
    给模型一个 per-call 覆盖口子：删大目录、编译这类「就是慢」的命令不该被
    30s 默认值砍掉，而它也不该去改全局 stepTimeout（那会影响所有工具）。
    """
    ms = _int_arg(a.get("timeout_ms"), 0, 0, SHELL_TIMEOUT_MS_RANGE[1])
    if ms > 0:
        lo, hi = SHELL_TIMEOUT_MS_RANGE
        return max(lo, min(hi, ms)) / 1000.0
    return float(agent_cfg("stepTimeout"))


def _shell_bg_log_path(jid):
    """后台任务日志文件路径。目录不可用时回落系统临时目录，绝不因日志写不了就不跑命令。"""
    try:
        return _shell_job_dir() / f"{jid}.log"
    except PathGuardError:
        return Path(tempfile.gettempdir()) / f"cyrene_{jid}.log"


def _h_shell(a):
    """shell：执行一条命令。前台阻塞拿输出，或后台起任务立刻返回 jobId。

    进程组隔离与 run_tool 的 cmd 分支同款（start_new_session + killpg）：
    超时只 kill `sh -c` 那层的话，它 fork 出来的孙子进程会变孤儿继续跑，
    上轮 termux-api 探测实测攒了 12 行残留。
    """
    cmd_str = str(a.get("cmd") or "").strip()
    if not cmd_str:
        return OUTCOME_FAILURE, "(缺少 cmd)"
    bg = _bool_arg(a.get("run_in_background"), False)
    jid = "job-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    log_path = _shell_bg_log_path(jid)

    popen_kw = {}
    if os.name == "posix":
        popen_kw["start_new_session"] = True   # setsid，子进程自成一个进程组

    # ---------- 后台模式 ----------
    if bg:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = open(log_path, "wb")
        except OSError as e:
            return OUTCOME_FAILURE, f"(后台日志文件打不开: {e})"
        try:
            p = subprocess.Popen(["sh", "-c", cmd_str], stdout=logf,
                                 stderr=subprocess.STDOUT, **popen_kw)
        except (OSError, ValueError) as e:
            try:
                logf.close()
            except OSError:
                pass
            return OUTCOME_FAILURE, f"(后台任务启动失败: {type(e).__name__}: {e})"
        # stdout/stderr 已重定向到文件，子进程不再持有这个句柄，父进程可以关。
        # 不关就是每次后台调用漏一个 fd —— ThreadingHTTPServer 下攒够快就 EMFILE。
        try:
            logf.close()
        except OSError:
            pass
        pgid = None
        if os.name == "posix":
            try:
                pgid = os.getpgid(p.pid)
            except (OSError, ProcessLookupError, ValueError):
                pgid = p.pid
        with SHELL_JOBS_LOCK:
            SHELL_JOBS[jid] = {
                "id": jid, "pid": p.pid, "pgid": pgid, "cmd": cmd_str[:500],
                "log": log_path, "startedAt": time.time(),
                "status": "running", "exitCode": None,
                # 持有 Popen 句柄：_job_reap 用 poll() 回收僵尸并拿退出码，
                # _job_kill 杀完用它 wait 收尸。stdout/stderr 已重定向到日志文件
                # 并在父进程侧 close，这个对象本身不再占额外 fd。
                "proc": p,
            }
            _jobs_prune()
        return OUTCOME_SUCCESS, json.dumps({
            "jobId": jid, "logFile": str(log_path), "pid": p.pid,
            "note": "已在后台启动，本调用立即返回。"
                    "用 shell_job(action=status) 查进度或 (action=stop) 终止；"
                    "totalBytes 两次查询有增量说明任务真在跑",
        }, ensure_ascii=False)

    # ---------- 前台模式 ----------
    timeout = _shell_timeout(a)
    p = None
    try:
        p = subprocess.Popen(["sh", "-c", cmd_str], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, **popen_kw)
        try:
            stdout, stderr = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _shell_kill_tree(p)
            try:
                p.communicate(timeout=5)
            except Exception:
                pass
            # unknown 而非 failure：命令可能已经部分生效，不能诱导模型重放
            return OUTCOME_UNKNOWN, (
                f"(执行超过 {timeout:.1f}s 被中止，已杀整个进程组；"
                f"命令可能已部分生效，请勿盲目重试。"
                f"长任务请改用 run_in_background=true)")
        out = (stdout or "").strip()
        err_out = (stderr or "").strip()
        if p.returncode == 0:
            return OUTCOME_SUCCESS, _truncate_output(out) if out else "(无输出)"
        detail = out or err_out or "(无输出)"
        if out and err_out:
            detail = out + "\n[stderr] " + err_out
        return OUTCOME_FAILURE, f"(退出码 {p.returncode}) {_truncate_output(detail)}"
    except FileNotFoundError:
        return OUTCOME_FAILURE, "(sh 不存在：这不是一个 POSIX shell 环境)"
    except Exception as e:
        if p is not None and p.poll() is None:
            _shell_kill_tree(p)
        return OUTCOME_FAILURE, f"(失败: {type(e).__name__}: {e})"


def _shell_kill_tree(p):
    """杀掉整个进程组。杀不掉也认了，但不能把异常抛到上层。"""
    if os.name != "posix":
        try:
            p.kill()
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _h_shell_job(a):
    """shell_job：查状态或终止一个后台任务。"""
    action = str(a.get("action") or "status").strip().lower()
    if action not in ("status", "stop"):
        return OUTCOME_FAILURE, f"(action 只能是 status 或 stop，收到 {action!r})"
    jid = str(a.get("job_id") or "").strip()
    if not jid:
        return OUTCOME_FAILURE, "(缺少 job_id。它是 run_in_background 返回的那个 id)"

    with SHELL_JOBS_LOCK:
        j = SHELL_JOBS.get(jid)
        if j is None:
            known = sorted(SHELL_JOBS.keys())
            return OUTCOME_FAILURE, (
                f"(没有这个后台任务: {jid}。"
                + (f"当前登记在册：{', '.join(known)}" if known
                   else "登记簿是空的——服务重启过，或任务从未创建") + ")")
        _job_reap(j)
        snap = _job_snapshot(j)
        if action == "stop":
            was_running = snap["status"] == "running"
            if was_running:
                _job_kill(j)
                j["status"] = "stopped"
                snap["status"] = "stopped"
            _jobs_prune()
            return OUTCOME_SUCCESS, json.dumps({
                "action": "stop", "job": snap,
                "note": ("（已按进程组终止，孙子进程一并清掉）" if was_running
                         else f"（任务早已是 {snap['status']}，本次没杀任何东西）"),
            }, ensure_ascii=False)
        _jobs_prune()
    return OUTCOME_SUCCESS, json.dumps({"action": "status", "job": snap},
                                       ensure_ascii=False)


# ========== 工具定义 ==========
# 每个工具的字段：
#   desc      给模型看的功能说明，同时用于设置面板卡片
#   icon      设置面板卡片图标
#   readonly  只读工具标记，供「只留只读工具」批量操作使用
#   risk      副作用等级，供前端审批与设置面板过滤（阶段2 的文件/网络工具会扩这个集合）
#               safe    = 纯读取，无副作用
#               device  = 改手机状态（发声/发光/震动/写剪贴板/拍照）
#               shell   = 执行任意命令
#               fs-write / network = 阶段2a/2b 引入
#   cmd       命令行模板。占位符 {参数名} 由 params 的键解析，{ts} 是当前时间戳
#   handler   与 cmd 二选一。填 Python 函数名（字符串），该函数收 typed args、
#             返回 (outcome, result_text)。文件/网络这类工具用它 —— 不需要 fork
#             子进程，也就能顺带做路径守卫、编码处理和结构化错误。
#             函数名 → 函数对象的映射见 TOOL_HANDLERS。
#   params    JSON Schema 的 properties 部分。每项可带：
#               type / desc / enum / default / required / minimum / maximum
#             这份定义驱动原生 Function Calling 的
#             tools[].function.parameters（见 tool_schema）。
#             模型按 schema 给命名参数，执行层直接吃 args_dict，只有一份路径。
#             （旧 [TOOL] 文本协议的位置参数映射已在阶段3 删除，随之删掉了
#              只服务于它的 whole 标记。）
TOOLS = {
    "battery":       {"desc": "查手机电量、充电状态与电池温度", "icon": "🔋",
                      "readonly": True, "risk": "safe",
                      "cmd": ["termux-battery-status"],
                      "params": {}},
    "camera":        {"desc": "拍照并保存到相册 DCIM", "icon": "📷",
                      "readonly": False, "risk": "device",
                      "cmd": ["termux-camera-photo", "-c", "0", "/sdcard/DCIM/cyrene_{ts}.jpg"],
                      "params": {}},
    "tts":           {"desc": "让手机朗读一段文字", "icon": "🔊",
                      "readonly": False, "risk": "device",
                      "cmd": ["termux-tts-speak", "{text}"],
                      "params": {"text": {"type": "string", "required": True,
                                          "desc": "要朗读的文字"}}},
    "notify":        {"desc": "发一条系统通知", "icon": "🔔",
                      "readonly": False, "risk": "device",
                      "cmd": ["termux-notification", "-t", "{title}", "-c", "{text}"],
                      "params": {"text": {"type": "string", "required": True,
                                          "desc": "通知正文"},
                                 "title": {"type": "string", "default": "昔涟",
                                           "desc": "通知标题，缺省「昔涟」"}}},
    "vibrate":       {"desc": "让手机震动一下", "icon": "📳",
                      "readonly": False, "risk": "device",
                      "cmd": ["termux-vibrate"],
                      "params": {}},
    "torch":         {"desc": "开关手电筒", "icon": "🔦",
                      "readonly": False, "risk": "device",
                      "cmd": ["termux-torch", "{on_off}"],
                      "params": {"on_off": {"type": "string", "required": True,
                                            "enum": ["on", "off"],
                                            "desc": "on=打开，off=关闭"}}},
    "location":      {"desc": "查 GPS 定位（需系统定位总开关已打开）", "icon": "📍",
                      "readonly": True, "risk": "safe",
                      "cmd": ["termux-location", "-p", "{provider}"],
                      "params": {"provider": {"type": "string", "default": "network",
                                              "enum": ["network", "gps", "passive"],
                                              "desc": "定位来源。室内用 network，"
                                                      "室外精确用 gps"}}},
    "clipboard_set": {"desc": "把文字写进剪贴板", "icon": "📋",
                      "readonly": False, "risk": "device",
                      "cmd": ["termux-clipboard-set", "{text}"],
                      "params": {"text": {"type": "string", "required": True,
                                          "desc": "要写入剪贴板的内容"}}},
    "clipboard_get": {"desc": "读取剪贴板当前内容", "icon": "📄",
                      "readonly": True, "risk": "safe",
                      "cmd": ["termux-clipboard-get"],
                      "params": {}},
    "wifi_info":     {"desc": "查当前 WiFi 连接信息（SSID、IP、信号强度）", "icon": "📶",
                      "readonly": True, "risk": "safe",
                      "cmd": ["termux-wifi-connectioninfo"],
                      "params": {}},
    "brightness":    {"desc": "调节屏幕亮度", "icon": "🔆",
                      "readonly": False, "risk": "device",
                      "cmd": ["termux-brightness", "{value}"],
                      "params": {"value": {"type": "integer", "required": True,
                                           "minimum": 0, "maximum": 255,
                                           "desc": "亮度值 0-255"}}},
    # 阶段 2c 升级：从 cmd 型转 handler 型。
    # 原因：cmd 型只能吃「一个固定超时（stepTimeout）+ 一次性拿全部输出」，
    # 无法表达 per-call 超时覆盖，也无法「起个后台任务立刻返回」。
    # 转 handler 后仍 fork 子进程（shell 的本质），只是控制权回到 Python 手里：
    # timeout_ms 覆盖、输出走 _truncate_output、run_in_background 登记到 SHELL_JOBS。
    "shell":         {"desc": "在 Termux 里执行一条 shell 命令并返回输出。"
                             "长时间任务可传 run_in_background=true 立刻返回 jobId，"
                             "之后用 shell_job 查状态或终止",
                      "icon": "💻", "readonly": False, "risk": "shell",
                      "handler": "_h_shell",
                      "params": {
                          "cmd": {"type": "string", "required": True,
                                  "desc": "要执行的完整 shell 命令"},
                          # ⚠ timeout_ms / run_in_background 只能经原生 Function
                          # Calling 指定（旧的 [TOOL] 文本协议无法表达多参数，
                          # 已在阶段3 删除，不再是限制）。
                          "timeout_ms": {"type": "integer", "default": 0,
                                         "minimum": 0, "maximum": SHELL_TIMEOUT_MS_RANGE[1],
                                         "desc": "本次调用的超时上限（毫秒）。"
                                                 "0=用系统默认 stepTimeout。"
                                                 f"最大 {SHELL_TIMEOUT_MS_RANGE[1]}。"
                                                 "设置后不再做无输出检测"},
                          "run_in_background": {"type": "boolean", "default": False,
                                                "desc": "true=后台执行，立刻返回 jobId "
                                                        "与日志路径，不阻塞本轮；"
                                                        "之后用 shell_job 查询或终止"}}},

    # ---------- 文件工具（阶段 2a）----------
    # 这六个是 **handler 型**：没有 cmd，Python 函数直接在服务进程里跑，
    # 不 fork 子进程。因此不受 termux-api 广播熔断器牵连，也没有孤儿进程风险。
    # 全部经 guard_path 限制在家目录内，写操作另有保护名单。
    "read_file":     {"desc": "读取本地文本文件内容（带行号，可用 offset/limit 分段读大文件）",
                      "icon": "📖", "readonly": True, "risk": "safe",
                      "handler": "_h_read_file",
                      "params": {
                          "path": {"type": "string", "required": True,
                                   "desc": f"文件路径。绝对路径或相对家目录（{FS_ROOT_DEFAULT}）的路径"},
                          "offset": {"type": "integer", "default": 0, "minimum": 0,
                                     "desc": "从第几行开始读（0 = 第一行）"},
                          "limit": {"type": "integer", "default": FS_READ_DEFAULT_LINES,
                                    "minimum": 1, "maximum": FS_READ_MAX_LINES,
                                    "desc": f"最多读多少行（默认 {FS_READ_DEFAULT_LINES}）"}}},
    "write_file":    {"desc": "创建新文件或整体覆写已有文件（覆写前自动备份 .bak）",
                      "icon": "✍️", "readonly": False, "risk": "fs-write",
                      "handler": "_h_write_file",
                      "params": {
                          "path": {"type": "string", "required": True,
                                   "desc": "目标文件路径，父目录不存在会自动创建"},
                          "content": {"type": "string", "required": True,
                                      "allowEmpty": True,
                                      "desc": "要写入的完整内容（传空串即清空文件）"}}},
    "edit_file":     {"desc": "对文件做精确文本替换：把 old_string 换成 new_string。"
                            "匹配到多处时必须显式传 replace_all，否则报错不改",
                      "icon": "✂️", "readonly": False, "risk": "fs-write",
                      "handler": "_h_edit_file",
                      "params": {
                          "path": {"type": "string", "required": True,
                                   "desc": "要修改的文件路径（必须已存在）"},
                          "old_string": {"type": "string", "required": True,
                                         "desc": "要被替换的原文。必须与文件内容逐字符一致（含缩进和换行），"
                                                 "建议先用 read_file 核对；不要带行号"},
                          "new_string": {"type": "string", "required": True,
                                         "allowEmpty": True,
                                         "desc": "替换成的新内容（传空串即删除 old_string 这段）"},
                          "replace_all": {"type": "boolean", "default": False,
                                          "desc": "true = 替换所有匹配处；false = 只允许唯一匹配"}}},
    "glob_files":    {"desc": "按通配模式查找文件（如 *.md、config*.json），返回相对路径列表",
                      "icon": "🔎", "readonly": True, "risk": "safe",
                      "handler": "_h_glob_files",
                      "params": {
                          "pattern": {"type": "string", "required": True,
                                      "desc": f"通配模式，支持 * 和 ?（如 *.py）。最多返回 {FS_GLOB_LIMIT} 条"},
                          "root": {"type": "string", "default": "",
                                   "desc": "从哪个目录开始找，缺省为家目录"}}},
    "grep_files":    {"desc": "用正则在文件内容里搜索，返回匹配行（带文件名和行号）",
                      "icon": "🧲", "readonly": True, "risk": "safe",
                      "handler": "_h_grep_files",
                      "params": {
                          "pattern": {"type": "string", "required": True,
                                      "desc": "正则表达式（Python re 语法）"},
                          "root": {"type": "string", "default": "",
                                   "desc": "搜索起点目录，缺省为家目录"},
                          "glob": {"type": "string", "default": "",
                                   "desc": "只搜匹配这个通配的文件（如 *.py），缺省搜全部"},
                          "context": {"type": "integer", "default": 0, "minimum": 0,
                                      "maximum": 10, "desc": "每个匹配前后各显示几行上下文"},
                          "files_with_matches": {"type": "boolean", "default": False,
                                                 "desc": "true = 只列文件名不列内容"}}},
    "list_dir":      {"desc": "列出目录内容（子目录在前、文件在后，带文件大小）",
                      "icon": "🗂️", "readonly": True, "risk": "safe",
                      "handler": "_h_list_dir",
                      "params": {
                          "path": {"type": "string", "default": "",
                                   "desc": "目录路径，缺省为家目录"}}},

    # ---------- 联网工具（阶段 2b）----------
    # 同样是 handler 型：进程内跑 urllib，不 fork 子进程。三个都标 risk="network"，
    # 供前端按类过滤；网络读操作失败返回 failure（可安全重试），只有超时才可能
    # 是 unknown（见 run_agent_loop 的 halted 判定：network 不在 safe 白名单）。
    # 安全红线：_net_open 只放行 http/https，file:// 等本地协议一律拒（防 SSRF）。
    "web_search":    {"desc": "联网搜索，返回标题/链接/摘要列表（多搜索引擎自动降级）",
                      "icon": "🌐", "readonly": True, "risk": "network",
                      "handler": "_h_web_search",
                      "params": {
                          "query": {"type": "string", "required": True,
                                    "desc": "搜索关键词"},
                          "max_results": {"type": "integer", "default": NET_DEFAULT_RESULTS,
                                          "minimum": 1, "maximum": NET_MAX_RESULTS,
                                          "desc": f"最多返回几条（默认 {NET_DEFAULT_RESULTS}）"}}},
    "fetch_url":     {"desc": "抓取一个网页正文，HTML 自动转 Markdown 纯文本返回（超长会截断标注）",
                      "icon": "📄", "readonly": True, "risk": "network",
                      "handler": "_h_fetch_url",
                      "params": {
                          "url": {"type": "string", "required": True,
                                  "desc": "要抓取的完整网址（http/https）"},
                          "max_chars": {"type": "integer", "default": 0, "minimum": 0,
                                        "maximum": 200000,
                                        "desc": "正文最多保留多少字（0=用系统输出上限）"}}},
    "download_file": {"desc": "从 URL 下载文件（图片/压缩包/PDF 等）存到 downloads 目录，返回落地路径",
                      "icon": "⬇️", "readonly": False, "risk": "network",
                      "handler": "_h_download_file",
                      "params": {
                          "url": {"type": "string", "required": True,
                                  "desc": "要下载的完整 URL（http/https）"},
                          "filename": {"type": "string", "default": "",
                                       "desc": "保存的文件名（可选，缺省从 URL 或响应头推断）"}}},

    # ---------- Agent 元工具（阶段 2c）----------
    # 这五个操作的是 **Agent 自身状态**，不碰硬件/文件内容/网络。
    # needs_ctx=True 的工具，run_tool 会把 {"sid": ...} 作为第二个参数传给 handler
    # （update_todo 要落 session.todos，必须知道是哪个会话；dispatch_tools 用线程池
    #  并发，threading.local / contextvars 都传不进子线程，只能显式传参）。
    # ask_user 是**排他工具**：见 EXCLUSIVE_TOOLS，命中即终止本轮 loop，
    # 同轮其余调用一律合成 not_executed 闭合槽位，绝不边问边做。
    "update_todo":   {"desc": "更新本次任务的工作笔记（Todo）。整表替换：每次传入完整的"
                             "任务清单，含已完成、进行中和待办的全部条目",
                      "icon": "📝", "readonly": False, "risk": "safe",
                      "handler": "_h_update_todo", "needs_ctx": True,
                      "params": {
                          "todos": {"type": "array", "required": True,
                                    "desc": "完整的新待办列表（替换旧列表）。每项形如 "
                                            "{\"id\":\"唯一标识\",\"content\":\"任务描述\","
                                            "\"status\":\"pending|in_progress|completed|cancelled\","
                                            "\"activeForm\":\"正在进行时的描述，可选\"}。"
                                            "同一时刻最多一条 in_progress；"
                                            f"最多 {META_TODO_MAX} 条"}},
                     },
    "ask_user":      {"desc": "需要用户做决定、补充信息或选择方向时提问。**排他工具**："
                             "调用后本轮立即结束，同批次其他工具都不会执行。"
                             "能自己查到的事不要用它，也不要拿它出测试题",
                      "icon": "❓", "readonly": True, "risk": "safe",
                      "handler": "_h_ask_user", "needs_ctx": True,
                      "params": {
                          "questions": {"type": "array", "required": True,
                                        "desc": f"1-{ASK_MAX_QUESTIONS} 个问题。每项形如 "
                                                "{\"id\":\"问题标识\",\"question\":\"问题文本\","
                                                "\"type\":\"single_select|multi_select|text\","
                                                "\"options\":[{\"label\":\"显示文本\","
                                                "\"value\":\"选项值\"}]}。"
                                                "type=text 时不要传 options"}},
                     },
    "invoke_skill":  {"desc": "读取一个技能的完整执行指令（SKILL.md 正文）。"
                             "技能清单在系统提示里，需要详情时才调它，不要凭猜执行",
                      "icon": "📚", "readonly": True, "risk": "safe",
                      "handler": "_h_invoke_skill",
                      "params": {
                          "skill_id": {"type": "string", "required": True,
                                       "desc": "技能 id（见系统提示的技能清单）"},
                          "max_chars": {"type": "integer", "default": 0, "minimum": 0,
                                        "maximum": 200000,
                                        "desc": "最多返回多少字（0=用系统输出上限）"}},
                     },
    "read_skill_reference": {
                      "desc": "读取技能 references/ 目录下的附件内容。"
                             "只有 invoke_skill 返回的清单里点名的附件才能读",
                      "icon": "📎", "readonly": True, "risk": "safe",
                      "handler": "_h_read_skill_reference",
                      "params": {
                          "skill_id": {"type": "string", "required": True,
                                       "desc": "技能 id"},
                          "ref": {"type": "string", "required": True,
                                  "desc": "references 下的文件名（须在该技能的附件清单里）"},
                          "offset": {"type": "integer", "default": 1, "minimum": 1,
                                     "desc": "从第几行开始读（1 基）"},
                          "limit": {"type": "integer", "default": 400, "minimum": 1,
                                    "maximum": 20000, "desc": "最多读多少行"}},
                     },
    "shell_job":     {"desc": "查询或终止 shell 后台任务（run_in_background=true 启动的）。"
                             "status 返回运行状态、退出码、累计输出字节数与日志尾部",
                      "icon": "🛰️", "readonly": False, "risk": "shell",
                      "handler": "_h_shell_job",
                      "params": {
                          "job_id": {"type": "string", "required": True,
                                     "desc": "shell 后台任务返回的 jobId"},
                          "action": {"type": "string", "default": "status",
                                     "enum": ["status", "stop"],
                                     "desc": "status=查状态（默认）；stop=终止任务"}},
                     },
}
# 只读工具清单。
#
# ⚠ 插件体系接入后这是**动态量**：插件启用会往 TOOLS 里加工具，停用会摘掉。
# 模块级 list 在 import 时算一次就定死了，插件工具永远进不来。
# 保留这个名字是为了兼容既有引用（含验证脚本的静态断言），但它只是
# 「内置只读工具」的快照 —— 需要完整清单时调 readonly_tools()。
READONLY_TOOLS = [k for k, v in TOOLS.items() if v.get("readonly")]


def readonly_tools():
    """当前全部只读工具（内置 + 已启用插件）。需要实时值就用这个。

    每次现算而不缓存：TOOLS 规模是几十个，一次列表推导的成本远低于
    「缓存失效时机没对上」带来的 bug。插件启停是低频操作，但 TOOLS
    的变更点分散在 _register_tools / _unregister_tools / 工具开关三处，
    任何一处漏了缓存清理就会读到脏数据。
    """
    return [k for k, v in TOOLS.items() if v.get("readonly")]

# handler 型工具的 name → 函数映射。TOOLS 里存函数名字符串而不是函数对象，
# 这样 TOOLS 仍然是纯数据（可 JSON 序列化、可被验证脚本静态检查）。
TOOL_HANDLERS = {
    "_h_read_file": _h_read_file,
    "_h_write_file": _h_write_file,
    "_h_edit_file": _h_edit_file,
    "_h_glob_files": _h_glob_files,
    "_h_grep_files": _h_grep_files,
    "_h_list_dir": _h_list_dir,
    "_h_web_search": _h_web_search,
    "_h_fetch_url": _h_fetch_url,
    "_h_download_file": _h_download_file,
    # 阶段 2c 元工具
    "_h_shell": _h_shell,
    "_h_update_todo": _h_update_todo,
    "_h_ask_user": _h_ask_user,
    "_h_invoke_skill": _h_invoke_skill,
    "_h_read_skill_reference": _h_read_skill_reference,
    "_h_shell_job": _h_shell_job,
}

# tool_schema 要剥掉的自定义字段（它们不是 JSON Schema 关键字，
# 留在 properties 里会让严格校验的端点直接 400）
_NON_SCHEMA_KEYS = ("required", "desc", "allowEmpty")


def tool_schema(name, t=None):
    """生成原生 Function Calling 的 tools[] 条目。

    params 直接就是 JSON Schema 的 properties，只需补外壳与 required 列表。
    `required` 标记写在每个 param 内部（比并列一份列表更难写漏），这里抽出来。
    """
    t = t if t is not None else TOOLS.get(name, {})
    props = {}
    required = []
    for pname, spec in (t.get("params") or {}).items():
        p = {k: v for k, v in spec.items() if k not in _NON_SCHEMA_KEYS}
        p["type"] = p.get("type", "string")
        if p["type"] == "array" and "items" not in p:
            # JSON Schema 里 array 缺 items 是合法的，但严格校验的端点会 400。
            # 元工具的 todos/questions 是对象数组，结构已在 description 里写清，
            # 这里补一个宽松的 object items 兜底，不做深度校验（模型给歪了
            # 由 normalize_todos/_normalize_ask 拦，报错信息比 schema 校验有用）。
            p["items"] = {"type": "object"}
        if spec.get("desc"):
            p["description"] = spec["desc"]
        props[pname] = p
        if spec.get("required"):
            required.append(pname)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": t.get("desc", name),
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def tool_schemas(names=None):
    """按给定顺序（缺省=TOOLS 声明顺序）生成 tools 数组。"""
    keys = names if names is not None else list(TOOLS.keys())
    return [tool_schema(k) for k in keys if k in TOOLS]


def resolve_args(name, args_dict):
    """把模型给的参数补全成「每个占位符都有值」的字符串字典。

    - 缺失的可选参数回落到 params 里的 default，没有 default 则给空串
    - 一律转成 str（命令行只吃字符串）；integer 型去掉浮点尾巴
    - 模型没声明的多余键直接丢弃，不让它注入未知占位符
    """
    params = TOOLS.get(name, {}).get("params") or {}
    src = args_dict if isinstance(args_dict, dict) else {}
    out = {}
    for pname, spec in params.items():
        v = src.get(pname)
        if v is None or (isinstance(v, str) and not v.strip()):
            v = spec.get("default", "")
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, float) and spec.get("type") == "integer":
            v = str(int(v))
        elif not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        out[pname] = v
    return out


def resolve_typed(name, args_dict):
    """handler 型工具的参数归一：按 params 声明的类型给出**原生类型**值。

    与 resolve_args 的区别：resolve_args 服务于命令行拼接，一律转 str；
    handler 直接在 Python 里跑，需要 int 就是 int、bool 就是 bool，
    否则 `_int_arg("5")` 这种到处兜底会把逻辑搅浑。

    - integer：转 int，非法值回落 default 再夹 minimum/maximum
    - boolean：认 bool 与 "true"/"1"/"yes"/"on" 等常见写法
    - 其余：转 str（None 回落 default）
    - 未声明的键一律丢弃，不让模型塞东西进来
    """
    params = TOOLS.get(name, {}).get("params") or {}
    src = args_dict if isinstance(args_dict, dict) else {}
    out = {}
    for pname, spec in params.items():
        ptype = spec.get("type", "string")
        default = spec.get("default")
        v = src.get(pname)

        if ptype == "integer":
            if v is None or (isinstance(v, str) and not v.strip()):
                v = 0 if default is None else default
            n = _int_arg(v, int(default) if default is not None else 0,
                         -(10 ** 9), 10 ** 9)
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if isinstance(lo, (int, float)) and n < lo:
                n = int(lo)
            if isinstance(hi, (int, float)) and n > hi:
                n = int(hi)
            out[pname] = n
            continue

        if ptype == "boolean":
            out[pname] = _bool_arg(v, bool(default))
            continue

        if ptype in ("array", "object"):
            # 结构化参数（update_todo.todos / ask_user.questions）必须**保留原生类型**。
            # 落到下面的 str 分支会被 json.dumps 成字符串，handler 拿到的就是
            # '[{"id":...}]' 而不是 list，normalize_todos 直接判「不是数组」拒绝。
            if isinstance(v, (list, dict)):
                out[pname] = v
                continue
            if isinstance(v, str) and v.strip():
                # 模型偶尔把数组序列化成字符串塞进来（text 协议下必然如此）。
                # 解一次 JSON 救回来，解不动就原样交给 handler 报错，别静默吞。
                try:
                    parsed = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    parsed = v
                out[pname] = parsed
                continue
            out[pname] = [] if ptype == "array" else {}
            continue

        if v is None:
            v = default if default is not None else ""
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        elif not isinstance(v, str):
            v = str(v)
        out[pname] = v
    return out

# ========== 四大对话模式 ==========
# 对齐桌面端 Chat / Work / Code / Learn。每种模式复用 prompts/ 下已存在的
# {mode}_identity.md / {mode}_system.md / {mode}_remark.md，缺文件则留空。
#   tools=False：Chat 模式按桌面端语义「不暴露、不调用、不执行任何工具」，
#                系统提示里不注入工具块，run_tool 对 chat 会话也一律拒绝。
MODES = {
    "chat":  {"label": "聊天", "icon": "💬", "tools": False,
              "desc": "自然陪伴对话，不调用任何工具"},
    "work":  {"label": "工作", "icon": "🛠️", "tools": True,
              "desc": "通用任务，可串联手机工具完成事情"},
    "code":  {"label": "代码", "icon": "💻", "tools": True,
              "desc": "编程 / 文件 / 命令，任务正确性优先"},
    "learn": {"label": "学习", "icon": "📚", "tools": True,
              "desc": "陪伴理解材料、整理笔记、生成练习"},
}
DEFAULT_MODE = "chat"


def normalize_mode(v):
    return v if v in MODES else DEFAULT_MODE


# 思考强度档位。off 不带 reasoning 参数；其余映射到 reasoning_effort。
# 手机端走通用 OpenAI 兼容端点，档位名沿用桌面端 ReasoningEffort 语义。
REASONING_EFFORTS = ("low", "medium", "high")


def normalize_reasoning(raw):
    r = raw if isinstance(raw, dict) else {}
    effort = r.get("effort")
    return {
        "enabled": _as_bool(r.get("enabled"), False),
        "effort": effort if effort in REASONING_EFFORTS else "medium",
        "showInChat": _as_bool(r.get("showInChat"), True),
    }


# ========== Agent Loop 配置 ==========
# 工具调用协议。阶段0 实测（_FC_PROBE_REPORT.md）：中转站 your-relay.example.com +
# gpt-4o-mini 五项全 PASS —— tools 字段被接受、模型返回 message.tool_calls、
# role=tool 回灌被正确消费、一轮可返回多个 tool_calls（并行）、SSE 下
# delta.tool_calls 同样可用。因此两档都以原生 FC 为唯一调用协议。
#   auto = 带 tools 请求；端点明确拒收 tools 时，本次 loop 剩余轮次不再带 tools，
#          退化成纯文本对话（保可用性，不硬撞 400）
#   fc   = 始终带 tools，即使端点拒收也继续尝试（换模型后验证 FC 能力时用）
# 旧的 "text" 档（[TOOL] 文本协议）已在阶段3 删除。旧配置里残留的 "text"
# 会被 normalize_agent 夹回 "auto"；loop 侧也按「非 fc 即 auto」处理，不会炸。
TOOL_PROTOCOLS = ("auto", "fc")

# 各字段合法区间 (lo, hi, fallback)。越界一律夹回，绝不让前端传个 0 或
# 99999 把 loop 变成死循环 / 把中转站打爆。
AGENT_RANGES = {
    "maxTurns":       (1, 12, 4),        # 单次请求最多工具轮数
    "maxParallel":    (1, 4, 4),         # 一轮内并发执行的工具上限
    "throttleMs":     (0, 10000, 3500),  # 相邻 LLM 调用最小间隔（治 40 rpm 限流）
    "stepTimeout":    (5, 120, 30),      # 单个工具执行超时（秒）
    "totalTimeout":   (30, 600, 180),    # 整条 loop 总预算（秒），超时=第四种终止状态
    "maxOutputChars": (500, 32000, 8000),  # 单工具输出上限（旧代码硬编码 800，太小）
}


def normalize_agent(raw):
    """把任意输入夹成合法 agent 配置。缺失项回落默认，非法值夹回区间。"""
    a = raw if isinstance(raw, dict) else {}
    out = {}
    for k, (lo, hi, fb) in AGENT_RANGES.items():
        out[k] = int(_clamp(a.get(k), lo, hi, fb))
    proto = a.get("protocol")
    out["protocol"] = proto if proto in TOOL_PROTOCOLS else "auto"
    out["showSteps"] = _as_bool(a.get("showSteps"), True)
    return out


def agent_cfg(key, fb=None):
    """读单个 agent 配置项。SETTINGS 未加载或键缺失时回落 AGENT_RANGES 默认值。"""
    if fb is None:
        rng = AGENT_RANGES.get(key)
        fb = rng[2] if rng else None
    try:
        return SETTINGS.get("agent", {}).get(key, fb)
    except (NameError, AttributeError):
        # NameError: 模块导入期 SETTINGS 尚未赋值（本机单元测试会走到这里）
        # AttributeError: SETTINGS 不是 dict
        return fb


# 前端 settle 轮询预算的余量（秒）：留给 loop 收尾落库 + 中止交代那一次 LLM 调用。
SETTLE_BUDGET_MARGIN = 15


def settle_budget():
    """前端「等后端这一轮落库」该等多久（秒）。随 GET /chat 一起回传。

    最坏情形：loop 在轮前 elapsed_over() 检查**通过后**才发出一次请求，那次请求
    要等满自己的 request_timeout 才返回，此时 totalTimeout 也基本到了 —— 两段相加
    再加落库余量，才是前端该等的上限。

    这个值必须由后端算：前端曾经硬编码 90s，而 totalTimeout 默认 180s，长轮次
    （开思考链时单次请求显著变慢，最容易撞上）会在后端还在跑的时候就放弃轮询、
    定格在旧画面上，提问卡选项与进度条依旧「消失」，用户仍要手动刷新。
    """
    try:
        total = int(agent_cfg("totalTimeout"))
    except (TypeError, ValueError):
        total = AGENT_RANGES["totalTimeout"][2]
    try:
        req = int(SETTINGS.get("model", {}).get("request_timeout", 120))
    except (NameError, AttributeError, TypeError, ValueError):
        req = 120
    return max(30, min(900, total + req + SETTLE_BUDGET_MARGIN))


# 插件 id / 工具 id 的合法形状。用作路径片段与命名空间前缀前必须校验，
# 否则 "../../evil" 这类 id 能穿到文件系统上。
#
# ⚠ 必须定义在这里（配置区）而不是插件段：SETTINGS = load_settings() 在模块
#   导入期就执行，会调到 normalize_plugins；而插件段在文件更靠后的位置，
#   那时 PLUGIN_ID_RE 还没定义，import 直接 NameError。
PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# GET /settings 回传时，插件密钥的值一律替换成这个占位符（见 _public_settings）。
# 前端把它原样回存时，deep_merge_settings 要认得出来并**跳过**，
# 否则真密钥会被字符串 "<set>" 顶掉 —— 表现为「插件突然全部失效」，
# 而 .config.json 里看不出任何异常，很难查。
PLUGIN_SECRET_MASK = "<set>"


def normalize_plugins(raw):
    """归一化插件配置。

    与其它 section 不同的地方：registry / secrets 是**用户数据**，不是偏好设置，
    所以这里只过滤非法 id，绝不做「回落默认值」——回落就是清空。

    normalize_settings 是白名单式的，这个函数不保留就等于每次启动、
    每次前端保存设置都把已安装台账与密钥抹掉。
    """
    p = raw if isinstance(raw, dict) else {}
    out = {"enabled": _as_bool(p.get("enabled"), True),
           # securityScan：插件导入时的静态高危扫描开关。必须在这里显式登记 ——
           # 下面 out_reg 是白名单式的，漏一个键就会在每次启动 / 每次前端保存
           # 设置时被静默抹回默认值，用户在面板上关掉又「自己打开」。
           "securityScan": _as_bool(p.get("securityScan"), True)}

    reg = p.get("registry") if isinstance(p.get("registry"), dict) else {}
    out_reg = {}
    for pid, entry in reg.items():
        # id 会拼进文件路径，形状不对的一律丢掉（防穿越）
        if not isinstance(pid, str) or not PLUGIN_ID_RE.match(pid):
            continue
        if not isinstance(entry, dict):
            continue
        out_reg[pid] = {
            "version": _as_str(entry.get("version"), "")[:40],
            "enabled": _as_bool(entry.get("enabled"), False),
            "source": _as_str(entry.get("source"), "")[:20],
            "sha256": _as_str(entry.get("sha256"), "")[:64],
            "installedAt": _as_str(entry.get("installedAt"), "")[:40],
            # lastError 可能带路径与堆栈，留长一点便于面板排查，但仍设上限
            "lastError": _as_str(entry.get("lastError"), "")[:2000],
            # risky：安装时静态扫出的高危调用（只存 pattern 名，不存文件清单——
            # 文件清单只在安装响应里给用户看一次，长期留在 .config.json 里没意义
            # 还会撑大文件）。不登记在这里就会被下次 normalize 静默抹掉，
            # 面板从此再也显示不出黄标（上面 out_reg 是白名单式的）。
            "risky": [str(x)[:40] for x in (entry.get("risky") or [])
                      if isinstance(x, str)][:12],
        }
    out["registry"] = out_reg

    sec = p.get("secrets") if isinstance(p.get("secrets"), dict) else {}
    out_sec = {}
    for pid, kv in sec.items():
        if not isinstance(pid, str) or not PLUGIN_ID_RE.match(pid):
            continue
        if not isinstance(kv, dict):
            continue
        # 密钥值原样保留（不做长度截断 —— 截断会悄悄废掉一个 key，
        # 用户看到的是「插件突然不工作了」而不是「配置被截断了」）
        out_sec[pid] = {str(k): v for k, v in kv.items() if isinstance(k, str)}
    out["secrets"] = out_sec
    return out


def plugins_cfg(key, fb=None):
    """读单个插件配置项。SETTINGS 未加载时回落默认值（与 agent_cfg 同构）。"""
    try:
        return SETTINGS.get("plugins", {}).get(key, fb)
    except (NameError, AttributeError):
        return fb


def plugin_registry():
    """已安装插件台账（内存视图）。调用方不应原地修改，改完要 save。"""
    try:
        reg = SETTINGS.get("plugins", {}).get("registry")
        return reg if isinstance(reg, dict) else {}
    except (NameError, AttributeError):
        return {}


def plugin_secrets(pid):
    """某插件的私有密钥字典。按 id 命名空间隔离：插件读不到别人的 key。"""
    try:
        sec = SETTINGS.get("plugins", {}).get("secrets", {})
        kv = sec.get(pid)
        return kv if isinstance(kv, dict) else {}
    except (NameError, AttributeError):
        return {}



# ========== 配置 schema ==========
# 分层结构。每层都有归一化函数，任何非法值都夹回合法范围而不是让前端崩。
TYPO_RANGES = {
    "fontSize":      (12, 20, 15),
    "lineHeight":    (1.2, 2.2, 1.85),
    "letterSpacing": (0, 2, 0.8),
    "fontWeight":    (300, 700, 400),
}

DEFAULT_SETTINGS = {
    "appearance": {
        "theme": "charcoal-pink",
        "messageTypography": {"fontSize": 15, "lineHeight": 1.85,
                              "letterSpacing": 0.8, "fontWeight": 400},
        "mobileMessageSegmentation": "off",
        "markdown": True,
        "highlight": True,
    },
    "model": {
        "api_base": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "top_p": 1.0,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "max_tokens": 2000,
        "request_timeout": 120,
        "max_history": 30,
    },
    "tools": {},      # {tool_id: bool}，缺省视为 True
    "skills": {},     # {skill_id: bool}，缺省视为 manifest.defaultEnabled
    "tts": {"autoSpeak": False, "rate": 1.0, "pitch": 1.0, "language": ""},
    "server": {"web_port": 28443, "bind_host": "0.0.0.0", "tool_timeout": 30},
    # 新会话的默认模式；每个会话可单独切换并记忆在 session.mode
    "chat": {"defaultMode": DEFAULT_MODE},
    # 思考链（reasoning）：对齐桌面端 ReasoningControl
    "reasoning": {"enabled": False, "effort": "medium", "showInChat": True},
    # Agent Loop：多轮工具循环的预算与协议。字段含义见 AGENT_RANGES 注释。
    # 默认值对齐桌面端「保守」取向，但 maxParallel=4 是用户明确要求全开的
    # （阶段0 探测2 已证端点一轮能返回多个 tool_calls）。
    "agent": {
        "maxTurns": 4,
        "maxParallel": 4,
        "throttleMs": 3500,
        "stepTimeout": 30,
        "totalTimeout": 180,
        "maxOutputChars": 8000,
        "protocol": "auto",
        "showSteps": True,
    },
    # 插件体系。三个子键都必须在 normalize_settings 里显式保留 ——
    # normalize_settings 是白名单式（未列出的键一律丢弃），漏一个就会
    # 在每次启动 / 每次前端保存设置时**静默清空**用户数据。
    "plugins": {
        "enabled": True,     # 总开关：关掉则不加载任何插件（回退路径）
        "securityScan": True,  # 插件安全检测：导入时静态扫高危调用（黄标提示，不阻断）
        "registry": {},      # {id: {version, enabled, source, sha256, installedAt, lastError}}
        "secrets": {},       # {id: {key: value}}，按插件命名空间隔离
    },
}


def _clamp(v, lo, hi, fb):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return fb
    if n != n:            # NaN
        return fb
    return min(hi, max(lo, n))


def _as_bool(v, fb=True):
    return v if isinstance(v, bool) else fb


def _as_str(v, fb=""):
    return v if isinstance(v, str) else fb


def normalize_typography(v):
    """对齐桌面端 normalizeMessageTypography：逐项回落默认值再夹回合法范围。"""
    inp = v if isinstance(v, dict) else {}
    out = {}
    for k, (lo, hi, fb) in TYPO_RANGES.items():
        n = _clamp(inp.get(k), lo, hi, fb)
        out[k] = int(n) if k in ("fontSize", "fontWeight") else round(n, 2)
    return out


def normalize_settings(raw):
    """把任意输入夹成合法结构。缺失项回落默认值，多余项丢弃。"""
    src = raw if isinstance(raw, dict) else {}
    out = {}

    ap = src.get("appearance") if isinstance(src.get("appearance"), dict) else {}
    d_ap = DEFAULT_SETTINGS["appearance"]
    theme = ap.get("theme")
    out["appearance"] = {
        "theme": theme if theme in ("charcoal-pink", "pearl-white") else d_ap["theme"],
        "messageTypography": normalize_typography(ap.get("messageTypography")),
        "mobileMessageSegmentation": (
            ap.get("mobileMessageSegmentation")
            if ap.get("mobileMessageSegmentation") in ("off", "on") else "off"),
        "markdown": _as_bool(ap.get("markdown"), True),
        "highlight": _as_bool(ap.get("highlight"), True),
    }

    mo = src.get("model") if isinstance(src.get("model"), dict) else {}
    d_mo = DEFAULT_SETTINGS["model"]
    out["model"] = {
        "api_base": _as_str(mo.get("api_base"), d_mo["api_base"]).rstrip("/") or d_mo["api_base"],
        "api_key": _as_str(mo.get("api_key"), ""),
        "model": _as_str(mo.get("model"), d_mo["model"]),
        "temperature": round(_clamp(mo.get("temperature"), 0, 2, d_mo["temperature"]), 3),
        "top_p": round(_clamp(mo.get("top_p"), 0.05, 1, d_mo["top_p"]), 3),
        "frequency_penalty": round(_clamp(mo.get("frequency_penalty"), -2, 2, 0), 3),
        "presence_penalty": round(_clamp(mo.get("presence_penalty"), -2, 2, 0), 3),
        "max_tokens": int(_clamp(mo.get("max_tokens"), 256, 8192, d_mo["max_tokens"])),
        "request_timeout": int(_clamp(mo.get("request_timeout"), 15, 300, d_mo["request_timeout"])),
        "max_history": int(_clamp(mo.get("max_history"), 4, 100, d_mo["max_history"])),
    }

    out["tools"] = {k: _as_bool(v, True) for k, v in (src.get("tools") or {}).items()
                    if isinstance(src.get("tools"), dict) and k in TOOLS} \
        if isinstance(src.get("tools"), dict) else {}
    out["skills"] = {k: _as_bool(v, True) for k, v in src.get("skills").items()
                     if isinstance(k, str)} if isinstance(src.get("skills"), dict) else {}

    tt = src.get("tts") if isinstance(src.get("tts"), dict) else {}
    out["tts"] = {
        "autoSpeak": _as_bool(tt.get("autoSpeak"), False),
        "rate": round(_clamp(tt.get("rate"), 0.5, 2.0, 1.0), 2),
        "pitch": round(_clamp(tt.get("pitch"), 0.5, 2.0, 1.0), 2),
        "language": _as_str(tt.get("language"), "")[:20],
    }

    sv = src.get("server") if isinstance(src.get("server"), dict) else {}
    d_sv = DEFAULT_SETTINGS["server"]
    host = _as_str(sv.get("bind_host"), d_sv["bind_host"]).strip()
    out["server"] = {
        "web_port": int(_clamp(sv.get("web_port"), 1, 65535, d_sv["web_port"])),
        "bind_host": host if host else d_sv["bind_host"],
        "tool_timeout": int(_clamp(sv.get("tool_timeout"), 5, 120, d_sv["tool_timeout"])),
    }

    ch = src.get("chat") if isinstance(src.get("chat"), dict) else {}
    out["chat"] = {"defaultMode": normalize_mode(ch.get("defaultMode"))}

    out["reasoning"] = normalize_reasoning(src.get("reasoning"))
    out["agent"] = normalize_agent(src.get("agent"))
    out["plugins"] = normalize_plugins(src.get("plugins"))
    return out


def deep_merge_settings(base, patch, trust_plugins=False):
    """局部 patch 合并进完整设置。只接受 schema 里存在的键，其余丢弃。

    trust_plugins —— plugins 段的 registry/secrets 要不要当权威数据全盘接收。
      · False（默认，HTTP patch 路径）：前端拿到的是**脱敏后**的配置
        （secrets 值全是 "<set>" 占位符，registry 里的 lastError 等也可能被裁剪），
        原样回写会污染真数据，所以只放行 enabled 开关与 secrets 的逐键真实值。
      · True（load_settings 路径）：磁盘上的 .config.json 是唯一权威源，
        registry 必须原样读进来 —— 否则冷启动时 plugin_boot_async 看到空台账，
        报「无已启用插件」，用户装的插件永远起不来。
        （这个区分是 P1-7 真机实测逼出来的：只按 False 一种语义处理，
         启动路径的 registry 会被整个丢掉。）
    """
    if not isinstance(patch, dict):
        return base
    out = json.loads(json.dumps(base))
    for section, val in patch.items():
        if section not in DEFAULT_SETTINGS or not isinstance(val, dict):
            continue
        if section in ("tools", "skills"):
            # 开关字典：只合并已知 id
            known = TOOLS if section == "tools" else None
            for k, v in val.items():
                if known is not None and k not in known:
                    continue
                if isinstance(v, bool):
                    out[section][k] = v
            continue
        if section == "plugins":
            # plugins 段不能走下面的「整段替换」通用分支：registry / secrets 是
            # 用户数据，前端改任何无关开关（主题、超时…）都会 POST /settings，
            # 整段覆盖会让插件台账与密钥当场蒸发。
            cur = out.setdefault("plugins", normalize_plugins(None))
            if isinstance(val.get("enabled"), bool):
                cur["enabled"] = val["enabled"]
            # securityScan 与 enabled 同类：普通偏好开关，逐键放行；
            # registry / secrets 依旧不被 HTTP patch 触碰。
            if isinstance(val.get("securityScan"), bool):
                cur["securityScan"] = val["securityScan"]
            if trust_plugins:
                # 启动路径：磁盘是权威源，整段交给 normalize_plugins 清洗后收下。
                # 它只过滤非法 id，不会回落默认值（回落就是清空）。
                out["plugins"] = normalize_plugins(val)
                continue
            patch_sec = val.get("secrets")
            if isinstance(patch_sec, dict):
                live = cur.setdefault("secrets", {})
                for pid, kv in patch_sec.items():
                    if not isinstance(pid, str) or not PLUGIN_ID_RE.match(pid):
                        continue
                    if not isinstance(kv, dict):
                        continue
                    dst = live.setdefault(pid, {})
                    for k, v in kv.items():
                        if not isinstance(k, str):
                            continue
                        # 占位符 = 「前端没改这个值」，保持原样，绝不写入
                        if isinstance(v, str) and v == PLUGIN_SECRET_MASK:
                            continue
                        # 空串表示「清掉这个密钥」（面板上的删除操作）
                        if isinstance(v, str) and not v.strip():
                            dst.pop(k, None)
                            continue
                        dst[k] = v
            continue
        if section == "appearance" and isinstance(val.get("messageTypography"), dict):
            cur = out["appearance"].get("messageTypography", {})
            cur.update(val["messageTypography"])
            val = dict(val)
            val["messageTypography"] = cur
        for k, v in val.items():
            if k in DEFAULT_SETTINGS[section]:
                out[section][k] = v
    return normalize_settings(out)


# v7 及之前的 .config.json 是扁平结构，必须迁移，否则用户现有配置全丢
LEGACY_MODEL_KEYS = ("api_base", "api_key", "model", "max_history", "request_timeout",
                     "temperature", "top_p", "frequency_penalty", "presence_penalty",
                     "max_tokens")
LEGACY_SERVER_KEYS = ("web_port", "bind_host", "tool_timeout")


def migrate_legacy_cfg(raw):
    """扁平 .config.json → 分层结构。已是分层则原样返回。"""
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("model"), dict) or isinstance(raw.get("appearance"), dict):
        return raw
    out, model, server = {}, {}, {}
    for k in LEGACY_MODEL_KEYS:
        if k in raw:
            model[k] = raw[k]
    for k in LEGACY_SERVER_KEYS:
        if k in raw:
            server[k] = raw[k]
    if model:
        out["model"] = model
    if server:
        out["server"] = server
    known = set(LEGACY_MODEL_KEYS) | set(LEGACY_SERVER_KEYS)
    extra = {k: v for k, v in raw.items() if k not in known}
    if extra:
        out["_legacy_extra"] = extra   # 未知键原样留存，不静默丢弃
    return out


def load_settings():
    raw = {}
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            try:
                bak = CONFIG_FILE.with_name(f".config.json.corrupt-{int(time.time())}")
                os.replace(CONFIG_FILE, bak)
                print(f"⚠ .config.json 解析失败，已保留副本: {bak.name}")
            except OSError:
                pass
            raw = {}
    migrated = migrate_legacy_cfg(raw)
    if migrated.get("_legacy_extra"):
        print(f"  迁移提示: 保留了 {len(migrated['_legacy_extra'])} 个旧版未知配置键")
    # trust_plugins=True：磁盘上的 .config.json 是插件台账的唯一权威源。
    # 走默认的 False 会让 registry 在这里被丢掉，冷启动时 plugin_boot_async
    # 读到空台账 → 报「无已启用插件」→ 用户装的插件永远起不来。
    s = normalize_settings(
        deep_merge_settings(DEFAULT_SETTINGS, migrated, trust_plugins=True))
    # 工具/技能默认值：schema 里没写的按默认启用
    for tid in TOOLS:
        s["tools"].setdefault(tid, True)
    return s


def save_settings_to_disk():
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
        return True
    except OSError as e:
        print(f"⚠ 配置保存失败: {e}")
        return False


SETTINGS = load_settings()
SETTINGS_LOCK = threading.Lock()

# ========== 技能发现 ==========
FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.DOTALL)


def parse_front_matter(text):
    """极简 YAML front matter 解析。只需 name/description/version/autoInject/modes，
    不引第三方 yaml 依赖（Termux 上多一个包就多一分装不上的风险）。"""
    m = FM_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    meta = {}
    cur_list_key = None
    for line in fm_text.split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        lst = re.match(r'^\s*-\s+(.*)$', line)
        if lst and cur_list_key:
            meta_val = lst.group(1).strip().strip('"\'')
            if not isinstance(meta.get(cur_list_key), list):
                meta[cur_list_key] = []
            meta[cur_list_key].append(meta_val)
            continue
        kv = re.match(r'^([A-Za-z_][\w-]*)\s*:\s*(.*)$', line)
        if not kv:
            continue
        k, v = kv.group(1), kv.group(2).strip()
        if v == "":
            cur_list_key = k
            continue
        cur_list_key = None
        v = v.strip('"\'')
        if v.lower() in ("true", "false"):
            meta[k] = v.lower() == "true"
        else:
            try:
                meta[k] = int(v)
            except ValueError:
                meta[k] = v
    return meta, body


def discover_skills():
    """扫 SKILLS_DIR，返回 [{id, name, description, version, autoInject, modes,
    defaultEnabled, exists, body_len}]。目录不存在则返回空列表。"""
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    try:
        entries = sorted(SKILLS_DIR.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.is_dir():
            continue
        sid = d.name
        info = {"id": sid, "name": sid, "description": "", "version": "",
                "autoInject": False, "modes": [], "defaultEnabled": True,
                "body_len": 0, "has_manifest": False}
        mf = d / "manifest.json"
        if mf.is_file():
            try:
                mj = json.loads(mf.read_text(encoding="utf-8"))
                if isinstance(mj, dict):
                    info["has_manifest"] = True
                    info["id"] = _as_str(mj.get("id"), sid)
                    info["version"] = _as_str(mj.get("version"), "")
                    if isinstance(mj.get("defaultEnabled"), bool):
                        info["defaultEnabled"] = mj["defaultEnabled"]
            except Exception:
                pass
        sk = d / "SKILL.md"
        if sk.is_file():
            try:
                text = sk.read_text(encoding="utf-8")
            except OSError:
                text = ""
            meta, body = parse_front_matter(text)
            if isinstance(meta.get("name"), str) and meta["name"]:
                info["name"] = meta["name"]
            if isinstance(meta.get("description"), str):
                info["description"] = meta["description"]
            if isinstance(meta.get("version"), (str, int)) and meta["version"]:
                info["version"] = str(meta["version"])
            if isinstance(meta.get("autoInject"), bool):
                info["autoInject"] = meta["autoInject"]
            if isinstance(meta.get("modes"), list):
                info["modes"] = [str(x) for x in meta["modes"]]
            info["body_len"] = len(body.strip())
            info["_body"] = body.strip()
        else:
            info["_body"] = ""
        out.append(info)
    return out


SKILL_CACHE = {"ts": 0.0, "items": []}
SKILL_CACHE_LOCK = threading.Lock()


def get_skills(force=False):
    """技能目录带 5 秒缓存，避免每次 /skills 请求都全盘扫描。"""
    with SKILL_CACHE_LOCK:
        now = time.time()
        if not force and SKILL_CACHE["items"] and now - SKILL_CACHE["ts"] < 5:
            return SKILL_CACHE["items"]
    items = discover_skills()
    with SKILL_CACHE_LOCK:
        SKILL_CACHE["ts"] = time.time()
        SKILL_CACHE["items"] = items
    return items


def skill_enabled(sid, info):
    """技能开关：显式设置优先，否则回落 manifest.defaultEnabled。"""
    v = SETTINGS.get("skills", {}).get(sid)
    if isinstance(v, bool):
        return v
    return bool(info.get("defaultEnabled", True))


# ========== Prompt 组装 ==========
def load_prompt(name):
    p = PROMPTS_DIR / name
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return ""


def enabled_tools():
    """开关生效点 1/2：只有 enabled 的工具才进系统提示。"""
    t = SETTINGS.get("tools", {})
    return {k: v for k, v in TOOLS.items() if t.get(k, True)}


def build_system_prompt(mode=None):
    """按模式组装系统提示。

    - 人格核心 soul + 语气 quotes：所有模式共用（昔涟的身份不因模式改变）
    - identity / system / remark：按 mode 取 prompts/{mode}_*.md，缺文件则空
    - 工具块：只有 MODES[mode].tools=True 且存在已启用工具时才注入；
              chat 模式（tools=False）完全不注入工具，也不告知模型可以调工具
    - tool_usage.md：工具与技能的通用使用纪律，只在 tools=True 的模式注入
    - 技能：autoInject + enabled + (未声明 modes 或当前 mode 命中) 才进清单。
            只注入 id + description，正文由模型按需 invoke_skill 拉取
    """
    mode = normalize_mode(mode)
    meta = MODES[mode]

    soul   = load_prompt("soul.md")
    quotes = load_prompt("canon_quotes_lite.md")
    ident  = load_prompt(f"{mode}_identity.md")
    system = load_prompt(f"{mode}_system.md")
    remark = load_prompt(f"{mode}_remark.md")

    # ---- 工具块 ----
    # can_use_tools = 这个模式真有工具可调（模式允许 + 至少一个工具开着）。
    # 工具块、tool_usage 纪律、技能清单三处共用这一个判断：
    # 没有工具能力却教模型「怎么调工具」，只会催生调不到工具的幻觉。
    tools = enabled_tools() if meta.get("tools") else {}
    can_use_tools = bool(tools)
    if can_use_tools:
        tool_lines = "\n".join(f"- {n}: {t['desc']}" for n, t in tools.items())
        # 只教原生 function calling 一种协议。旧的 [TOOL] 文本协议已在阶段3 删除：
        # 两套指令并存会让模型时而调 tool_calls、时而吐文本标记，行为不可预测。
        howto = """调用方式：直接用 function calling 发起工具调用（请求已附带 tools 定义）。
不要手写 [TOOL] 之类的文本标记，那不会被执行。"""
        tool_block = f"""=== 可用工具 ===
{howto}
{tool_lines}

规则：可以连续多轮调用工具，每轮结果会自动回灌给你，看到结果再决定下一步。
一次可以并行发起多个互不依赖的调用。只能用上面列出的工具名。
拿到足够信息后要主动收尾，直接回答用户，不要无限调用。"""
        # 通用工具纪律（事实优先 / 真实性纪律 / 多轮循环 / ask_user 与 update_todo 用法等）。
        # chat 模式不注入——它没有工具，讲这些只会诱导幻觉调用。
        usage = load_prompt("tool_usage.md")
        if usage.strip():
            tool_block += f"\n\n{usage.strip()}"
    elif meta.get("tools"):
        tool_block = """=== 可用工具 ===
当前所有工具都已在设置里关闭，你无法调用任何工具。
不要尝试 function calling，直接用文字回复。"""
    else:
        # chat 模式：不暴露工具，也不提工具调用机制，避免模型幻觉调用
        tool_block = """=== 当前模式：纯聊天 ===
你正在 Chat 模式下陪伴用户，此模式不提供任何工具。
不要发起 function calling，不要提及自己能操控手机，
直接用文字自然回应。"""

    # ---- 技能清单注入（开关生效点 1/2）----
    # 阶段3 改动：只注入 id + description 的**清单**，不再把 SKILL.md 正文塞进系统提示。
    # 正文由模型判断适用后调 invoke_skill(skill_id) 按需拉取。
    # 旧做法在 learn 模式下光技能正文就吃 7980 字，且多数轮次根本用不上。
    #
    # 守卫：技能要靠 invoke_skill 读，没有工具能力的模式（chat）注入清单
    # 等于教模型调一个它调不到的工具 —— 既存缺陷，本轮一并修掉。
    skill_lines = []
    for info in (get_skills() if can_use_tools else []):
        if not info.get("autoInject"):
            continue
        if not skill_enabled(info["id"], info):
            continue
        declared = info.get("modes") or []
        # 技能声明了 modes 时，只在命中的模式下进清单；未声明=通用，全模式可见
        if declared and mode not in declared:
            continue
        desc = (info.get("description") or "").strip().replace("\n", " ")
        skill_lines.append(f"- {info['id']}: {desc}")
    skill_block = ""
    if skill_lines:
        skill_block = f"""
=== 可用技能（{len(skill_lines)} 个） ===
下面是技能清单，只有 id 和一句话说明，**不含正文**。
判断某个技能适用于当前任务时，先调 invoke_skill(skill_id) 读完整执行指令，再按它做；不要凭这句话猜流程。
{chr(10).join(skill_lines)}
"""

    md_hint = ""
    if SETTINGS.get("appearance", {}).get("markdown", True):
        md_hint = """

=== 排版 ===
网页端已支持 Markdown 渲染（标题、列表、表格、代码块、引用、粗体、任务清单）。
需要结构化表达时可以正常使用，代码请放进带语言标记的围栏代码块。"""

    remark_block = f"\n=== 模式补充 ===\n{remark}\n" if remark.strip() else ""

    mode_title = f"当前处于 {meta['label']}（{mode}）模式：{meta['desc']}。"

    return f"""你是昔涟，正在通过网页和用户文字交流。
{mode_title}

=== 人格核心 ===
{soul}

=== 身份 ===
{ident}

=== 系统规则 ===
{system}
{remark_block}
=== 语气参考 ===
{quotes}

{tool_block}
{skill_block}{md_hint}

用户昵称：（可在设置中自定义）
"""


# 每种模式一份系统提示缓存。切换模式=换一份已构建好的提示，无需重算。
SYSTEM_PROMPTS = {m: build_system_prompt(m) for m in MODES}
PROMPT_LOCK = threading.Lock()


def rebuild_system_prompt():
    """设置变更后重建全部模式的系统提示。持锁避免并发写。"""
    global SYSTEM_PROMPTS
    fresh = {m: build_system_prompt(m) for m in MODES}
    with PROMPT_LOCK:
        SYSTEM_PROMPTS = fresh
    return {m: len(t) for m, t in fresh.items()}


def get_system_prompt(mode=None):
    mode = normalize_mode(mode)
    with PROMPT_LOCK:
        return SYSTEM_PROMPTS.get(mode) or SYSTEM_PROMPTS[DEFAULT_MODE]


# ========== 用量统计 ==========
USAGE_LOCK = threading.Lock()
USAGE = {
    "requests": 0, "promptTokens": 0, "completionTokens": 0, "totalTokens": 0,
    "toolCalls": 0, "errors": 0, "lastError": None,
    "byModel": {}, "startedAt": time.time(),
}


def usage_snapshot():
    with USAGE_LOCK:
        snap = json.loads(json.dumps(USAGE))
    with STORE_LOCK:
        snap["sessions"] = len(SESSIONS)
    return snap


def usage_record(model, usage, error=None):
    with USAGE_LOCK:
        USAGE["requests"] += 1
        if isinstance(usage, dict):
            p = int(usage.get("prompt_tokens") or 0)
            c = int(usage.get("completion_tokens") or 0)
            t = int(usage.get("total_tokens") or (p + c))
            USAGE["promptTokens"] += p
            USAGE["completionTokens"] += c
            USAGE["totalTokens"] += t
            m = USAGE["byModel"].setdefault(model or "?", {
                "requests": 0, "promptTokens": 0, "completionTokens": 0, "totalTokens": 0})
            m["requests"] += 1
            m["promptTokens"] += p
            m["completionTokens"] += c
            m["totalTokens"] += t
        if error:
            USAGE["errors"] += 1
            USAGE["lastError"] = str(error)[:300]


def usage_reset():
    with USAGE_LOCK:
        for k in ("requests", "promptTokens", "completionTokens", "totalTokens",
                  "toolCalls", "errors"):
            USAGE[k] = 0
        USAGE["lastError"] = None
        USAGE["byModel"] = {}
        USAGE["startedAt"] = time.time()


# ========== LLM ==========
class LLMClient:
    def __init__(self, cfg):
        m = cfg.get("model", {})
        self.base = str(m.get("api_base", "")).rstrip("/")
        self.key  = m.get("api_key", "")
        self.model = m.get("model", "")
        self.temperature = m.get("temperature", 0.7)
        self.top_p = m.get("top_p", 1.0)
        self.frequency_penalty = m.get("frequency_penalty", 0.0)
        self.presence_penalty = m.get("presence_penalty", 0.0)
        self.max_tokens = int(m.get("max_tokens", 2000))
        try:
            self.timeout = int(m.get("request_timeout", 120))
        except (TypeError, ValueError):
            self.timeout = 120
        r = cfg.get("reasoning", {})
        self.reasoning_enabled = bool(r.get("enabled"))
        self.reasoning_effort = r.get("effort", "medium")

    def _base_payload(self, messages, max_tokens=None):
        return {
            "model": self.model, "messages": messages,
            "temperature": self.temperature, "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "max_tokens": int(max_tokens) if max_tokens else self.max_tokens,
        }

    def _post(self, payload):
        """发一次请求，返回 (status, data_dict)。status: 200 / HTTP错误码 / None(网络异常)。"""
        url = self.base + "/chat/completions"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return 200, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode(errors="replace")
            except Exception:
                detail = ""
            return e.code, {"_error": detail}
        except Exception as e:
            return None, {"_error": str(e)}

    def _post_stream(self, payload, on_delta=None, cancel=None):
        """流式发一次请求：边收 SSE 边把增量回调出去。

        on_delta(kind, text) 里 kind 是 "reasoning" 或 "content"；
        cancel() 返回 True 时立刻收手（前端点了停止）。
        返回 (status, result)——result 与 chat_ex 同形，失败时 status 为
        错误码或 None、result 里带 _error。供 chat_ex_stream 复用。
        """
        url = self.base + "/chat/completions"
        body = dict(payload)
        body["stream"] = True
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "Authorization": f"Bearer {self.key}"})
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode(errors="replace")
            except Exception:
                detail = ""
            return e.code, {"_error": detail}
        except Exception as e:
            return None, {"_error": str(e)}

        content_parts, reasoning_parts, tc_acc = [], [], {}
        finish_reason = None
        try:
            for raw in resp:
                if cancel is not None and cancel():
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                piece_r = delta.get("reasoning_content") or delta.get("reasoning")
                if piece_r:
                    reasoning_parts.append(piece_r)
                    if on_delta:
                        try:
                            on_delta("reasoning", piece_r)
                        except Exception:
                            pass
                piece_c = delta.get("content")
                if piece_c:
                    content_parts.append(piece_c)
                    if on_delta:
                        try:
                            on_delta("content", piece_c)
                        except Exception:
                            pass
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    slot = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
        finally:
            try:
                resp.close()
            except Exception:
                pass

        reasoning = "".join(reasoning_parts) or None
        if isinstance(reasoning, str) and not reasoning.strip():
            reasoning = None
        tool_calls = []
        for i in sorted(tc_acc):
            slot = tc_acc[i]
            raw_args = slot["args"] or "{}"
            try:
                args = json.loads(raw_args)
            except Exception:
                args = raw_args
            tool_calls.append({"id": slot["id"], "name": slot["name"], "args": args})
        # 回灌用的原始 message：agent loop 靠 message.tool_calls 把 assistant
        # 这次调用写回上下文；缺了它，后面 append 的 role=tool 就没有对应槽位，
        # 端点会直接 400（tool message 前必须先有带 tool_calls 的 assistant）。
        raw_tcs = []
        for i in sorted(tc_acc):
            slot = tc_acc[i]
            raw_tcs.append({"id": slot["id"], "type": "function",
                            "function": {"name": slot["name"],
                                         "arguments": slot["args"] or "{}"}})
        message = {"role": "assistant", "content": "".join(content_parts)}
        if reasoning:
            message["reasoning_content"] = reasoning
        if raw_tcs:
            message["tool_calls"] = raw_tcs
        return 200, {"content": "".join(content_parts),
                     "reasoning": reasoning,
                     "tool_calls": tool_calls,
                     "finish_reason": finish_reason,
                     "error": None,
                     "message": message}

    def chat_ex(self, messages, max_tokens=None, tools=None):
        """完整形态的一次 LLM 调用，返回结构化 dict：

            content       正文（模型决定调工具时通常为空串）
            reasoning     思考链原文，无则 None
            tool_calls    [{"id","name","args"}]，按模型原始顺序；无则 []
            finish_reason 端点给的终止原因（"stop" / "tool_calls" / "length" …）
            error         错误串，成功时 None
            fc_rejected   True = 端点不接受 tools 字段，调用方应降级到文本协议
            length_hit    True = 正文被 max_tokens 截断（要在回复尾部提示，不静默截）
            message       原始 message dict，回灌 assistant 消息时直接用它

        自愈顺序（每一步都只在前一步仍失败时才做）：
          1. 带 reasoning 参数被拒(400/422) → 剥掉 reasoning 重试
          2. 带 tools 被拒(400/422)         → 剥掉 tools 重试并置 fc_rejected
          3. 撞 554（中转站 40 rpm 限流）    → 指数退避 1s/3s/9s，最多重试 2 次
        """
        payload = self._base_payload(messages, max_tokens)
        used_reasoning = False
        if self.reasoning_enabled:
            # 通用 OpenAI 兼容写法；不同厂商字段名不一，一并带上，服务端各取所需
            payload["reasoning_effort"] = self.reasoning_effort
            payload["reasoning"] = {"effort": self.reasoning_effort}
            payload["enable_thinking"] = True
            used_reasoning = True

        used_tools = bool(tools)
        if used_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        status, data = None, {}
        posts = 0
        while True:
            posts += 1
            status, data = self._post(payload)

            # 554 = 中转站限流。退避重试，别把额度彻底打爆
            if status == 554 and posts <= 3:
                time.sleep([1, 3, 9][posts - 1])
                continue

            # 自愈 1：思考参数不被接受
            if used_reasoning and status in (400, 422):
                for k in ("reasoning_effort", "reasoning", "enable_thinking"):
                    payload.pop(k, None)
                used_reasoning = False
                if posts <= 5:
                    continue

            # 自愈 2：tools 字段不被接受 → 降级到文本协议
            if used_tools and status in (400, 422):
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                used_tools = False
                if posts <= 5:
                    continue
            break

        fc_rejected = bool(tools) and not used_tools

        if status != 200:
            msg = f"[API {status}] {data.get('_error', '')[:300]}"
            usage_record(self.model, None, error=msg)
            # content 留空：错误串只能走 error 通道，不能被当正文落库或回灌上下文
            return {"content": "", "reasoning": None, "tool_calls": [],
                    "finish_reason": None, "error": msg,
                    "fc_rejected": fc_rejected, "length_hit": False, "message": {}}

        usage_record(self.model, data.get("usage"))
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            msg = "[错误] 响应结构异常"
            usage_record(self.model, None, error=msg)
            return {"content": "", "reasoning": None, "tool_calls": [],
                    "finish_reason": None, "error": msg,
                    "fc_rejected": fc_rejected, "length_hit": False, "message": {}}

        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning") or None
        if isinstance(reasoning, str) and not reasoning.strip():
            reasoning = None
        return {"content": content,
                "reasoning": reasoning,
                "tool_calls": parse_tool_calls(message),
                "finish_reason": choice.get("finish_reason"),
                "error": None,
                "fc_rejected": fc_rejected,
                "length_hit": choice.get("finish_reason") == "length",
                "message": message}

    def chat_ex_stream(self, messages, max_tokens=None, tools=None,
                       on_delta=None, cancel=None):
        """chat_ex 的流式版本：入参与返回同形，另收两个回调。

        on_delta(kind, text) —— kind 为 "reasoning" / "content"，边收边吐；
        cancel()             —— 返回 True 时立刻收手（前端点了停止）。

        端点不认流式、或流式这一趟失败时，自动回落到整段的 chat_ex，
        功能不会因为「想看到思考过程」而丢。
        """
        payload = self._base_payload(messages, max_tokens)
        if self.reasoning_enabled:
            payload["reasoning_effort"] = self.reasoning_effort
            payload["reasoning"] = {"effort": self.reasoning_effort}
            payload["enable_thinking"] = True
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        status, data = self._post_stream(payload, on_delta=on_delta, cancel=cancel)
        if status == 200:
            out = dict(data)
            out["fc_rejected"] = False
            out["length_hit"] = out.get("finish_reason") == "length"
            return out
        return self.chat_ex(messages, max_tokens=max_tokens, tools=tools)

    def chat(self, messages, max_tokens=None):
        """兼容旧调用方：返回 (content, reasoning, error) 三元组。

        多轮 loop 走 chat_ex()；这个薄封装保留给 TTS 试听、旧验证脚本等
        不需要工具的单次调用。
        """
        r = self.chat_ex(messages, max_tokens=max_tokens)
        return r["content"], r["reasoning"], r["error"]


# ========== 工具执行 ==========
# 工具调用只走原生 Function Calling 一条路。
# 旧的 [TOOL] 文本协议（TOOL_PATTERN / extract_tool / positional_to_named）已在阶段3 删除：
# 端点 FC 能力经 _probe_fc.py 五项实测通过，两套协议并存只会让模型行为不可预测。

# 四态 outcome（对齐桌面端 CyreneHarness 的工具结果语义）：
#   success      命令跑完且退出码 0
#   failure      命令跑了但明确失败（非零退出 / 命令不存在 / 执行前就异常）
#   unknown      结果不确定 —— 目前只有「超时」归这类。命令可能已经生效
#                （手电筒超时了灯可能真亮着），不能当失败告诉模型，否则它
#                会重放这个副作用。非幂等工具命中 unknown 时 loop 应暂停。
#   not_executed 根本没执行 —— 未知工具名 / 开关已关 / 缺必填参数。
#                这一态不会留下任何副作用，可以安全重试。
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_NOT_EXECUTED = "not_executed"

# ---------- termux-api 广播熔断器 ----------
# 实测结论（_probe_poison.py）：termux-* 工具都靠 `am broadcast` 把请求发给
# com.termux.api 接收。一旦某个广播永不返回（最典型：系统定位关着时调
# termux-location），整条广播通道会被卡住几十秒 —— 之后连 battery 这种
# 平时 400ms 秒回的调用都会一路超时。实测 battery 成功率 5/5 → 1/5。
#
# 在多轮 loop 里这是致命的：模型试一次定位失败，接下来每个工具都要白等一个
# stepTimeout，几十秒就烧光了，还极易撞上 totalTimeout。
#
# 对策：超时后开一个熔断窗口，窗口内的广播类工具**立即**返回 not_executed，
# 附上明确说明让模型换路子（而不是逐个去撞死通道）。窗口过后自动恢复。
# 只影响广播类工具，shell / 阶段2 的文件与网络工具不受牵连。
BROADCAST_COOLDOWN = 25.0     # 熔断窗口（秒），实测通道恢复需要十几到几十秒
_broadcast_cooldown_until = 0.0
BROADCAST_LOCK = threading.Lock()


def _is_broadcast_tool(name):
    """是否走 termux-api 广播（会被通道卡死牵连的工具）。"""
    cmd = (TOOLS.get(name) or {}).get("cmd") or []
    return bool(cmd) and str(cmd[0]).startswith("termux-")


def broadcast_open():
    """超时后打开熔断窗口。"""
    global _broadcast_cooldown_until
    with BROADCAST_LOCK:
        _broadcast_cooldown_until = time.time() + BROADCAST_COOLDOWN


def broadcast_cooldown_left():
    """距熔断窗口结束还剩多少秒；<=0 表示通道可用。"""
    with BROADCAST_LOCK:
        return max(0.0, _broadcast_cooldown_until - time.time())


def reset_broadcast_breaker():
    """手动复位（服务启动时、或探测脚本清场时用）。"""
    global _broadcast_cooldown_until
    with BROADCAST_LOCK:
        _broadcast_cooldown_until = 0.0


def _truncate_output(text, limit=None):
    """按 maxOutputChars 截断，并显式标注被丢掉了多少字。

    旧实现是 out[:800] 静默砍尾 —— 模型看不到内容被截断，会当成完整结果用。
    阶段6 会进一步升级成「保头保尾 + 大输出落盘 + read_tool_result 按需回读」。
    """
    limit = int(limit if limit is not None else agent_cfg("maxOutputChars"))
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n…（输出过长，已截断 {dropped} 字）"


def missing_required(name, args_dict):
    """返回缺失的必填参数名列表。FC 下模型偶尔会漏参数，提前拦住比让命令报错清楚。

    allowEmpty=True 的参数例外：空串是**合法值**而不是「没填」。
    write_file 的 content="" 意思是清空文件，edit_file 的 new_string=""
    意思是删掉这段代码 —— 这两种都是真实需求，不能当成漏参数拦掉。
    """
    params = TOOLS.get(name, {}).get("params") or {}
    src = args_dict if isinstance(args_dict, dict) else {}
    out = []
    for pname, spec in params.items():
        if not spec.get("required"):
            continue
        if pname not in src:
            out.append(pname)
            continue
        v = src.get(pname)
        if v is None:
            out.append(pname)
            continue
        if spec.get("allowEmpty"):
            continue
        if isinstance(v, str) and not v.strip():
            out.append(pname)
    return out


# ========== 插件体系（对齐桌面端 Cyrene Plugin API v1）==========
#
# 契约基准：Playa-Cyrene/Cyrene-Agent · packages/plugin-sdk/src/api.ts
# 市场仓库：Playa-Cyrene/Cyrene-Plugins（registry.json + plugins/<id>/ + zips/）
#
# 桌面端插件是 Node 的 index.cjs，手机端宿主是纯 Python —— 靠一个
# **子进程桥**把 PluginContext 契约投影过去，让市场里的插件不改一行就能跑：
#
#   cyrene_web.py ──spawn──> node plugin_host.cjs <id>
#        │                          │
#        │  NDJSON over stdio       │ require("index.cjs")
#        │  （一行一个 JSON-RPC）    │
#        └──────────────────────────┘
#
# 一插件一进程：崩溃隔离（插件 throw 不带走宿主）· 独立超时 · 可单独 kill
# 做停用 · 内存可单独回收。代价是每插件 ~30-50MB Node 常驻，故默认不自动启用。
#
# 协议方向（详见 plugin_host.cjs 头部注释）：
#   Python → Node：plugin.register / tool.execute / prompt.provide /
#                  plugin.dispose / host.event / host.ping
#   Node → Python：storage.* / secrets.* / llm.* / conversations.* /
#                  events.emit / log
#
# ⚠ stdout 是协议通道，只准出现 JSON 行。插件的 console.* 已在 plugin_host.cjs
#   里改道到 stderr，Python 侧读 stderr 转宿主日志。
PLUGINS_DIR      = BASE_DIR / "plugins"
PLUGIN_HOST_JS   = RUNTIME_DIR / "plugin_host.cjs"
PLUGIN_LOCAL_DB  = PLUGINS_DIR / "registry.local.json"   # 本地安装台账（P3 持久化）

PLUGIN_API_VERSION = 1        # 桌面端 CURRENT_PLUGIN_API_VERSION
PLUGIN_READY_TIMEOUT = 20.0   # 等 host.ready 的上限（node 冷启动 + require 插件）
PLUGIN_REGISTER_TIMEOUT = 30.0
PLUGIN_DISPOSE_TIMEOUT = 10.0
PLUGIN_PING_TIMEOUT = 5.0
PLUGIN_RSS_LIMIT_MB = 256     # 单插件常驻内存上限，超限 kill（P3 落地巡检）
PLUGIN_LOG_RING = 200         # 每插件保留的日志行数（面板「查看日志」用）
PLUGIN_LOG_BURST = 60         # 日志速率限制：窗口内最多多少行，防插件刷爆

# 九种稳定错误码（对齐 SDK 的 PLUGIN_HOST_ERROR_CODES）。插件靠
# isPluginHostError(e.code) 分支，所以**不能静默返回 undefined**——
# 那会让插件以为调用成功，走进错误路径。
PLUGIN_HOST_ERROR_CODES = frozenset({
    "E_CAPABILITY_UNAVAILABLE", "E_INVALID_ARGUMENT", "E_NOT_FOUND",
    "E_NOT_OWNER", "E_STORAGE_UNAVAILABLE", "E_SPEECH_INPUT_BUSY",
    "E_NO_ACTIVE_INPUT_TARGET", "E_PLUGIN_STOPPING", "E_INTERNAL",
})

# 插件状态机（见计划 P1 的 stateDiagram）
PLUGIN_STATES = ("not_installed", "installed", "starting", "running",
                 "stopping", "failed", "crashed", "unsupported")

# node 可执行文件。Termux 装在 $PREFIX/bin/node；桌面调试时可能在 PATH 里。
PLUGIN_NODE_CANDIDATES = (
    "/data/data/com.termux/files/usr/bin/node",
    "node",
)

# ---------- 导入（P6）：inbox 目录与体积上限 ----------
#
# inbox 是「离线导入」通道：用户用 adb push 或 Termux 手动把 ZIP 放进来，
# 面板点「扫描 inbox」就能装。手机上不方便走浏览器上传时的兜底。
PLUGIN_INBOX_DIR = BASE_DIR / "plugins_inbox"

# 三道上限，缺一不可（都是防「一个恶意/损坏的包把手机撑爆」）：
#   · ZIP_MAX_MB     —— 压缩包本身。浏览器上传走 base64，体积会涨 4/3，
#                       所以真正卡的是 base64 字符串长度（见 _handle_plugin_import）。
#   · UNPACK_MAX_MB  —— 解压后总字节。**边解边算**，超限立刻中止，
#                       不能等解完再看（那时磁盘已经写满了）。zip bomb 就是冲这道来的。
#   · ZIP_MAX_FILES  —— 成员数。几万个小文件一样能把 inode 和复制时间打爆。
# 市场里最大的 minecraft-bot 是 9.1 MB（含 node_modules），32 MB 留足余量。
PLUGIN_ZIP_MAX_MB = 32
PLUGIN_UNPACK_MAX_MB = 128
PLUGIN_ZIP_MAX_FILES = 4000

# 静态高危扫描的模式。原设计要求「黄标提示、不阻断」——插件与宿主同权限
# （桌面端 README 已明示「审核不构成担保」），扫出来只是让用户装之前看得见。
PLUGIN_RISKY_PATTERNS = (
    ("child_process", "可执行任意系统命令"),
    ("process.binding", "可绕过 JS 层直接摸 node 内部绑定"),
    ("eval(", "动态执行任意代码"),
    ("new Function(", "动态执行任意代码"),
    ("fs.unlink", "可删除文件"),
    ("fs.rm", "可删除文件"),
    ("require('fs')", "可读写宿主文件系统"),
    ('require("fs")', "可读写宿主文件系统"),
)

# ---------- 插件市场（P5）：双源回退 ----------
#
# 主源 Gitee（国内快，registry.json 里的 zip 直链本来就全指向它），
# 回退 GitHub（Gitee 挂了或不同步时）。两个都是 raw 文件直出，无 API 鉴权。
# 所有请求必须走 _net_open（只放行 http/https，防 SSRF）。
PLUGIN_MARKET_SOURCES = (
    ("gitee", "https://gitee.com/playa0/cyrene-plugins/raw/main/"),
    ("github", "https://raw.githubusercontent.com/Playa-Cyrene/Cyrene-Plugins/main/"),
)
PLUGIN_MARKET_TTL = 300         # registry.json 缓存 5 分钟；?refresh=1 可强制刷新
PLUGIN_MARKET_TIMEOUT = 25.0    # 单次拉取超时（秒）
PLUGIN_MARKET_MAX_BYTES = 8 * 1024 * 1024   # registry.json / 详情文件的读取上限

# 插件包解包的临时根。**必须放 BASE_DIR（= ~/cyrene）下**：
# Termux 上 /tmp 不可写、$PREFIX/tmp 的 SELinux 标签与 home/ 不同也会 Permission denied
# （第二十四节实踩过的两个坑），~/cyrene 是反复验证过可写的。
PLUGIN_TMP_ROOT = BASE_DIR / "_plugin_tmp"

# ---------- 导入/安装进度（P5 与 P6 共用） ----------
# 大 ZIP（minecraft-bot 9.1 MB）下载 + 解包可能几十秒，前端要进度条。
# 纯内存表，不持久化：进度只在一次安装过程里有意义，重启丢了无所谓。
PLUGIN_PROGRESS = {}
PLUGIN_PROGRESS_LOCK = threading.Lock()

PROGRESS_STAGES = ("downloading", "verifying", "unpacking", "installing",
                   "done", "failed")


def _progress_set(key, stage, done=0, total=0, message="", info=None):
    with PLUGIN_PROGRESS_LOCK:
        rec = PLUGIN_PROGRESS.get(str(key))
        prev_info = (rec or {}).get("info")
        PLUGIN_PROGRESS[str(key)] = {
            "key": str(key), "stage": stage,
            "done": int(done), "total": int(total),
            "message": str(message or ""), "at": time.time(),
            # info 只在终态（done/failed）写一次，中间阶段传 None 时**保留**
            # 上一次的值 —— 覆盖成 None 会把安装结果丢掉，前端轮询到的就是空。
            "info": info if info is not None else prev_info,
        }


def _progress_get(key):
    with PLUGIN_PROGRESS_LOCK:
        p = dict(PLUGIN_PROGRESS.get(str(key)) or {})
    if not p:
        return {"key": str(key), "stage": "unknown", "done": 0, "total": 0,
                "message": "没有这个安装任务（未开始、已结束并清理、或 key 写错了）",
                "at": 0, "info": None}
    return p


def _progress_clear(key):
    with PLUGIN_PROGRESS_LOCK:
        PLUGIN_PROGRESS.pop(str(key), None)


def _zip_member_is_symlink(info):
    """ZIP 成员的 external_attr 高 16 位存 unix mode，判 S_IFLNK。

    为什么必须显式拒软链：zipfile 默认会把软链成员**当成普通文件**写出来，
    内容就是目标路径字符串，看起来无害。但随后的 install_from_dir →
    _copy_tree_safe 会 rglob 遍历，如果哪一步用了跟随软链的复制，
    一个指向 /data/data/com.termux/files/home 的软链就等于把宿主家目录
    整个抄进插件目录。在解包这一层就掐掉，不给下游留判断负担。
    """
    mode = (getattr(info, "external_attr", 0) >> 16) & 0xFFFF
    return bool(mode) and stat.S_ISLNK(mode)


def _zip_safe_relpath(name):
    """把 ZIP 成员名归一化成安全相对路径；不安全返回 None。

    拒的形状（每一类都是真实攻击面，不是理论洁癖）：
      · 绝对路径 "/etc/passwd"、盘符 "C:/x"
      · 任何一段是 ".."（ZIP Slip 本体）
      · 反斜杠（Windows 分隔符，posixpath 归一化不掉，混着来最阴）
      · 空名 / 纯分隔符 / 含空段（"a//b"）
    目录条目（"a/b/"）返回去尾斜杠的 "a/b"，由调用方决定 mkdir。
    """
    if not isinstance(name, str) or not name:
        return None
    n = name.replace("\\", "/")
    if n.startswith("/") or (len(n) > 1 and n[1] == ":"):
        return None
    parts = n.split("/")
    while parts and parts[-1] == "":     # 目录条目的尾斜杠
        parts.pop()
    if not parts:
        return None
    for p in parts:
        if p in ("", ".", ".."):
            return None
    return "/".join(parts)


class PluginHostError(Exception):
    """插件桥接层错误。code 取 PLUGIN_HOST_ERROR_CODES 之一。

    与 Node 侧 plugin_host.cjs 的 hostError() 对应：跨进程传错误时，
    code 走 JSON 的 error.code 字段，两边各自还原成带 code 的异常对象。
    """

    def __init__(self, code, message=""):
        self.code = code if code in PLUGIN_HOST_ERROR_CODES else "E_INTERNAL"
        super().__init__(message or self.code)


def find_node_binary():
    """定位 node 可执行文件。找不到返回 None（插件整体降级为 unsupported）。

    每次现查而不缓存：P1-2 刚装上 node，若在安装前就 import 了本模块，
    缓存住 None 会导致装完仍认为不可用，必须重启服务才行。
    """
    for cand in PLUGIN_NODE_CANDIDATES:
        if os.path.sep in cand or "/" in cand:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


class BridgeClient:
    """一个插件的 Node 子进程 + NDJSON 双向 RPC 通道。

    线程模型（关键，改之前先读完）：
      - reader 线程：常驻读 stdout。收到**响应**按 id 配对唤醒等待者；
        收到 Node 发来的**请求**（storage.get 等）必须丢给工作线程处理，
        绝不能在 reader 线程里同步做 —— 因为插件可能在 tool.execute 的
        处理过程中回头调 storage.get，形成嵌套；reader 一旦被慢操作占住，
        整条协议流就死了（表现为所有请求超时）。
      - stderr 线程：常驻读 stderr 转宿主日志，带速率限制。
        不读就会填满管道缓冲区，把 node 阻塞死。
      - 调用方线程：call() 发请求后用 Condition 等响应，带超时。

    写 stdin 必须持 _send_lock：多个 HTTP 线程可能同时调工具，
    JSON 行交错会让 Node 侧 readline 收到半行，解析失败。
    """

    def __init__(self, plugin_id, plugin_dir, manifest, node_bin=None):
        if not PLUGIN_ID_RE.match(plugin_id or ""):
            raise PluginHostError("E_INVALID_ARGUMENT", f"非法插件 id: {plugin_id!r}")
        self.plugin_id = plugin_id
        self.plugin_dir = Path(plugin_dir)
        self.manifest = manifest if isinstance(manifest, dict) else {}
        self.node_bin = node_bin or find_node_binary()

        self.proc = None
        self.pid = None
        self.ready = False
        self.dead = False
        self.dead_reason = ""
        self.node_version = ""
        self.started_at = 0.0

        self._next_id = 1
        self._pending = {}                 # id -> {"ev": Event, "msg": None}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._logs = []                    # 环形日志（面板用）
        self._log_window = []              # 速率限制窗口的时间戳
        self._reader = None
        self._errreader = None
        self._work_pool = None             # 处理 Node 入站请求的线程池
        # 入站请求的服务实现由 PluginManager 注入（它才拿得到 SETTINGS / LLMClient）
        self.host_services = {}

    # ---------- 日志 ----------

    def log(self, line):
        """收一行插件日志。带速率限制：插件狂打日志不能拖垮宿主。"""
        now = time.time()
        with self._log_lock:
            # 滑动窗口限流
            self._log_window = [t for t in self._log_window if now - t < 1.0]
            if len(self._log_window) >= PLUGIN_LOG_BURST:
                # 已经在这个窗口里丢过行了就不重复记
                if not self._logs or not self._logs[-1].endswith("(日志过多，已限流)"):
                    self._logs.append(f"[{self.plugin_id}] …(日志过多，已限流)")
                    if len(self._logs) > PLUGIN_LOG_RING:
                        del self._logs[0]
                return
            self._log_window.append(now)
            self._logs.append(f"[{self.plugin_id}] {line}")
            if len(self._logs) > PLUGIN_LOG_RING:
                del self._logs[0]

    def get_logs(self, limit=80):
        with self._log_lock:
            return list(self._logs)[-limit:]

    # ---------- 进程生命周期 ----------

    def start(self, storage_snapshot=None):
        """起 node 子进程 → 等 host.ready → 跑 plugin.register。

        返回 register 的结果 dict（含 tools / promptProviders / warnings）。
        失败抛 PluginHostError，调用方负责把插件落到 failed 态。
        """
        if not self.node_bin:
            raise PluginHostError(
                "E_CAPABILITY_UNAVAILABLE",
                "找不到 node 可执行文件（Termux 需 pkg install nodejs）")
        if not PLUGIN_HOST_JS.is_file():
            raise PluginHostError(
                "E_NOT_FOUND", f"缺少插件宿主壳: {PLUGIN_HOST_JS}")
        entry = self.manifest.get("entry")
        if not entry or not (self.plugin_dir / entry).is_file():
            raise PluginHostError(
                "E_NOT_FOUND", f"插件入口不存在: {self.plugin_dir / str(entry)}")

        popen_kw = {}
        if os.name == "posix":
            # 自成进程组：kill 时能连插件可能 fork 的孙进程一起清掉，
            # 否则 node 死了但它 spawn 的东西变孤儿（termux-api 那套已踩过这坑）
            popen_kw["start_new_session"] = True

        try:
            self.proc = subprocess.Popen(
                [self.node_bin, str(PLUGIN_HOST_JS), self.plugin_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=str(self.plugin_dir),
                encoding="utf-8", errors="replace", bufsize=1, **popen_kw)
        except OSError as e:
            raise PluginHostError("E_INTERNAL", f"启动 node 失败: {e}")

        self.pid = self.proc.pid
        self.started_at = time.time()

        # 入站请求的工作池：并发度 4 够了（storage/secrets 都是快操作，
        # llm.generateText 慢但不会同时来很多）。队列不设上限会让内存失控，
        # 但设太小又会在插件密集回调时抛 RejectedExecution —— 折中给 64。
        self._work_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix=f"plg-{self.plugin_id}")

        self._reader = threading.Thread(
            target=self._read_stdout, name=f"plg-out-{self.plugin_id}", daemon=True)
        self._errreader = threading.Thread(
            target=self._read_stderr, name=f"plg-err-{self.plugin_id}", daemon=True)
        self._reader.start()
        self._errreader.start()

        self._wait_ready(PLUGIN_READY_TIMEOUT)
        return self.register(storage_snapshot or {})

    def _wait_ready(self, timeout):
        """等 plugin_host.cjs 发来的 host.ready 通知。

        不等就绪信号就直接发 register 会撞竞态：node 进程 fork 出来了但
        JS 还没跑到 readline 那一行，此时写入的请求会躺在管道缓冲里，
        看起来「没响应」，实际只是还没开始读。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.dead:
                raise PluginHostError(
                    "E_INTERNAL", f"插件进程启动即退出: {self.dead_reason}")
            if self.ready:
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise PluginHostError(
                    "E_INTERNAL",
                    f"插件进程退出码 {self.proc.returncode}（未见 host.ready）: "
                    f"{self.dead_reason or '无 stderr 输出'}")
            time.sleep(0.02)
        raise PluginHostError("E_INTERNAL", f"等待 host.ready 超时 {timeout}s")

    def register(self, storage_snapshot):
        """跑插件的 register(ctx)，拿回它注册的工具与提示词 provider 清单。"""
        storage_root = self.plugin_dir / "data"
        try:
            res = self.call("plugin.register", {
                "manifest": self.manifest,
                "pluginDir": str(self.plugin_dir),
                "storageRoot": str(storage_root),
                # storage 快照：PluginStorage.get 是**同步**契约，但 Python 在
                # 进程外只能异步。解法是 register 时把整个 kv 一次性灌给 Node，
                # 之后 get 读本地缓存、set 异步回写。契约语义得以保持。
                "storageSnapshot": storage_snapshot,
            }, timeout=PLUGIN_REGISTER_TIMEOUT)
        except PluginHostError:
            raise
        except Exception as e:
            raise PluginHostError("E_INTERNAL", f"register 失败: {type(e).__name__}: {e}")
        if not isinstance(res, dict) or not res.get("ok"):
            raise PluginHostError("E_INTERNAL", f"register 返回异常: {res!r}")
        return res

    def kill(self):
        """硬杀进程组。用于崩溃回收与 dispose 失败后的兜底。"""
        p = self.proc
        if p is None:
            return
        try:
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:
                    p.kill()
            else:
                p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass

    def dispose(self):
        """优雅停止：先让 Node 跑 dispose（onDispose 逆序 + unregister），再关管道。

        dispose 失败不等于停止失败 —— 插件可能已经崩了，但宿主必须把自己的
        资源收干净，所以无论如何最后都 kill + 标记 dead。
        """
        errors = []
        if self.proc is not None and self.proc.poll() is None and not self.dead:
            try:
                res = self.call("plugin.dispose", {}, timeout=PLUGIN_DISPOSE_TIMEOUT)
                if isinstance(res, dict) and res.get("errors"):
                    errors = list(res["errors"])
            except Exception as e:
                errors.append(f"dispose 调用失败: {type(e).__name__}: {e}")
        self._shutdown()
        return errors

    def _shutdown(self):
        """关管道 + 杀进程 + 唤醒所有等待者（否则它们会干等到超时）。"""
        self.dead = True
        p = self.proc
        if p is not None:
            for stream in (p.stdin, p.stdout, p.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
        self.kill()
        pool = self._work_pool
        if pool is not None:
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
            self._work_pool = None
        # 把所有在途请求全部失败掉。不这么做的话，正在等响应的 HTTP 线程
        # 会一直等到自己的 timeout，表现为「停用插件后界面卡几十秒」。
        with self._pending_lock:
            for slot in self._pending.values():
                slot["msg"] = {"error": {"code": "E_PLUGIN_STOPPING",
                                         "message": "插件已停止"}}
                slot["ev"].set()
            self._pending.clear()

    def send_event(self, event, payload):
        """投递宿主事件（host:turn:finished 等）。单向，不等响应，失败静默。"""
        if self.dead or not self.ready:
            return
        try:
            self.notify("host.event", {"event": event, "payload": payload})
        except Exception:
            pass

    # ---------- RPC ----------

    def _send(self, obj):
        """写一行 JSON 到 stdin。必须持锁：并发写会让行交错，Node 侧解析炸。"""
        p = self.proc
        if p is None or p.stdin is None or self.dead:
            raise PluginHostError("E_PLUGIN_STOPPING", "插件进程未运行")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._send_lock:
            try:
                p.stdin.write(line)
                p.stdin.flush()
            except (OSError, ValueError) as e:
                self.dead = True
                self.dead_reason = f"写入管道失败: {e}"
                raise PluginHostError("E_PLUGIN_STOPPING", self.dead_reason)

    def call(self, method, params=None, timeout=30.0):
        """发请求并等响应。返回 result，error 分支抛 PluginHostError。"""
        if self.dead:
            raise PluginHostError("E_PLUGIN_STOPPING",
                                  f"插件已停止（{self.dead_reason or '未知原因'}）")
        with self._pending_lock:
            rid = self._next_id
            self._next_id += 1
            slot = {"ev": threading.Event(), "msg": None}
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params if isinstance(params, dict) else {}})
        except PluginHostError:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise

        if not slot["ev"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise PluginHostError(
                "E_INTERNAL", f"{method} 超时 {timeout:.0f}s（插件可能挂死）")

        msg = slot["msg"] or {}
        with self._pending_lock:
            self._pending.pop(rid, None)
        if "error" in msg and msg["error"]:
            err = msg["error"]
            raise PluginHostError(err.get("code") or "E_INTERNAL",
                                  err.get("message") or "(无错误信息)")
        return msg.get("result")

    def notify(self, method, params=None):
        """单向通知（无 id，不等响应）。"""
        self._send({"jsonrpc": "2.0", "method": method,
                    "params": params if isinstance(params, dict) else {}})

    # ---------- reader 线程 ----------

    def _read_stdout(self):
        """常驻读协议流。这个线程一旦退出，插件就等于失联。"""
        p = self.proc
        if p is None or p.stdout is None:
            return
        while True:
            try:
                line = p.stdout.readline()
            except (OSError, ValueError):
                break
            if not line:
                break                      # EOF：进程退了或管道被关
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # 协议被污染。plugin_host.cjs 已把 console.* 改道 stderr，
                # 走到这里说明有东西绕过改道直接写了 stdout（比如插件用了
                # process.stdout.write）。记日志但**不能崩**：一行坏数据
                # 不该让整个插件失联。
                self.log(f"⚠ 协议流出现非 JSON 行（已忽略）: {line[:200]}")
                continue
            self._dispatch(msg)

        # 走到这里 = 协议流断了
        rc = p.poll()
        self.dead = True
        if not self.dead_reason:
            self.dead_reason = f"协议流 EOF（进程退出码 {rc}）"
        self._fail_all_pending(self.dead_reason)

    def _read_stderr(self):
        """常驻读 stderr 转宿主日志。不读会填满管道把 node 阻塞死。"""
        p = self.proc
        if p is None or p.stderr is None:
            return
        while True:
            try:
                line = p.stderr.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            line = line.rstrip()
            if line:
                self.log(line)

    def _fail_all_pending(self, reason):
        """进程死了，把所有在途请求立即失败掉，别让调用方干等到超时。"""
        with self._pending_lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["msg"] = {"error": {"code": "E_PLUGIN_STOPPING", "message": reason}}
            slot["ev"].set()

    def _dispatch(self, msg):
        """分流一条协议消息：响应 → 唤醒等待者；请求 → 丢工作线程处理。"""
        method = msg.get("method")
        mid = msg.get("id")

        if method is None:
            # 响应
            if mid is None:
                # id=null 的错误 = plugin_host.cjs 启动期致命错误（fail()）
                err = msg.get("error") or {}
                self.dead = True
                self.dead_reason = (f"{err.get('code') or 'E_INTERNAL'}: "
                                    f"{err.get('message') or '未知启动错误'}")
                self._fail_all_pending(self.dead_reason)
                return
            with self._pending_lock:
                slot = self._pending.get(mid)
            if slot is not None:
                slot["msg"] = msg
                slot["ev"].set()
            return

        # 请求或通知。host.ready 是通知（无 id），其余是 Node 发来的宿主服务调用。
        if method == "host.ready":
            params = msg.get("params") or {}
            self.node_version = str(params.get("node") or "")
            self.ready = True
            self.log(f"host.ready node={self.node_version} pid={params.get('pid')}")
            return

        if mid is None:
            # 通知（log 等），不需要应答，直接就地处理（都是快操作）
            self._serve(method, msg.get("params") or {}, None)
            return

        # 需要应答的请求：必须丢工作线程。在 reader 线程里同步做会造成
        # 嵌套死锁 —— 插件在 tool.execute 处理中回调 storage.get，
        # 而 reader 正被 tool.execute 的响应链占着。
        pool = self._work_pool
        if pool is None:
            self._reply_error(mid, "E_PLUGIN_STOPPING", "插件正在停止")
            return
        try:
            pool.submit(self._serve, method, msg.get("params") or {}, mid)
        except RuntimeError:
            self._reply_error(mid, "E_PLUGIN_STOPPING", "插件正在停止")

    def _reply(self, mid, result):
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:
            self.log(f"⚠ 回应答失败 id={mid}: {e}")

    def _reply_error(self, mid, code, message):
        try:
            self._send({"jsonrpc": "2.0", "id": mid,
                        "error": {"code": code, "message": message}})
        except Exception:
            pass

    def _serve(self, method, params, mid):
        """处理 Node 发来的宿主服务请求。mid=None 表示通知（无需应答）。

        ⚠ 任何未实现的 method 都必须**回错误**，绝不能不回 ——
        不回的话 Node 侧那个 Promise 会一直挂着直到超时，插件表现为
        「莫名卡死」，极难排查。
        """
        try:
            fn = self.host_services.get(method)
            if fn is None:
                if mid is not None:
                    self._reply_error(
                        mid, "E_CAPABILITY_UNAVAILABLE",
                        f"宿主未实现服务: {method}")
                return
            result = fn(params)
            if mid is not None:
                self._reply(mid, result)
        except PluginHostError as e:
            if mid is not None:
                self._reply_error(mid, e.code, str(e))
        except Exception as e:
            self.log(f"⚠ 服务 {method} 异常: {type(e).__name__}: {e}")
            if mid is not None:
                self._reply_error(mid, "E_INTERNAL", f"{type(e).__name__}: {e}")


# ---------- 插件存储：storage（kv.json） ----------

PLUGIN_STORAGE_LOCK = threading.Lock()


def plugin_storage_path(pid):
    return PLUGINS_DIR / pid / "data" / "kv.json"


def plugin_storage_load(pid):
    """读插件的 kv 存储。文件缺失/损坏都当空 —— 插件首次启动是正常情况，
    不能因为「没有 kv.json」就让它 register 失败。"""
    p = plugin_storage_path(pid)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


def plugin_storage_write(pid, kv):
    """原子写：先落 .tmp 再 os.replace。

    直接覆盖写会让「写到一半时服务被杀」留下半截 JSON，
    下次加载解析失败 → 数据全丢。os.replace 在同一文件系统内是原子的。
    """
    p = plugin_storage_path(pid)
    with PLUGIN_STORAGE_LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(kv, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, p)
            return True
        except OSError:
            return False


# ---------- 插件 → TOOLS 的字段映射 ----------

# 桌面端 PluginTool.risk 的取值集与手机端 TOOLS[*]["risk"] 完全一致，
# 直接透传即可，不需要映射表。readonly 则手机端独有，按 risk 推导。
PLUGIN_READONLY_RISKS = frozenset({"safe", "fs-read"})

# 桌面端 risk → 面板图标。内置工具在 TOOLS 里自带 icon，插件工具没有，
# 补一个按风险级的默认图标，让工具面板不至于显示成 ⚙ 一片。
PLUGIN_RISK_ICONS = {
    "safe": "🧩", "fs-read": "🧩", "fs-write": "🧩",
    "shell": "🧩", "network": "🧩", "input-control": "🧩",
}


def plugin_tool_params(input_schema):
    """PluginTool.inputSchema → 手机端 TOOLS[*]["params"]。

    两边的形状差异（这是接线的核心，改之前先看清）：

      桌面端 inputSchema（标准 JSON Schema）:
        {"type":"object",
         "properties": {"city": {"type":"string","description":"城市名"}},
         "required": ["city"]}              ← required 是**并列的数组**

      手机端 params（tool_schema 自己那套约定）:
        {"city": {"type":"string","desc":"城市名","required":True}}
                                             ← required 写在**每个 param 内部**

    所以要做两件事：
      1. required 数组回写进各 param 的 required 标记 —— tool_schema 靠
         spec.get("required") 抽 required 列表，missing_required() 也靠它拦
         「模型漏传必填参数」。不回写的话必填约束就整个丢了。
      2. description → desc —— tool_schema 里 `if spec.get("desc")` 才会写回
         description。插件用的是标准 JSON Schema 的 description，直接留着也行
         （_NON_SCHEMA_KEYS 不剥它），但统一成 desc 更符合本文件的既有约定，
         也避免「插件写了 description、内置工具写 desc」两套并存。
    """
    props = input_schema.get("properties") if isinstance(input_schema, dict) else None
    props = props if isinstance(props, dict) else {}
    req = input_schema.get("required") if isinstance(input_schema, dict) else None
    req_set = {r for r in req if isinstance(r, str)} if isinstance(req, list) else set()

    out = {}
    for pname, spec in props.items():
        if not isinstance(pname, str) or not pname:
            continue
        if not isinstance(spec, dict):
            spec = {}
        p = dict(spec)
        # description → desc（保留原 description 也无害，tool_schema 会剥 desc）
        if "desc" not in p and isinstance(p.get("description"), str):
            p["desc"] = p["description"]
        p["required"] = pname in req_set
        out[pname] = p
    return out


def plugin_tool_to_spec(plugin_id, tool):
    """一个 PluginTool 投影 → 手机端 TOOLS 条目。"""
    tid = tool.get("id")
    name = tool.get("name") or tid
    desc = tool.get("description") or ""
    risk = tool.get("risk") or "safe"
    if risk not in PLUGIN_RISK_ICONS:
        risk = "safe"

    # desc 是模型唯一能看到的工具说明（见 tool_schema），所以要把显示名拼进去。
    # 桌面端 name 与 description 是分开的两个字段，手机端只有一个 desc。
    full_desc = f"{name}：{desc}" if name and desc and name != desc else (desc or name or tid)

    return {
        "desc": full_desc,
        "icon": PLUGIN_RISK_ICONS.get(risk, "🧩"),
        "readonly": risk in PLUGIN_READONLY_RISKS,
        "risk": risk,
        "params": plugin_tool_params(tool.get("inputSchema") or {}),
        # handler 用 "plugin:<pid>:<tid>" 前缀标记。run_tool 看到这个前缀
        # 就走桥接分支，而不是去 TOOL_HANDLERS 里找本地函数。
        # 用字符串前缀而不是往 TOOL_HANDLERS 塞闭包，是为了保持 TOOLS
        # 「纯数据、可 JSON 序列化、可被验证脚本静态检查」这个既有约定。
        "handler": f"plugin:{plugin_id}:{tid}",
        "plugin": plugin_id,
        # modes 是手机端此前没有的字段（内置工具全模式可用）。
        # 缺省 None = 全模式；有值时 build_system_prompt 按模式过滤。
        "modes": tool.get("modes") if isinstance(tool.get("modes"), list) else None,
        "needs_ctx": True,     # 插件工具要收 PluginToolContext（userQuery/mode/sid）
        "effectKind": tool.get("effectKind") or "unknown",
        "plugin_tool_name": name,
    }


# ---------- PluginManager ----------

class PluginManager:
    """插件生命周期管理：安装 / 启用 / 停用 / 卸载 / 状态查询。

    与 BridgeClient 的分工：
      BridgeClient = 一个插件的进程与协议通道（技术层）
      PluginManager = 谁该起、谁该停、状态怎么落盘、工具怎么进出 TOOLS（业务层）

    锁策略：只有一把 _lock，且**绝不在持锁期间做阻塞 IO**。
    start/stop 里起进程、等 register 都是秒级操作，持锁会把 HTTP 线程全堵死
    （这个坑 v7 的全局锁踩过，见文件头注释）。所以：
      - 持锁只做字典读写与 TOOLS 增删
      - 起进程/发 register 在锁外做，成功后再持锁登记
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._clients = {}        # pid -> BridgeClient
        self._states = {}         # pid -> 状态字符串（PLUGIN_STATES 之一）
        self._errors = {}         # pid -> 最近一次错误
        self._manifests = {}      # pid -> manifest dict
        self._tool_ids = {}       # pid -> [工具 id]，停用时要精确摘掉
        self._providers = {}      # pid -> [provider dict]（P4 用）
        self._fail_counts = {}    # pid -> 连续失败次数（熔断用，P3）

    # ---------- 路径与清单 ----------

    def plugin_dir(self, pid):
        return PLUGINS_DIR / pid

    def scan_installed(self):
        """扫 plugins/ 目录，返回 {pid: manifest}。以磁盘为准，不信任台账。

        台账（SETTINGS.plugins.registry）记的是「用户意图」（要不要启用），
        磁盘记的是「事实」（装了什么）。两者不一致时以磁盘为准，
        否则用户手删目录后台账还留着一条永远起不来的僵尸记录。
        """
        out = {}
        if not PLUGINS_DIR.is_dir():
            return out
        for entry in sorted(PLUGINS_DIR.iterdir()):
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            pid = entry.name
            if not PLUGIN_ID_RE.match(pid):
                continue
            mf = self._read_manifest(entry)
            if mf is not None:
                out[pid] = mf
        return out

    def _read_manifest(self, pdir):
        """读并校验 manifest.json。不合法返回 None（调用方决定怎么标状态）。"""
        f = Path(pdir) / "manifest.json"
        if not f.is_file():
            return None
        try:
            mf = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
        if not isinstance(mf, dict):
            return None
        pid = mf.get("id")
        if not isinstance(pid, str) or not PLUGIN_ID_RE.match(pid):
            return None
        # manifest.id 必须与目录名一致：否则台账、密钥命名空间、路径三者会错位
        if pid != Path(pdir).name:
            return None
        return mf

    def check_support(self, mf):
        """判断插件在本宿主上能不能跑。返回 (supported: bool, reason: str)。

        不 supported 时面板应灰显并说明原因，**不给启用开关** ——
        让用户点了再失败是很糟的体验。
        """
        api = mf.get("apiVersion")
        if api != PLUGIN_API_VERSION:
            return False, (f"插件要求 apiVersion={api}，本宿主只支持 "
                           f"{PLUGIN_API_VERSION}")
        if mf.get("entry") != "index.cjs" and not str(mf.get("entry", "")).endswith(".cjs"):
            # 不是硬性拒绝，但值得提示：非 .cjs 入口可能是 ESM，require 会失败
            pass
        if not find_node_binary():
            return False, "未找到 node 运行时（Termux 需 pkg install nodejs）"
        if not PLUGIN_HOST_JS.is_file():
            return False, f"缺少插件宿主壳 {PLUGIN_HOST_JS.name}"
        return True, ""

    # ---------- 状态 ----------

    def state(self, pid):
        with self._lock:
            return self._states.get(pid, "not_installed")

    def manifest(self, pid):
        """某插件的 manifest（加锁读，读不到就现读磁盘）。

        给 HTTP handler 用的公共入口。handler 直接摸 self._manifests 是**不加锁**
        读内部字典，虽然 CPython 单键读本身原子，但与 start()/stop() 里的写
        并发时语义不清；加个锁的成本是一次可重入判断，换来「handler 永远不
        碰内部状态」这条干净的边界。
        """
        with self._lock:
            mf = self._manifests.get(pid)
        if isinstance(mf, dict):
            return mf
        return self._read_manifest(self.plugin_dir(pid))

    def running_ids(self):
        with self._lock:
            return sorted(k for k, v in self._states.items() if v == "running")

    def snapshot(self):
        """全量状态快照，供 GET /plugins 与设置面板用。"""
        installed = self.scan_installed()
        reg = plugin_registry()
        out = []
        with self._lock:
            for pid, mf in sorted(installed.items()):
                supported, reason = self.check_support(mf)
                client = self._clients.get(pid)
                reg_e = reg.get(pid) if isinstance(reg.get(pid), dict) else {}
                entry = {
                    "id": pid,
                    "name": mf.get("name") or pid,
                    "version": str(mf.get("version") or ""),
                    "author": str(mf.get("author") or ""),
                    "description": str(mf.get("description") or ""),
                    "icon": mf.get("icon") or None,
                    "settingsPanel": mf.get("settingsPanel") or None,
                    "deps": mf.get("deps") or [],
                    "state": self._states.get(pid, "installed"),
                    "enabled": bool(reg_e.get("enabled", False)),
                    "supported": supported,
                    "unsupportedReason": "" if supported else reason,
                    "error": self._errors.get(pid, ""),
                    "tools": list(self._tool_ids.get(pid, [])),
                    "pid": client.pid if client else None,
                    "nodeVersion": client.node_version if client else "",
                    "startedAt": client.started_at if client else 0,
                    # 安装时静态扫出的高危调用（pattern 名）。面板据此打黄标，
                    # 让用户在点「启用」之前看得见这个包能干什么。
                    "risky": list(reg_e.get("risky") or []),
                    # 从哪装的（upload / url / inbox / market / local），以及装的时候
                    # sha256 有没有被校验过 —— unverified 的包面板要单独提示一句。
                    "source": str(reg_e.get("source") or ""),
                    "installedAt": str(reg_e.get("installedAt") or ""),
                    "verified": bool(reg_e.get("sha256")),
                }
                out.append(entry)
        # 台账里有、磁盘上没有的（用户手删了目录）：报出来让用户知道
        with self._lock:
            for pid, entry in reg.items():
                if pid not in installed:
                    out.append({
                        "id": pid, "name": pid, "version": entry.get("version", ""),
                        "author": "", "description": "(插件目录已不存在)",
                        "icon": None, "settingsPanel": None, "deps": [],
                        "state": "not_installed",
                        "enabled": bool(entry.get("enabled", False)),
                        "supported": False,
                        "unsupportedReason": "插件目录已被删除，台账残留",
                        "error": entry.get("lastError", ""),
                        "tools": [], "pid": None, "nodeVersion": "", "startedAt": 0,
                        # 形状必须与上面那条分支一致：前端按同一套字段渲染，
                        # 少一个就会读到 undefined，卡片上出现「undefined」字样。
                        "risky": list(entry.get("risky") or []),
                        "source": str(entry.get("source") or ""),
                        "installedAt": str(entry.get("installedAt") or ""),
                        "verified": bool(entry.get("sha256")),
                    })
        return out

    # ---------- 安装 ----------

    def install_from_dir(self, src_dir, source="local", sha256="",
                         enable=False, allow_unverified=True):
        """从本地目录安装插件（绕过市场，供 P1 自测与「扫描 inbox」通道用）。

        返回 (ok: bool, info: dict|str)。

        安全要点：
          · 目标目录名**只能**来自校验过的 manifest.id，绝不用 src_dir 的名字
            —— src_dir 可能叫 "../../etc" 之类，拿它拼路径就是穿越。
          · id 必须过 PLUGIN_ID_RE（^[a-z0-9][a-z0-9._-]{0,63}$）。
          · 复制时逐个成员校验落点必须在 plugins/<id>/ 内（防符号链接与 ../）。
          · 已存在的同名插件先停进程再覆盖，否则会出现「磁盘是新版本、
            内存里跑的是旧版本」的分裂状态。
        """
        src = Path(src_dir)
        if not src.is_dir():
            return False, f"源目录不存在: {src_dir}"

        mf = self._read_manifest(src)
        if mf is None:
            # _read_manifest 会校验 id 形状与「id == 目录名」，但源目录名是任意的，
            # 这里只需要 id 本身合法，所以单独再判一次给出准确原因。
            raw = src / "manifest.json"
            if not raw.is_file():
                return False, "源目录缺少 manifest.json"
            try:
                probe = json.loads(raw.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError, OSError) as e:
                return False, f"manifest.json 解析失败: {e}"
            pid = probe.get("id") if isinstance(probe, dict) else None
            if not isinstance(pid, str) or not PLUGIN_ID_RE.match(pid):
                return False, (f"manifest.id 非法: {pid!r}"
                               f"（要求小写字母数字开头，仅含 a-z 0-9 . _ -，≤64 字符）")
            mf = probe
            # 源目录名与 id 不一致是允许的（比如 inbox 里解压出的临时目录），
            # 落位时统一按 id 命名。

        pid = str(mf.get("id"))
        api = mf.get("apiVersion")
        if api != PLUGIN_API_VERSION:
            return False, f"插件要求 apiVersion={api}，本宿主只支持 {PLUGIN_API_VERSION}"
        entry = mf.get("entry")
        if not isinstance(entry, str) or not entry or "/" in entry or "\\" in entry or ".." in entry:
            return False, f"manifest.entry 必须是插件目录内的裸文件名，实际: {entry!r}"
        if not (src / entry).is_file():
            return False, f"入口文件不存在: {entry}"

        # 覆盖安装：先停掉正在跑的旧实例
        with self._lock:
            old_state = self._states.get(pid)
        if old_state in ("running", "starting", "stopping", "crashed", "failed"):
            self.stop(pid, keep_registry=True)

        dest = PLUGINS_DIR / pid
        try:
            PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
            dest.mkdir(parents=True, exist_ok=True)
            copied, skipped = self._copy_tree_safe(src, dest, pid)
        except OSError as e:
            return False, f"复制插件文件失败: {e}"

        # 装完立刻校验：manifest 与 entry 必须真的落位了
        if not (dest / "manifest.json").is_file() or not (dest / entry).is_file():
            return False, "复制后校验失败：manifest.json 或入口文件未落位"

        self._save_registry(
            pid,
            version=str(mf.get("version") or ""),
            # 对齐桌面端语义：用户插件首次安装后**默认停用**，启用需显式操作
            enabled=bool(enable),
            source=source,
            sha256=sha256 if sha256 else "",
            installedAt=time.strftime("%Y-%m-%dT%H:%M:%S"),
            lastError="" if allow_unverified else "未经 sha256 校验",
        )
        with self._lock:
            self._states[pid] = "installed"
            self._manifests[pid] = mf
            self._errors.pop(pid, None)

        supported, reason = self.check_support(mf)
        # 静态高危扫描。放在 install_from_dir 里而不是各个导入通道里 ——
        # 这里是**所有**安装路径的汇聚点（ZIP 上传 / URL / inbox / 市场 /
        # 脚本直调 install_from_dir），放一处就全覆盖，不会漏掉某条通道。
        # 扫的是已落位的目录：那才是真正会被 node 执行的那份。
        # 安全检测开关（plugins.securityScan）：关掉后导入时不再做静态高危
        # 扫描，也不写黄标台账。扫描点仍固定在 install_from_dir —— 开关只决定
        # 「扫不扫」，不改变「所有通道都汇聚到这一处」的结构。
        if plugins_cfg("securityScan", True):
            try:
                risky = self.scan_risky(dest)
            except OSError:
                risky = []
        else:
            risky = []
        if risky:
            # 只把 pattern 名写进台账（文件清单是「安装那一刻」给用户看的，
            # 长期留在 .config.json 里没意义，而那文件还含 API key，别撑大它）。
            # 台账留名字，面板重开后依然能显示黄标。
            try:
                self._save_registry(pid, risky=[r["pattern"] for r in risky])
            except Exception:
                pass            # 黄标写失败不该让一次成功的安装变成失败
        info = {
            "id": pid, "version": str(mf.get("version") or ""),
            "name": mf.get("name") or pid, "files": copied, "skipped": skipped,
            "enabled": bool(enable), "supported": supported,
            "unsupportedReason": "" if supported else reason,
            "verified": bool(sha256), "risky": risky,
        }
        if enable and supported:
            good, res = self.start(pid)
            info["startOk"] = bool(good)
            info["startResult"] = res
        return True, info

    def _copy_tree_safe(self, src, dest, pid):
        """递归复制插件目录，逐成员校验落点。返回 (copied, skipped)。

        为什么不用 shutil.copytree：它的 dirs_exist_ok 在 3.8+ 才有，
        而且对符号链接的默认行为（copytree 会跟随）在这里不安全 ——
        插件包里放一个指向 /data/data/com.termux/files/home 的软链，
        跟随复制就等于把宿主的家目录整个抄进插件目录。
        这里显式跳过软链，并且每个落点都验一次是否在 dest 内。
        """
        src = Path(src)
        dest = Path(dest)
        dest_root = dest.resolve()
        copied, skipped = 0, []
        for item in sorted(src.rglob("*")):
            try:
                rel = item.relative_to(src)
            except ValueError:
                skipped.append(str(item))
                continue
            target = (dest / rel)
            # 落点必须严格在 dest 内。rglob 不会产出 ../，但软链解析后可能
            # 指向外面，所以 resolve 之后再比一次。
            #
            # ⚠ 用 is_relative_to 而不是 str.startswith：startswith 会把
            #   "/plugins/foobar/x" 判成在 "/plugins/foo" 之内（前缀撞名），
            #   那等于这道防线形同虚设。Python 3.9+ 有 is_relative_to，
            #   本机 3.12 / 手机 3.14 都支持；保留 parts 兜底只为极端老版本。
            try:
                resolved = target.resolve()
                if hasattr(resolved, "is_relative_to"):
                    inside = resolved.is_relative_to(dest_root)
                else:
                    inside = dest_root.parts == resolved.parts[:len(dest_root.parts)]
                if not inside:
                    skipped.append(f"{rel}(越出插件目录)")
                    continue
            except OSError:
                skipped.append(f"{rel}(路径不可解析)")
                continue
            if item.is_symlink():
                skipped.append(f"{rel}(符号链接，已跳过)")
                continue
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not item.is_file():
                continue
            # data/ 是插件运行时数据目录，安装时不该从源包带过来
            # （覆盖安装会抹掉用户已有的 kv.json）
            parts = rel.parts
            if parts and parts[0] == "data":
                skipped.append(f"{rel}(运行时数据，已跳过)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target))
            copied += 1
        return copied, skipped

    # ---------- 导入：ZIP 解包（P6 内核，三条通道共用） ----------

    def scan_risky(self, pdir):
        """静态扫插件目录里的 .cjs/.js，报告高危调用。**只提示，不阻断**。

        插件与宿主同权限（node 子进程没有沙箱，桌面端 README 也明示「审核不构成
        担保」），所以扫描的价值不是拦截，而是让用户在点「启用」之前看得见
        这个包能干什么。返回 [{"pattern","why","files":[...]}]。

        只读文件头 512 KB：插件源码通常几十 KB，读全量既慢又可能被巨型文件拖住。
        用 os.walk 而不是 rglob：walk 能**原地剪枝** node_modules（del dirs[:]），
        rglob 会先把几千个第三方文件全遍历出来再靠 parts 判断跳过，白跑一趟。
        """
        hits = {}
        pdir = Path(pdir)
        if not pdir.is_dir():
            return []
        for cur, dirs, names in os.walk(str(pdir)):
            # 剪枝：node_modules 里是第三方代码，不算插件作者的意图，且量极大
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
            for nm in sorted(names):
                if not nm.lower().endswith((".cjs", ".js", ".mjs")):
                    continue
                f = Path(cur) / nm
                try:
                    with open(f, "rb") as fh:
                        text = fh.read(512 * 1024).decode("utf-8", "replace")
                except OSError:
                    continue
                try:
                    rel = str(f.relative_to(pdir))
                except ValueError:
                    rel = nm
                for pat, why in PLUGIN_RISKY_PATTERNS:
                    if pat in text:
                        slot = hits.setdefault(pat, {"pattern": pat, "why": why, "files": []})
                        if len(slot["files"]) < 8:
                            slot["files"].append(rel)
        return [hits[p] for p, _ in PLUGIN_RISKY_PATTERNS if p in hits]

    def _unpack_zip_safe(self, data, work_dir):
        """把 ZIP 字节安全解到 work_dir。返回 (root_dir, stats) 或抛 ValueError。

        root_dir 是探测出的**插件根**（含 manifest.json 的那一层）。市场 ZIP 有两种
        常见结构：直接是 manifest.json，或者包一层 `<id>/manifest.json`。
        不探测就会让 install_from_dir 报「源目录缺少 manifest.json」，而用户看到的
        是一个跟真实原因无关的错误。

        四道防线，缺一就有真实攻击面：
          1. 成员名归一化（_zip_safe_relpath）→ 拒 `../`、绝对路径、盘符、反斜杠
          2. 落点 resolve() 后必须 is_relative_to(work_dir) → 兜住归一化没想到的形状
          3. 软链成员直接拒（_zip_member_is_symlink）
          4. 边解边算累计字节与成员数 → zip bomb 在写满磁盘之前就中止
        """
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError) as e:
            raise ValueError(f"不是合法的 ZIP 文件: {e}")

        max_bytes = PLUGIN_UNPACK_MAX_MB * 1024 * 1024
        with zf:
            infos = zf.infolist()
            if len(infos) > PLUGIN_ZIP_MAX_FILES:
                raise ValueError(
                    f"压缩包内文件数 {len(infos)} 超过上限 {PLUGIN_ZIP_MAX_FILES}")
            # 先用声明的 file_size 预筛一遍：zip bomb 的特征就是声明体积巨大，
            # 这一步能在真正写盘之前就拒掉，比边解边算更早失败。
            declared = sum(int(getattr(i, "file_size", 0) or 0) for i in infos)
            if declared > max_bytes:
                raise ValueError(
                    f"解压后总大小 {declared // (1024*1024)} MB 超过上限 "
                    f"{PLUGIN_UNPACK_MAX_MB} MB")

            work_root = Path(work_dir).resolve()
            written, total = 0, 0
            for info in infos:
                if _zip_member_is_symlink(info):
                    raise ValueError(f"压缩包内含符号链接成员，已拒绝: {info.filename}")
                rel = _zip_safe_relpath(info.filename)
                if rel is None:
                    raise ValueError(f"压缩包内路径不安全，已拒绝: {info.filename!r}")
                target = work_root / rel
                # 第二道防线：归一化过了也要再验一次落点
                try:
                    resolved = target.resolve()
                    inside = (resolved.is_relative_to(work_root)
                              if hasattr(resolved, "is_relative_to")
                              else work_root.parts == resolved.parts[:len(work_root.parts)])
                except OSError:
                    inside = False
                if not inside:
                    raise ValueError(f"压缩包内路径越出解包目录，已拒绝: {info.filename!r}")

                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # 流式写 + 边写边累计：不整块读进内存（单文件也可能很大）
                with zf.open(info, "r") as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(
                                f"解压过程中超过上限 {PLUGIN_UNPACK_MAX_MB} MB，已中止"
                                f"（疑似 zip bomb）")
                        dst.write(chunk)
                written += 1

        root = self._find_plugin_root(work_root)
        if root is None:
            raise ValueError(
                "压缩包里找不到 manifest.json（插件根目录下必须有）。"
                f"已检查 {work_root}")
        return root, {"files": written, "unpackedBytes": total,
                      "declaredBytes": declared}

    @staticmethod
    def _find_plugin_root(work_root):
        """在解包结果里找含 manifest.json 的**最浅**那一层目录（BFS 逐层展开）。

        为什么取最浅：ZIP 里可能同时有 `manifest.json` 与 `docs/sample/manifest.json`，
        取最浅的才是插件本体。

        为什么不用 rglob("*")：市场包（minecraft-bot）里 node_modules 有几千个文件，
        全量遍历每层都跑一遍会明显卡住。逐层 iterdir 只碰需要碰的目录，
        并且**不下探 node_modules**（那里面全是 package.json，也可能有别人的
        manifest.json，下探既慢又会误判）。

        限深 3 层：够覆盖「直接是根」与「包一层 <id>/」两种常见结构，
        再深的基本可以判定这个包不是插件。
        """
        work_root = Path(work_root)
        if (work_root / "manifest.json").is_file():
            return work_root
        frontier = [work_root]
        for _ in range(3):
            nxt = []
            for d in frontier:
                try:
                    entries = sorted(d.iterdir(), key=lambda x: str(x))
                except OSError:
                    continue
                for p in entries:
                    if not p.is_dir() or p.name in ("node_modules", ".git"):
                        continue
                    if (p / "manifest.json").is_file():
                        return p
                    nxt.append(p)
            if not nxt:
                break
            frontier = nxt
        return None

    def import_zip_bytes(self, data, source="upload", expect_sha256=None,
                         enable=False, progress_key=None):
        """从 ZIP 字节安装插件。三条导入通道（上传 / URL / inbox）都调这里。

        返回 (ok: bool, info: dict|str)。校验顺序是**先便宜的、后昂贵的**：
        体积 → sha256 → 解包 → manifest 校验（在 install_from_dir 里）→ 落位。
        sha256 放在解包之前，坏包根本不用碰磁盘。

        ⚠ 不自己做 manifest 校验：install_from_dir 已经做全了（id 形状、apiVersion
        精确等于 1、entry 是裸文件名且存在、覆盖安装先停旧进程、装后校验落位）。
        这里重写一遍只会出现两份不一致的校验逻辑。
        """
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False, "空数据"
        max_zip = PLUGIN_ZIP_MAX_MB * 1024 * 1024
        if len(data) > max_zip:
            return False, (f"压缩包 {len(data) // (1024*1024)} MB 超过上限 "
                           f"{PLUGIN_ZIP_MAX_MB} MB")

        actual_sha = hashlib.sha256(bytes(data)).hexdigest()
        if expect_sha256:
            want = str(expect_sha256).strip().lower()
            if want != actual_sha:
                # 留证：把实际值报出去，用户才知道是「包被篡改」还是「自己填错了 hash」
                if progress_key:
                    _progress_set(progress_key, "failed",
                                  message=f"sha256 不匹配: 期望 {want[:16]}… 实际 {actual_sha[:16]}…")
                return False, {
                    "error": "sha256 校验失败，包内容与预期不符（可能被篡改或 hash 填错）",
                    "expected": want, "actual": actual_sha,
                }
        if progress_key:
            _progress_set(progress_key, "verifying", done=len(data), total=len(data),
                          message=f"sha256 {actual_sha[:12]}…")

        # 临时目录必须在 PLUGIN_TMP_ROOT（= ~/cyrene/_plugin_tmp）下：
        # Termux 的 /tmp 不可写、$PREFIX/tmp 的 SELinux 标签不同也写不了。
        work_dir = None
        try:
            PLUGIN_TMP_ROOT.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="imp_", dir=str(PLUGIN_TMP_ROOT)))
            if progress_key:
                _progress_set(progress_key, "unpacking", message="解包中")
            try:
                root, stats = self._unpack_zip_safe(bytes(data), work_dir)
            except ValueError as e:
                if progress_key:
                    _progress_set(progress_key, "failed", message=str(e))
                return False, str(e)
            except (zipfile.BadZipFile, OSError) as e:
                msg = f"解包失败: {type(e).__name__}: {e}"
                if progress_key:
                    _progress_set(progress_key, "failed", message=msg)
                return False, msg

            if progress_key:
                _progress_set(progress_key, "installing",
                              done=stats["files"], total=stats["files"],
                              message="校验并落位中")
            ok, info = self.install_from_dir(
                root, source=source, sha256=actual_sha,
                enable=enable,
                # 有 expect_sha256 且已通过 = 已验证；上传/inbox 通道通常没有 hash
                allow_unverified=not bool(expect_sha256))
            if not ok:
                if progress_key:
                    _progress_set(progress_key, "failed",
                                  message=info if isinstance(info, str) else str(info))
                return False, info

            if isinstance(info, dict):
                info["sha256"] = actual_sha
                info["verified"] = bool(expect_sha256)
                info["unpacked"] = stats
                # risky 由 install_from_dir 统一扫描并塞进 info（那是所有安装
                # 路径的汇聚点），这里不重复扫 —— 重复扫等于同一个目录走两遍
                # os.walk，白花时间。
            if progress_key:
                _progress_set(progress_key, "done", message=f"已安装 {info.get('id', '')}")
            return True, info
        except OSError as e:
            msg = f"临时目录创建失败: {e}"
            if progress_key:
                _progress_set(progress_key, "failed", message=msg)
            return False, msg
        finally:
            # 无论成败都要清干净：解包出来的东西一次都不该留在磁盘上
            if work_dir is not None:
                shutil.rmtree(str(work_dir), ignore_errors=True)

    def import_zip_file(self, path, source="inbox", expect_sha256=None,
                        enable=False, progress_key=None):
        """从磁盘上的 ZIP 文件安装（inbox 与 URL 下载后都走这里）。

        流式读 + 分块喂 sha256，不把整个文件读进内存两遍 —— minecraft-bot
        是 9.1 MB，读两遍就是 18 MB 常驻，手机内存本来就紧。
        """
        p = Path(path)
        if not p.is_file():
            return False, f"文件不存在: {path}"
        size = p.stat().st_size
        max_zip = PLUGIN_ZIP_MAX_MB * 1024 * 1024
        if size > max_zip:
            return False, (f"压缩包 {size // (1024*1024)} MB 超过上限 "
                           f"{PLUGIN_ZIP_MAX_MB} MB")
        if size <= 0:
            return False, "文件是空的（0 字节）"
        try:
            with open(p, "rb") as fh:
                data = fh.read(max_zip + 1)
        except OSError as e:
            return False, f"读取失败: {e}"
        return self.import_zip_bytes(data, source=source,
                                     expect_sha256=expect_sha256, enable=enable,
                                     progress_key=progress_key)

    def scan_inbox(self):
        """扫 inbox 目录，列出可导入的 ZIP 候选（不安装）。

        顺带尽量读出每个包的插件名与版本，让用户在列表里认得出是哪个插件，
        而不是面对一堆文件名。读不出来就标 unknown，不因为一个坏包让整个扫描失败。
        """
        d = PLUGIN_INBOX_DIR
        if not d.is_dir():
            return {"dir": str(d), "dirExists": False, "items": []}
        items = []
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.suffix.lower() != ".zip":
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            entry = {"filename": f.name, "size": st.st_size,
                     "mtime": int(st.st_mtime), "id": "", "version": "",
                     "name": "", "readable": False, "error": ""}
            try:
                with zipfile.ZipFile(str(f)) as zf:
                    mf_name = None
                    for n in zf.namelist():
                        rel = _zip_safe_relpath(n)
                        if rel is None:
                            continue
                        if rel == "manifest.json" or rel.endswith("/manifest.json"):
                            # 取最浅的那个
                            if mf_name is None or rel.count("/") < mf_name.count("/"):
                                mf_name = rel
                    if mf_name:
                        mf = json.loads(zf.read(mf_name).decode("utf-8", "replace"))
                        if isinstance(mf, dict):
                            entry["id"] = str(mf.get("id") or "")
                            entry["name"] = str(mf.get("name") or "")
                            entry["version"] = str(mf.get("version") or "")
                            entry["readable"] = True
                    else:
                        entry["error"] = "包内没有 manifest.json"
            except (zipfile.BadZipFile, ValueError, OSError, json.JSONDecodeError) as e:
                entry["error"] = f"{type(e).__name__}: {e}"
            entry["oversize"] = st.st_size > PLUGIN_ZIP_MAX_MB * 1024 * 1024
            items.append(entry)
        return {"dir": str(d), "dirExists": True, "items": items}

    def uninstall(self, pid, remove_data=False):
        """卸载插件：停进程 → 摘工具 → 删目录 → 清台账。

        remove_data=False（默认）时保留 data/ 与 secrets ——
        对齐桌面端语义「重新导入新版 ZIP 即可，插件数据（存储、密钥）不会丢失」。
        彻底删除需要用户在面板二次确认后显式传 True。
        """
        if not isinstance(pid, str) or not PLUGIN_ID_RE.match(pid):
            return False, f"非法插件 id: {pid!r}"

        self.stop(pid, keep_registry=False)
        self._unregister_tools(pid)

        dest = PLUGINS_DIR / pid
        removed = False
        if dest.is_dir():
            if remove_data:
                try:
                    shutil.rmtree(str(dest))
                    removed = True
                except OSError as e:
                    return False, f"删除插件目录失败: {e}"
            else:
                # 只删代码，留 data/。先备份 data 再删目录再放回去，
                # 比逐个挑文件删更简单也更不容易漏（插件可能还有别的运行时文件）。
                keep = dest / "data"
                tmp_keep = None
                if keep.is_dir():
                    tmp_keep = PLUGINS_DIR / f".{pid}.data-keep"
                    try:
                        if tmp_keep.exists():
                            shutil.rmtree(str(tmp_keep))
                        shutil.move(str(keep), str(tmp_keep))
                    except OSError:
                        tmp_keep = None      # 搬不动就退化成全删（数据会丢，但卸载能成）
                try:
                    shutil.rmtree(str(dest))
                    removed = True
                except OSError as e:
                    return False, f"删除插件目录失败: {e}"
                if tmp_keep is not None:
                    try:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(tmp_keep), str(dest / "data"))
                    except OSError:
                        pass                 # 恢复失败不阻断卸载，数据仍在 .{pid}.data-keep

        # 清 secrets（remove_data 时才清，否则保留以便重装后继续用）
        if remove_data:
            with SETTINGS_LOCK:
                sec = SETTINGS.get("plugins", {}).get("secrets", {})
                if isinstance(sec, dict) and pid in sec:
                    del sec[pid]
                save_settings_to_disk()
        self._clear_registry(pid)
        with self._lock:
            self._states.pop(pid, None)
            self._errors.pop(pid, None)
            self._manifests.pop(pid, None)
            self._fail_counts.pop(pid, None)
        return True, {"id": pid, "removed": removed, "dataKept": not remove_data}

    # ---------- 台账持久化 ----------

    def _save_registry(self, pid, **fields):
        """更新台账里某个插件的字段并落盘。

        ⚠ 必须持 SETTINGS_LOCK：save_settings_to_disk 写的是整个 SETTINGS，
        两个插件同时启停会互相覆盖（后写的把先写的改动抹掉）。
        """
        with SETTINGS_LOCK:
            reg = SETTINGS.setdefault("plugins", {}).setdefault("registry", {})
            cur = reg.get(pid) if isinstance(reg.get(pid), dict) else {}
            cur.update(fields)
            reg[pid] = cur
            return save_settings_to_disk()

    def _clear_registry(self, pid):
        with SETTINGS_LOCK:
            reg = SETTINGS.setdefault("plugins", {}).setdefault("registry", {})
            if pid in reg:
                del reg[pid]
                return save_settings_to_disk()
        return True

    # ---------- 宿主服务投影（Node → Python 的回调） ----------

    def _build_host_services(self, client):
        """给一个 BridgeClient 装上宿主服务实现。

        这些方法在 client 的**工作线程**里跑（不是 reader 线程），所以可以
        做阻塞 IO，但不能无限阻塞 —— 每个都要有超时或明确的快路径。
        """
        pid = client.plugin_id

        def svc_storage_get(params):
            key = params.get("key")
            if not isinstance(key, str) or not key:
                raise PluginHostError("E_INVALID_ARGUMENT", "storage.get 需要非空 key")
            # Node 侧已有快照缓存，正常不会走到这里；走到说明插件在
            # register 之后又主动查了一次（或快照没覆盖到）。以磁盘为准。
            return plugin_storage_load(pid).get(key)

        def svc_storage_set(params):
            key = params.get("key")
            if not isinstance(key, str) or not key:
                raise PluginHostError("E_INVALID_ARGUMENT", "storage.set 需要非空 key")
            kv = plugin_storage_load(pid)
            kv[key] = params.get("value")
            if not plugin_storage_write(pid, kv):
                raise PluginHostError("E_STORAGE_UNAVAILABLE", "写入 kv.json 失败")
            return {"ok": True}

        def svc_secrets_get(params):
            key = params.get("key")
            if not isinstance(key, str) or not key:
                raise PluginHostError("E_INVALID_ARGUMENT", "secrets.get 需要非空 key")
            # 命名空间隔离：只在本插件的 dict 里找，插件读不到别人的密钥
            return plugin_secrets(pid).get(key)

        def svc_secrets_set(params):
            key = params.get("key")
            if not isinstance(key, str) or not key:
                raise PluginHostError("E_INVALID_ARGUMENT", "secrets.set 需要非空 key")
            with SETTINGS_LOCK:
                sec = SETTINGS.setdefault("plugins", {}).setdefault("secrets", {})
                kv = sec.get(pid) if isinstance(sec.get(pid), dict) else {}
                kv[key] = params.get("value")
                sec[pid] = kv
                ok = save_settings_to_disk()
            if not ok:
                raise PluginHostError("E_STORAGE_UNAVAILABLE", "写入 .config.json 失败")
            return {"ok": True}

        def svc_secrets_delete(params):
            key = params.get("key")
            if not isinstance(key, str) or not key:
                raise PluginHostError("E_INVALID_ARGUMENT", "secrets.delete 需要非空 key")
            with SETTINGS_LOCK:
                sec = SETTINGS.setdefault("plugins", {}).setdefault("secrets", {})
                kv = sec.get(pid) if isinstance(sec.get(pid), dict) else {}
                existed = key in kv
                kv.pop(key, None)
                sec[pid] = kv
                save_settings_to_disk()
            return existed

        def svc_events_emit(params):
            # P2 才接事件总线。这里先收下并记日志，回 ok ——
            # 不能报 E_CAPABILITY_UNAVAILABLE，因为 events.emit 在契约里
            # 是插件自有事件，宿主本来就只是转发，没有「不可用」一说。
            client.log(f"emit {params.get('event')}")
            return {"ok": True}

        def svc_log(params):
            args = params.get("args")
            if isinstance(args, list):
                client.log(" ".join(str(a) for a in args))
            else:
                client.log(str(args))
            return None

        return {
            "storage.get": svc_storage_get,
            "storage.set": svc_storage_set,
            "secrets.get": svc_secrets_get,
            "secrets.set": svc_secrets_set,
            "secrets.delete": svc_secrets_delete,
            "events.emit": svc_events_emit,
            "log": svc_log,
            # 以下在 P2 接真实现。现在回 E_CAPABILITY_UNAVAILABLE 而不是
            # 静默 undefined —— 契约要求插件能靠 code 分支判断。
            "llm.generateText": _plugin_svc_unavailable("llm", "generateText"),
            "llm.runGoal": _plugin_svc_unavailable("llm", "runGoal"),
            "conversations.list": _plugin_svc_unavailable("conversations", "list"),
            "conversations.getMessages": _plugin_svc_unavailable("conversations", "getMessages"),
            "workspace.getBinding": lambda p: None,   # 契约允许返回 null
        }

    # ---------- 启停 ----------

    def start(self, pid):
        """启用插件。返回 (ok: bool, info: dict|str)。

        失败时把插件落到 failed 态并记 lastError，**不抛异常** ——
        一个插件坏了不该让 HTTP 请求 500。
        """
        if not plugins_cfg("enabled", True):
            return False, "插件总开关已关闭（settings.plugins.enabled=false）"

        with self._lock:
            if self._states.get(pid) == "running":
                return True, {"already": True, "tools": list(self._tool_ids.get(pid, []))}
            self._states[pid] = "starting"
            self._errors.pop(pid, None)

        pdir = self.plugin_dir(pid)
        mf = self._read_manifest(pdir)
        if mf is None:
            return self._mark_failed(pid, "manifest.json 缺失或非法（id 需与目录名一致）")

        supported, reason = self.check_support(mf)
        if not supported:
            with self._lock:
                self._states[pid] = "unsupported"
            self._save_registry(pid, enabled=True, lastError=reason,
                                version=str(mf.get("version") or ""))
            return False, reason

        client = BridgeClient(pid, pdir, mf)
        client.host_services = self._build_host_services(client)
        try:
            res = client.start(storage_snapshot=plugin_storage_load(pid))
        except PluginHostError as e:
            client.kill()
            return self._mark_failed(pid, f"{e.code}: {e}")
        except Exception as e:
            client.kill()
            return self._mark_failed(pid, f"{type(e).__name__}: {e}")

        tools = res.get("tools") if isinstance(res.get("tools"), list) else []
        providers = res.get("promptProviders") if isinstance(res.get("promptProviders"), list) else []
        warnings = res.get("warnings") if isinstance(res.get("warnings"), list) else []

        # 把插件工具挂进 TOOLS。冲突处理见 _register_tools。
        added, conflicts = self._register_tools(pid, tools)

        with self._lock:
            self._clients[pid] = client
            self._states[pid] = "running"
            self._manifests[pid] = mf
            self._providers[pid] = providers
            self._fail_counts[pid] = 0

        self._save_registry(pid, enabled=True, lastError="",
                            version=str(mf.get("version") or ""),
                            installedAt=str((plugin_registry().get(pid) or {}).get("installedAt") or ""))

        info = {
            "tools": added,
            "toolCount": len(added),
            "conflicts": conflicts,
            "promptProviders": [p.get("id") for p in providers if isinstance(p, dict)],
            "warnings": list(warnings) + ([f"工具 id 与内置冲突，已跳过: {c}" for c in conflicts] if conflicts else []),
            "pid": client.pid,
            "nodeVersion": client.node_version,
        }
        client.log(f"已启用：注册 {len(added)} 个工具"
                   + (f"，{len(conflicts)} 个 id 冲突被跳过" if conflicts else ""))
        return True, info

    def _mark_failed(self, pid, message):
        with self._lock:
            self._states[pid] = "failed"
            self._errors[pid] = message
        self._save_registry(pid, lastError=message)
        return False, message

    def _register_tools(self, pid, tools):
        """插件工具 → TOOLS / TOOL_HANDLERS。返回 (added_ids, conflict_ids)。

        冲突策略：跳过冲突的那个工具，但**不拒整个插件**。理由是一个插件
        可能注册 5 个工具，只有 1 个撞名，因为 1 个把 5 个全废掉太粗暴；
        跳过 + 在 warnings 里报出来，用户和插件作者都能看到。
        """
        added, conflicts = [], []
        with self._lock:
            for t in tools:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id")
                if not isinstance(tid, str) or not tid:
                    continue
                # 工具 id 不套 PLUGIN_ID_RE：桌面端插件的 id 形如
                # "weather-tool_query"（插件id + 下划线 + 工具名），
                # 而 PLUGIN_ID_RE 是给「会拼进文件路径」的插件 id 用的。
                # 工具 id 只进 TOOLS 字典与 FC schema，不落盘，形状约束
                # 交给端点的 schema 校验兜住比在这里猜规则更可靠。
                # 但仍要挡掉会破坏协议的值：空白与超长。
                if tid != tid.strip() or len(tid) > 64 or "\n" in tid:
                    conflicts.append(f"{tid}(id 非法，已跳过)")
                    continue
                if tid in BUILTIN_TOOL_IDS:
                    conflicts.append(tid)
                    continue
                if t.get("enabled") is False:
                    continue          # 插件自己声明不启用，不占 TOOLS
                TOOLS[tid] = plugin_tool_to_spec(pid, t)
                added.append(tid)
            self._tool_ids[pid] = added
            # 工具开关默认值：新工具按「启用」入库，用户可在面板单独关
            tools_cfg = SETTINGS.setdefault("tools", {})
            for tid in added:
                tools_cfg.setdefault(tid, True)
        return added, conflicts

    def _unregister_tools(self, pid):
        """从 TOOLS 摘掉某插件的全部工具。停用/卸载/崩溃都要走这里。

        不摘的后果：TOOLS 里留着一个 handler 指向已死进程的条目，
        模型调用它会拿到 E_PLUGIN_STOPPING，而且 /tools 面板永远显示
        一个关不掉的幽灵工具。
        """
        with self._lock:
            ids = self._tool_ids.pop(pid, [])
            for tid in ids:
                TOOLS.pop(tid, None)
            self._providers.pop(pid, None)
            return list(ids)

    def stop(self, pid, keep_registry=True, set_disabled=True):
        """停用插件。返回 (ok, info)。幂等：已经停着的直接返回 ok。

        keep_registry  台账条目要不要留（uninstall 传 False = 连条目一起删）
        set_disabled   留台账时要不要把 enabled 写成 False。
                       ⚠ 这两个参数必须分开：调用方的意图是相反的。
                         · disable 端点 —— 用户明确要求停用，enabled=False 是对的。
                         · shutdown_all（atexit / SIGTERM）—— 宿主自己要退了，
                           跟用户的启用意图无关，必须原样保留 enabled=True，
                           否则重启后 plugin_boot_async 看到 False 就不会拉起，
                           用户装的插件「重启一次就再也起不来」，且面板上
                           显示的是「已停用」，看起来像用户自己关的，极难排查。
                       早期版本这里写死 enabled=False，注释还写着「保留 enabled
                       意图」—— 代码与注释相反，是个真 bug，已在 P1-7 真机实测暴露
                       （install_from_dir(enable=True) 后台账内存是 true、
                       经 shutdown_all 落盘变成 false）。
        """
        with self._lock:
            client = self._clients.pop(pid, None)
            state = self._states.get(pid)
            if client is None and state in (None, "installed", "not_installed"):
                return True, {"already": True}
            self._states[pid] = "stopping"

        errors = []
        if client is not None:
            try:
                errors = client.dispose() or []
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
            finally:
                # dispose 失败也必须收干净：进程、线程池、管道
                client._shutdown()

        removed = self._unregister_tools(pid)

        with self._lock:
            self._states[pid] = "installed"
            self._errors.pop(pid, None)
        if keep_registry:
            if set_disabled:
                self._save_registry(pid, enabled=False, lastError="")
            else:
                # 宿主退场：只清 lastError（上次跑成功结束的，不算错误），
                # enabled 一个字都不碰。
                self._save_registry(pid, lastError="")
        return True, {"unregisteredTools": removed, "disposeErrors": errors}

    def logs(self, pid, limit=None):
        """某插件的宿主日志环 + 当前状态。GET /plugins/<id>/logs 用。

        日志只活在 BridgeClient 的内存环里（进程一死就没了），所以 state 与
        error 必须一起回传 —— 否则用户看到空日志会以为「插件没输出」，
        而真相是「插件早就死了」。logsLost 把这个区别显式说出来。
        """
        with self._lock:
            client = self._clients.get(pid)
            state = self._states.get(pid, "not_installed")
            error = self._errors.get(pid, "")
        try:
            n = int(limit) if limit else PLUGIN_LOG_RING
        except (TypeError, ValueError):
            n = PLUGIN_LOG_RING
        n = max(1, min(n, PLUGIN_LOG_RING))
        return {
            "id": pid,
            "state": state,
            "error": error,
            "logs": client.get_logs(n) if client is not None else [],
            "logsLost": client is None,
        }

    def patrol(self):
        """对所有 running 插件做一轮崩溃巡检。返回被判定 crashed 的 id 列表。

        手机上不开常驻轮询线程（多一份线程就是多一份电），改由两个时机按需触发：
        工具执行失败路径（crash_check）与面板查询（GET /plugins）。
        这里只是把「逐个 crash_check」收成一次调用，方便面板一次性刷新真相。
        """
        with self._lock:
            pids = [pid for pid, st in self._states.items() if st == "running"]
        return [pid for pid in pids if self.crash_check(pid) == "crashed"]

    def crash_check(self, pid):
        """巡检：进程死了但状态还是 running → 标 crashed 并摘工具。

        由 /plugins 查询与工具执行失败路径触发。不做常驻轮询线程 ——
        手机上多一个常驻线程就是多一份电，按需检查足够。
        """
        with self._lock:
            client = self._clients.get(pid)
            if client is None:
                return None
            if self._states.get(pid) != "running":
                return self._states.get(pid)
            alive = (client.proc is not None and client.proc.poll() is None
                     and not client.dead)
            if alive:
                return "running"
            # 确认死了
            self._clients.pop(pid, None)
            self._states[pid] = "crashed"
            reason = client.dead_reason or f"进程退出码 {client.proc.poll() if client.proc else '?'}"
            self._errors[pid] = reason
        self._unregister_tools(pid)
        self._save_registry(pid, lastError=f"进程崩溃: {reason}")
        return "crashed"

    def execute_tool(self, pid, tid, args, tool_ctx):
        """跑一个插件工具。返回 (outcome, text)，与 handler 型工具同构。

        这一层负责把桥接的异常翻译成四态 outcome —— run_tool 只认这个。
        """
        with self._lock:
            client = self._clients.get(pid)
        if client is None:
            return OUTCOME_NOT_EXECUTED, f"(插件 {pid} 未运行)"

        timeout = float(agent_cfg("stepTimeout") or 30)
        # 插件工具可能自己发网络请求（weather-tool 就是），stepTimeout 是
        # 给 termux-api 广播定的，对网络型插件偏紧。给到 3 倍但不超过 120s。
        timeout = min(max(timeout * 3, 30.0), 120.0)
        try:
            res = client.call("tool.execute", {
                "toolId": tid,
                "args": args if isinstance(args, dict) else {},
                "ctx": tool_ctx if isinstance(tool_ctx, dict) else {},
            }, timeout=timeout)
        except PluginHostError as e:
            if e.code == "E_PLUGIN_STOPPING":
                self.crash_check(pid)
                return OUTCOME_NOT_EXECUTED, f"(插件 {pid} 已停止: {e})"
            # 超时归 unknown：插件可能已经产生了副作用（发了请求、写了文件），
            # 报 failure 会让模型以为「没生效」而重放，那才是真危险。
            if "超时" in str(e):
                return OUTCOME_UNKNOWN, f"(插件工具超时 {timeout:.0f}s: {e})"
            return OUTCOME_FAILURE, f"(插件工具失败 {e.code}: {e})"
        except Exception as e:
            return OUTCOME_FAILURE, f"(插件工具异常: {type(e).__name__}: {e})"

        text = res.get("text") if isinstance(res, dict) else None
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        return OUTCOME_SUCCESS, text

    def invoke_ipc(self, pid, channel, args=None, timeout=20.0):
        """面板 UI 调插件注册的 IPC handler，返回原始结果。

        与 execute_tool 分工不同：那条是「模型调插件工具」，要把结果翻译成
        四态 outcome；这条是「插件自己的面板拉数据」，结果直接回给前端，不
        经过模型。Node 侧 doIpcInvoke 早就写好了，这里只把它接到 HTTP 面上。
        """
        with self._lock:
            client = self._clients.get(pid)
        if client is None:
            raise PluginHostError("E_PLUGIN_STOPPING", f"插件 {pid} 未运行")
        res = client.call("ipc.invoke", {
            "channel": channel,
            "args": args if isinstance(args, list) else [],
        }, timeout=timeout)
        return res.get("result") if isinstance(res, dict) else None

    def shutdown_all(self):
        """宿主退出时收干净全部子进程。atexit 与 SIGTERM 都要挂。

        不做这一步的后果：Python 服务退了，node 子进程变孤儿常驻，
        手机内存被一点点吃光，而且下次启动端口/资源可能撞车。
        """
        with self._lock:
            pids = list(self._clients.keys())
        for pid in pids:
            try:
                # set_disabled=False：宿主退场不改用户的启用意图，
                # 重启后 plugin_boot_async 才能按台账把它们重新拉起。
                # （这里曾经写死 enabled=False，导致「重启一次插件就再也起不来」，
                #  详见 stop() 的 docstring。）
                self.stop(pid, keep_registry=True, set_disabled=False)
            except Exception:
                with self._lock:
                    c = self._clients.pop(pid, None)
                if c is not None:
                    c._shutdown()


def _plugin_svc_unavailable(cap, method):
    """造一个「按契约报 E_CAPABILITY_UNAVAILABLE」的服务占位实现。

    为什么不用 lambda 直接返回 None：契约明确插件靠 isPluginHostError(e.code)
    分支，静默返回 undefined 会让插件以为调用成功，走进错误路径 ——
    比如 long-term-memory 拿到 undefined 的会话列表，会以为「没有历史」
    然后把记忆清空。抛带 code 的错才是诚实的。
    """
    def _svc(params):
        raise PluginHostError(
            "E_CAPABILITY_UNAVAILABLE",
            f'宿主能力 "{cap}" 在手机端未实现（调用 {cap}.{method}）')
    return _svc


# 内置工具 id 的冻结快照。插件工具注册时要拿它查重，防止插件覆盖内置工具
# （比如注册一个叫 "shell" 的工具把真的 shell 顶掉 —— 那是提权）。
# 必须在 TOOLS 定义之后、任何插件启动之前取，所以放在这里模块级求值。
BUILTIN_TOOL_IDS = frozenset(TOOLS.keys())

PLUGIN_MANAGER = PluginManager()


# ========== 插件市场（P5） ==========
#
# 源仓库 Playa-Cyrene/Cyrene-Plugins 的结构：
#   registry.json          {apiVersion, updatedAt, plugins:[{id,name,description,
#                           author,homepage,version,zip,sha256,downloads}]}
#   plugins/<id>/          源码目录（manifest.json + index.cjs + README.md …）
#   marketplace/<id>.json  详情弹窗数据（只有 4 个插件有）
#   zips/<id>-<ver>.zip    分发包
#
# 两个已核实的现实约束，代码里都要照顾到：
#   ① registry.json 只登记 13 个**带 ZIP** 的插件；weather-tool 等四个官方示例
#      标注「示例，不分发」，没有 ZIP —— 装它们只能走 install_from_source
#      （逐个拉 plugins/<id>/ 下的文件）。
#   ② registry 里的 zip 字段是**绝对直链且全部指向 Gitee**。所以主源用 Gitee
#      时直链原样可用；一旦回退到 GitHub，就得把直链的 Gitee 前缀换成 GitHub
#      base，否则「列表从 GitHub 来、下载却打 Gitee」，回退等于没回退。

MARKET_CACHE = {"at": 0.0, "source": "", "base": "", "data": None, "error": ""}
MARKET_CACHE_LOCK = threading.Lock()


def _assert_http_url(url):
    """只放行 http/https，其余协议一律拒。

    与 _net_open 开头那段检查是同一套白名单。这里单独写一份是因为市场下载
    要**流式落盘**（_net_open 会整块读进内存，32 MB 上限的包在手机上是负担），
    不能直接复用它；但 SSRF 防线必须一模一样，所以照抄语义并显式标注。
    """
    if not isinstance(url, str) or not url.strip():
        raise NetError("(URL 为空)")
    scheme = urlparse(url.strip()).scheme.lower()
    if scheme not in ("http", "https"):
        raise NetError(f"(只支持 http/https，拒绝 {scheme or '(无协议)'}:// —— "
                       f"file/ftp 等协议可能读取本地文件)")
    return url.strip()


def _ver_tuple(v):
    """把版本字符串拆成可比较的元组。脏值不抛异常，返回全 0。

    市场里版本号并不总是严格 SemVer（可能有 -beta、v 前缀），所以只做
    「尽力解析」：解析不出来的段当 0。比较结果只用于决定要不要显示
    「有更新」徽标，判错了顶多是徽标不准，不会造成数据损坏。
    """
    s = str(v or "").strip().lstrip("vV")
    s = re.split(r"[-+]", s, 1)[0]
    out = []
    for part in s.split("."):
        m = re.match(r"^(\d+)", part)
        out.append(int(m.group(1)) if m else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _market_zip_url(zip_field, base, source_name):
    """把 registry 里的 zip 字段解析成当前源下真正能下的 URL。

    绝对直链指向 Gitee 而当前源是 GitHub 时，改写 host+路径前缀；
    其余情况（相对路径、或直链本来就属于当前源）按需拼接。
    """
    z = str(zip_field or "").strip()
    if not z:
        return ""
    if not z.startswith("http"):
        return base.rstrip("/") + "/" + z.lstrip("/")
    if source_name == "github" and "gitee.com" in z:
        # 取最后的 zips/<file> 部分，接到 GitHub base 上
        tail = z.split("/raw/", 1)[-1]
        tail = tail.split("/", 1)[-1] if "/" in tail else tail
        if not tail.startswith("zips/"):
            tail = "zips/" + tail.rsplit("/", 1)[-1]
        return base.rstrip("/") + "/" + tail
    if source_name == "gitee" and "raw.githubusercontent.com" in z:
        tail = z.split("/main/", 1)[-1]
        if not tail.startswith("zips/"):
            tail = "zips/" + tail.rsplit("/", 1)[-1]
        return base.rstrip("/") + "/" + tail
    return z


def _market_fetch_registry(force_refresh=False):
    """拉 registry.json，带 5 分钟缓存与双源回退。

    返回 dict：{ok, source, base, fellBack, updatedAt, plugins[], error, fromCache}

    回退策略：主源（Gitee）失败才试回退源（GitHub）；两个都失败时，
    如果缓存里有旧数据就**返回旧数据并标注 stale**——手机上网络本来就飘，
    让面板直接白屏不如显示 5 分钟前的列表并告诉用户「这是缓存」。
    """
    now = time.time()
    with MARKET_CACHE_LOCK:
        fresh = (MARKET_CACHE["data"] is not None
                 and (now - MARKET_CACHE["at"]) < PLUGIN_MARKET_TTL)
        if fresh and not force_refresh:
            cached = MARKET_CACHE["data"]
            out = dict(cached)
            out["fromCache"] = True
            out["cacheAge"] = int(now - MARKET_CACHE["at"])
            return out

    errors = []
    for idx, (name, base) in enumerate(PLUGIN_MARKET_SOURCES):
        url = base.rstrip("/") + "/registry.json"
        try:
            _st, _hdr, body, truncated = _net_open(
                url, PLUGIN_MARKET_MAX_BYTES, timeout=PLUGIN_MARKET_TIMEOUT)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        if truncated:
            errors.append(f"{name}: registry.json 超过 {PLUGIN_MARKET_MAX_BYTES // (1024*1024)} MB，已拒收")
            continue
        try:
            raw = json.loads(body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            errors.append(f"{name}: registry.json 解析失败 {type(e).__name__}")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{name}: registry.json 顶层不是对象")
            continue
        api = raw.get("apiVersion")
        if api != PLUGIN_API_VERSION:
            # 整份拒收：apiVersion 不匹配说明市场契约变了，字段含义可能全变，
            # 硬着头皮解析会给用户看一份错乱的列表，比报错更糟。
            errors.append(f"{name}: 市场 apiVersion={api}，本宿主只支持 {PLUGIN_API_VERSION}")
            continue
        items = raw.get("plugins")
        if not isinstance(items, list):
            errors.append(f"{name}: registry.plugins 不是数组")
            continue
        plugins = []
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("id") or "").strip()
            # id 会拼进文件路径与 URL，形状不对的直接丢（与本地台账同一把尺）
            if not PLUGIN_ID_RE.match(pid):
                continue
            plugins.append({
                "id": pid,
                "name": _as_str(it.get("name"), pid)[:80],
                "description": _as_str(it.get("description"), "")[:600],
                "author": _as_str(it.get("author"), "")[:80],
                "homepage": _as_str(it.get("homepage"), "")[:300],
                "version": _as_str(it.get("version"), "")[:40],
                "zip": _as_str(it.get("zip"), "")[:600],
                "sha256": _as_str(it.get("sha256"), "").strip().lower()[:64],
                "downloads": it.get("downloads") if isinstance(it.get("downloads"), int) else 0,
                "zipUrl": _market_zip_url(it.get("zip"), base, name),
            })
        data = {
            "ok": True, "source": name, "base": base, "fellBack": idx > 0,
            "updatedAt": _as_str(raw.get("updatedAt"), "")[:40],
            "plugins": plugins, "error": "", "fromCache": False, "stale": False,
        }
        with MARKET_CACHE_LOCK:
            MARKET_CACHE.update({"at": now, "source": name, "base": base,
                                 "data": data, "error": ""})
        return data

    # 所有源都失败：能给缓存就给缓存，并明说是旧的
    with MARKET_CACHE_LOCK:
        if MARKET_CACHE["data"] is not None:
            out = dict(MARKET_CACHE["data"])
            out.update({"stale": True, "fromCache": True,
                        "cacheAge": int(now - MARKET_CACHE["at"]),
                        "error": "所有市场源都不可达，显示的是缓存： " + " | ".join(errors)})
            return out
    return {"ok": False, "source": "", "base": "", "fellBack": False,
            "updatedAt": "", "plugins": [], "stale": False, "fromCache": False,
            "error": "所有市场源都不可达： " + " | ".join(errors)}


def market_overview(force_refresh=False):
    """市场列表 + 与本地已装状态合并成三态。

    三态：not_installed / installed / update_available。
    合并在**这一层**做而不是前端做：前端要同时拿到本地台账与 registry 版本
    才能比，而本地台账里有 sha256、source 等不该随便撒给前端的细节，
    在后端算完只回一个状态字符串最省事也最安全。
    """
    reg = _market_fetch_registry(force_refresh)
    if not reg.get("ok"):
        return reg
    local = plugin_registry()
    installed = PLUGIN_MANAGER.scan_installed()
    out_items = []
    for p in reg["plugins"]:
        pid = p["id"]
        item = dict(p)
        le = local.get(pid) if isinstance(local.get(pid), dict) else {}
        has_dir = pid in installed
        item["localInstalled"] = bool(has_dir)
        item["localVersion"] = _as_str(le.get("version"), "")[:40]
        item["localEnabled"] = bool(le.get("enabled"))
        item["localSource"] = _as_str(le.get("source"), "")[:20]
        if not has_dir and not le:
            item["status"] = "not_installed"
        elif item["version"] and item["localVersion"] and \
                _ver_tuple(item["version"]) > _ver_tuple(item["localVersion"]):
            item["status"] = "update_available"
        else:
            item["status"] = "installed"
        item["state"] = PLUGIN_MANAGER.state(pid)
        out_items.append(item)
    result = dict(reg)
    result["plugins"] = out_items
    result["counts"] = {
        "total": len(out_items),
        "installed": sum(1 for i in out_items if i["status"] == "installed"),
        "updateAvailable": sum(1 for i in out_items if i["status"] == "update_available"),
        "notInstalled": sum(1 for i in out_items if i["status"] == "not_installed"),
    }
    return result


def _market_find(pid, force_refresh=False):
    """在 registry 里找一个插件条目。返回 (entry|None, reg_dict)。"""
    reg = _market_fetch_registry(force_refresh)
    if not reg.get("ok"):
        return None, reg
    for p in reg["plugins"]:
        if p["id"] == pid:
            return p, reg
    return None, reg


def market_detail(pid):
    """插件详情：优先 marketplace/<id>.json，没有则回落 plugins/<id>/README.md。

    registry 里只有 4 个插件有 marketplace 详情文件，其余的详情就是 README。
    两次网络请求都可能失败，失败不抛，返回带 error 的结构让面板自己显示。
    """
    entry, reg = _market_find(pid)
    if entry is None:
        return {"ok": False, "id": pid,
                "error": reg.get("error") or f"市场里没有这个插件: {pid}"}
    base = reg["base"]
    out = {"ok": True, "id": pid, "entry": entry,
           "source": reg["source"], "detail": None, "readme": "",
           "detailKind": "none", "error": ""}

    url = base.rstrip("/") + f"/marketplace/{pid}.json"
    try:
        _st, _h, body, _tr = _net_open(url, PLUGIN_MARKET_MAX_BYTES,
                                       timeout=PLUGIN_MARKET_TIMEOUT)
        raw = json.loads(body.decode("utf-8", "replace"))
        if isinstance(raw, dict) and raw:
            out["detail"] = raw
            out["detailKind"] = "marketplace"
    except Exception:
        pass        # 没有详情文件是常态（13 个里只有 4 个有），不算错误

    if out["detail"] is None:
        url2 = base.rstrip("/") + f"/plugins/{pid}/README.md"
        try:
            _st, _h, body, _tr = _net_open(url2, 512 * 1024,
                                           timeout=PLUGIN_MARKET_TIMEOUT)
            text = body.decode("utf-8", "replace")
            if text.strip():
                out["readme"] = text[:200000]
                out["detailKind"] = "readme"
        except Exception as e:
            out["error"] = f"详情与 README 都取不到: {e}"
    return out


def _market_download_stream(url, dest_path, max_bytes, progress_key=None):
    """流式下载 url 到 dest_path，边下边算 sha256 与报进度。返回 (sha256, size)。

    为什么不用 _net_open：它把整个 body 读进内存。市场里 minecraft-bot 是
    9.1 MB，上限是 32 MB —— 在已经跑着若干个 node 子进程（每个 30-50 MB）的
    手机上，再叠一个 32 MB 的 bytes 对象是实打实的 OOM 风险。流式写盘让内存
    占用恒定在 chunk 大小。

    超限处理：读到 max_bytes+1 就中止并删掉半成品，抛 ValueError。
    """
    url = _assert_http_url(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": NET_UA,
        "Accept": "*/*",
    })
    h = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=PLUGIN_MARKET_TIMEOUT) as r:
            declared = 0
            try:
                declared = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                declared = 0
            if progress_key:
                _progress_set(progress_key, "downloading", 0, declared, "开始下载")
            with open(dest_path, "wb") as fh:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(
                            f"下载超过上限 {max_bytes // (1024*1024)} MB，已中止")
                    h.update(chunk)
                    fh.write(chunk)
                    if progress_key:
                        _progress_set(progress_key, "downloading", total, declared,
                                      f"已下载 {total // 1024} KB")
    except urllib.error.HTTPError as e:
        raise NetError(f"(HTTP {e.code} 错误：{e.reason})")
    except urllib.error.URLError as e:
        raise NetError(f"(连不上目标：{getattr(e, 'reason', e)})")
    except ValueError:
        raise
    except Exception as e:
        raise NetError(f"(下载失败：{type(e).__name__}: {e})")
    return h.hexdigest(), total


def market_install(pid, enable=False, progress_key=None, force_refresh=False):
    """从市场装插件：下载 ZIP → sha256 强校验 → 走 P6 导入内核。

    sha256 是**强校验**：registry 里有值就必须匹配，不匹配直接拒装并留证。
    这条不能松——市场是远端内容，中间人换一个包，用户看到的就是「我装的
    明明是官方插件」。registry 里没有 sha256 时才允许 unverified，并显式标注。
    """
    if not PLUGIN_ID_RE.match(pid or ""):
        return False, f"非法插件 id: {pid!r}"
    entry, reg = _market_find(pid, force_refresh)
    if entry is None:
        return False, reg.get("error") or f"市场里没有这个插件: {pid}"
    zip_url = entry.get("zipUrl") or ""
    if not zip_url:
        return False, (f"插件 {pid} 在 registry 里没有 zip 字段（可能是「示例，不分发」"
                       f"的插件）。这类插件请改用 install-from-source 通道。")

    max_bytes = PLUGIN_ZIP_MAX_MB * 1024 * 1024
    tmp_file = None
    try:
        PLUGIN_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="mkt_", suffix=".zip",
                                        dir=str(PLUGIN_TMP_ROOT))
        os.close(fd)
        tmp_file = Path(tmp_name)
        if progress_key:
            _progress_set(progress_key, "downloading", 0, 0, f"下载 {pid}")
        try:
            actual_sha, size = _market_download_stream(
                zip_url, tmp_file, max_bytes, progress_key=progress_key)
        except (NetError, ValueError) as e:
            if progress_key:
                _progress_set(progress_key, "failed", message=str(e))
            return False, f"下载失败 {e}"

        expect = entry.get("sha256") or ""
        if expect and expect.lower() != actual_sha:
            msg = (f"sha256 校验失败：registry 声明 {expect[:16]}…，"
                   f"实际下到 {actual_sha[:16]}…。包可能被篡改或源不同步，已拒装。")
            if progress_key:
                _progress_set(progress_key, "failed", message=msg)
            # 留证：把两个值都写进宿主日志，事后能查是哪一边的问题
            print(f"插件市场: {msg} (url={zip_url} size={size})")
            return False, {"error": msg, "expected": expect, "actual": actual_sha,
                           "url": zip_url, "size": size}

        ok, info = PLUGIN_MANAGER.import_zip_file(
            tmp_file, source="market", expect_sha256=expect or None,
            enable=enable, progress_key=progress_key)
        if ok and isinstance(info, dict):
            info["market"] = {"source": reg.get("source"), "version": entry.get("version"),
                              "verified": bool(expect), "downloads": entry.get("downloads")}
        return ok, info
    except OSError as e:
        if progress_key:
            _progress_set(progress_key, "failed", message=str(e))
        return False, f"临时文件创建失败: {e}"
    finally:
        if tmp_file is not None:
            try:
                tmp_file.unlink()
            except OSError:
                pass


def market_install_from_source(pid, enable=False, progress_key=None,
                               force_refresh=False):
    """从市场的**源码目录**装插件（逐个拉 plugins/<id>/ 下的文件）。

    这是装 weather-tool / long-term-memory / scheduled-automation /
    local-asr-contract 这四个「示例，不分发」插件的**唯一途径**——它们在
    registry 里没有 zip 字段。

    没有 sha256 可校验，所以响应标 unverified=True，前端要二次确认。

    文件清单靠 GitHub/Gitee 的目录 API 拿不到（两个源的 API 形状不同，
    且 raw 端点不提供列目录能力），所以这里按 manifest 声明的 entry 加上
    一组约定文件名去试。拉不到的就跳过，最后由 install_from_dir 校验
    「manifest.json 与 entry 必须都在」来兜底。
    """
    if not PLUGIN_ID_RE.match(pid or ""):
        return False, f"非法插件 id: {pid!r}"
    entry, reg = _market_find(pid, force_refresh)
    if entry is None:
        return False, reg.get("error") or f"市场里没有这个插件: {pid}"
    base = reg["base"].rstrip("/")

    work_dir = None
    try:
        PLUGIN_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="src_", dir=str(PLUGIN_TMP_ROOT)))

        # 第一步：manifest.json 是必须的，拿不到就直接失败
        mf_path = work_dir / "manifest.json"
        try:
            _st, _h, body, _tr = _net_open(
                f"{base}/plugins/{pid}/manifest.json", 512 * 1024,
                timeout=PLUGIN_MARKET_TIMEOUT)
        except Exception as e:
            if progress_key:
                _progress_set(progress_key, "failed", message=f"取不到 manifest.json: {e}")
            return False, f"取不到 plugins/{pid}/manifest.json: {e}"
        mf_path.write_bytes(body)
        try:
            mf = json.loads(body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError) as e:
            return False, f"manifest.json 解析失败: {e}"
        if not isinstance(mf, dict):
            return False, "manifest.json 顶层不是对象"

        if progress_key:
            _progress_set(progress_key, "downloading", 1, 0, "拉取源码文件")

        # 第二步：entry 必须拉得到
        entry_name = str(mf.get("entry") or "")
        wanted = []
        if entry_name and "/" not in entry_name and "\\" not in entry_name \
                and ".." not in entry_name:
            wanted.append(entry_name)
        # 约定文件名：市场里的插件基本都有这些（有就拉，404 就跳过）
        for nm in ("index.cjs", "README.md", "ui.html", "icon.png", "package.json"):
            if nm not in wanted:
                wanted.append(nm)

        got = []
        for nm in wanted:
            dest = work_dir / nm
            try:
                _st, _h, body2, _tr = _net_open(
                    f"{base}/plugins/{pid}/{nm}", 8 * 1024 * 1024,
                    timeout=PLUGIN_MARKET_TIMEOUT)
            except Exception:
                continue        # 拉不到就跳过，最后由 install_from_dir 兜底校验
            # 落点必须在 work_dir 内（nm 已过滤过分隔符与 ..，这里再验一次）
            try:
                if not dest.resolve().is_relative_to(work_dir.resolve()):
                    continue
            except (OSError, AttributeError):
                continue
            try:
                dest.write_bytes(body2)
                got.append(nm)
            except OSError:
                continue

        if entry_name and entry_name not in got:
            msg = f"入口文件 {entry_name} 拉取失败，无法安装"
            if progress_key:
                _progress_set(progress_key, "failed", message=msg)
            return False, msg

        if progress_key:
            _progress_set(progress_key, "installing", len(got), len(got), "校验并落位")
        ok, info = PLUGIN_MANAGER.install_from_dir(
            work_dir, source="market-src", sha256="", enable=enable,
            allow_unverified=True)
        if ok and isinstance(info, dict):
            info["unverified"] = True
            info["fetchedFiles"] = got
            info["market"] = {"source": reg.get("source"),
                              "version": entry.get("version")}
            if progress_key:
                _progress_set(progress_key, "done", message=f"已安装 {pid}（未经 sha256 校验）")
        elif progress_key:
            _progress_set(progress_key, "failed",
                          message=info if isinstance(info, str) else str(info))
        return ok, info
    except OSError as e:
        if progress_key:
            _progress_set(progress_key, "failed", message=str(e))
        return False, f"临时目录创建失败: {e}"
    finally:
        if work_dir is not None:
            shutil.rmtree(str(work_dir), ignore_errors=True)


def plugin_boot_async():
    """宿主启动后在后台线程拉起 enabled=true 的插件。

    为什么必须异步：起一个插件要 spawn node + 等 host.ready + 跑 register，
    实测 1-3 秒（node 冷启动占大头）。放在 main() 主流程里做，
    /health 就要等所有插件起完才返回 —— 而守护器判定服务健康的超时是固定的，
    插件多起来会直接被判死重启，进入循环。

    失败隔离：单个插件起不来只落它自己的 failed 态并记 lastError，
    不影响其他插件，更不影响 HTTP 服务。用户能在面板看到原因并重试。
    """
    if not plugins_cfg("enabled", True):
        print("插件: 总开关已关闭，跳过加载")
        return

    def _boot():
        reg = plugin_registry()
        want = [pid for pid, e in reg.items() if isinstance(e, dict) and e.get("enabled")]
        if not want:
            print("插件: 无已启用插件")
            return
        installed = PLUGIN_MANAGER.scan_installed()
        ok_n = 0
        for pid in want:
            if pid not in installed:
                # 台账说启用了，但目录没了（用户手删 / 部署没带上）。
                # 落 not_installed 并记原因 —— 这不是插件坏了，是文件不在，
                # 标 failed 会误导用户去「重试」，而重试永远不可能成功。
                with PLUGIN_MANAGER._lock:
                    PLUGIN_MANAGER._states[pid] = "not_installed"
                    PLUGIN_MANAGER._errors[pid] = "插件目录不存在（台账残留）"
                continue
            good, info = PLUGIN_MANAGER.start(pid)
            if good:
                ok_n += 1
                n = info.get("toolCount", 0) if isinstance(info, dict) else 0
                print(f"  ✓ 插件 {pid} 已启用（{n} 个工具）")
            else:
                print(f"  ⚠ 插件 {pid} 启用失败: {info}")

        # ⚠ 必须重建系统提示：SYSTEM_PROMPTS 是模块导入期就构建好的文本快照
        # （见文件里 `SYSTEM_PROMPTS = {m: build_system_prompt(m) for m in MODES}`），
        # 而插件是在 main() 之后、由本线程异步注册进 TOOLS 的。不重建的话
        # 「=== 可用工具 ===」清单里永远没有插件工具 —— FC schema 每轮现算所以
        # 工具仍能调通，但提示明写着「只能用上面列出的工具名」，模型会因此拒调。
        if ok_n:
            try:
                chars = rebuild_system_prompt()
                print(f"  提示词已重建: " +
                      "  ".join(f"{m}={n}" for m, n in chars.items()))
            except Exception as e:
                print(f"  ⚠ 提示词重建失败: {type(e).__name__}: {e}")

        print(f"插件: {ok_n}/{len(want)} 启用成功")

    t = threading.Thread(target=_boot, name="plugin-boot", daemon=True)
    t.start()


# 宿主退出时收干净 node 子进程。不做这一步的后果：Python 服务退了，
# node 变孤儿常驻，手机内存被一点点吃光（每个 ~30-50MB），
# 而且下次启动可能与残留进程抢资源。
#
# 除了 atexit 还要挂 SIGTERM：守护器与 /service/stop 都是用信号停服务的，
# 而 atexit 在收到未处理的 SIGTERM 时**不会执行**（默认动作是立即终止）。
def _plugin_atexit():
    try:
        PLUGIN_MANAGER.shutdown_all()
    except Exception:
        pass


atexit.register(_plugin_atexit)


def _plugin_on_sigterm(signum, frame):
    """收到 SIGTERM 时先收插件，再按默认语义退出（这样 atexit 也会跑）。"""
    try:
        PLUGIN_MANAGER.shutdown_all()
    except Exception:
        pass
    # 交回默认处理：sys.exit 会触发 atexit，_plugin_atexit 再跑一次是幂等的
    # （shutdown_all 对已停的插件直接返回 already）。
    sys.exit(0)


try:
    signal.signal(signal.SIGTERM, _plugin_on_sigterm)
except (ValueError, OSError, AttributeError):
    # ValueError: 不在主线程注册信号（单元测试导入时会遇到）
    # 注册失败不影响主功能，只是 SIGTERM 时靠 node 自己检测父进程消失后退出
    pass


def run_tool(name, args_dict=None, ctx=None):
    """执行一个工具，返回结构化结果 dict：

        {"tool", "args", "outcome", "result", "ms"}

    args_dict 是命名参数字典。两套协议（原生 FC / 旧文本）都在调用前归一到
    这个形态，因此执行层只有这一份实现。

    ctx 是可选的调用上下文（目前只有 {"sid": 会话 id}）。needs_ctx=True 的
    handler（update_todo/ask_user）会收到它作第二个参数 —— 因为 dispatch_tools
    用线程池并发，threading.local / contextvars 都传不进子线程，只能显式传参。
    """
    args_dict = args_dict if isinstance(args_dict, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    t0 = time.time()

    def done(outcome, result):
        return {"tool": name, "args": args_dict, "outcome": outcome,
                "result": result, "ms": int((time.time() - t0) * 1000)}

    # 开关生效点 2/2：Runtime 级校验。即使模型无视提示硬写工具名，
    # 或者提示是旧的、开关刚关，这里也会拦住。不依赖模型自律。
    if name not in TOOLS:
        return done(OUTCOME_NOT_EXECUTED, f"未知工具: {name}")
    if not SETTINGS.get("tools", {}).get(name, True):
        return done(OUTCOME_NOT_EXECUTED,
                    f"工具 {name} 已在设置中关闭，服务端拒绝执行")

    miss = missing_required(name, args_dict)
    if miss:
        return done(OUTCOME_NOT_EXECUTED,
                    f"缺少必填参数: {', '.join(miss)}（请补齐后重试）")

    # 熔断：广播通道刚被卡死过，短期内再去调只会白等一个 stepTimeout。
    # 立即失败 + 说清原因，让模型换路子或直接答复用户。
    if _is_broadcast_tool(name):
        left = broadcast_cooldown_left()
        if left > 0:
            return done(OUTCOME_NOT_EXECUTED,
                        f"(手机硬件通道刚有调用超时，暂时不可用，约 {int(left)}s 后恢复。"
                        f"本次未执行 {name}；请改用其他方式或直接告知用户稍后再试)")

    # ---------- handler 型工具（文件/网络/插件）：进程内直接跑 Python ----------
    # 不 fork 子进程，所以：没有孤儿进程、不受广播熔断牵连、不需要进程组 kill。
    # 代价是它跑在服务进程里，必须自己控住资源上限（读 4MiB / 扫 2万条）。
    #
    # 插件工具也走这个分支，但 handler 名是 "plugin:<pid>:<tid>" 前缀标记，
    # 不在 TOOL_HANDLERS 里 —— 它要通过 BridgeClient 跨进程调到 node 那边。
    # 必须先于 TOOL_HANDLERS 查表拦截，否则会误报「工具实现缺失」。
    handler_name = TOOLS[name].get("handler")
    if handler_name:
        with USAGE_LOCK:
            USAGE["toolCalls"] += 1

        if isinstance(handler_name, str) and handler_name.startswith("plugin:"):
            parts = handler_name.split(":", 2)
            if len(parts) != 3:
                return done(OUTCOME_FAILURE, f"(插件工具标记损坏: {handler_name})")
            _, plug_id, tool_id = parts
            # 崩溃巡检：进程可能已经死了但状态还是 running。
            # 在这里顺手查一次，比常驻轮询线程省电（手机上多一个线程就是多一份电）。
            st = PLUGIN_MANAGER.crash_check(plug_id)
            if st != "running":
                return done(OUTCOME_NOT_EXECUTED,
                            f"(插件 {plug_id} 当前状态 {st}，未执行本工具)")
            # PluginToolContext（桌面端契约）：userQuery / conversationId / mode / runId
            tool_ctx = {
                "userQuery": ctx.get("userQuery", "") if isinstance(ctx, dict) else "",
                "conversationId": ctx.get("sid") if isinstance(ctx, dict) else None,
                "runId": ctx.get("runId") if isinstance(ctx, dict) else None,
                "mode": ctx.get("mode") if isinstance(ctx, dict) else None,
                "permissionMode": "normal",
            }
            try:
                # 插件工具吃**原生类型**参数（resolve_typed），与内置 handler 型
                # 一致 —— node 那边 args.city 拿到的是字符串还是数字，取决于
                # inputSchema 的声明，不做归一会让插件的类型判断失效。
                typed = resolve_typed(name, args_dict)
            except Exception as e:
                return done(OUTCOME_FAILURE, f"(参数归一失败: {type(e).__name__}: {e})")
            outcome, text = PLUGIN_MANAGER.execute_tool(plug_id, tool_id, typed, tool_ctx)
            if outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_UNKNOWN,
                               OUTCOME_NOT_EXECUTED):
                outcome = OUTCOME_FAILURE
            text = text if isinstance(text, str) else str(text)
            return done(outcome, _truncate_output(text) if text.strip() else "(无输出)")

        fn = TOOL_HANDLERS.get(handler_name)
        if fn is None:
            return done(OUTCOME_FAILURE, f"(工具实现缺失: {handler_name})")
        try:
            typed = resolve_typed(name, args_dict)
            # needs_ctx 的 handler 多收一个上下文字典（见 _loop_ctx：sid / mode /
            # userQuery / runId）。其余 handler 签名不变，避免为了几个工具去改已有的。
            if TOOLS[name].get("needs_ctx"):
                outcome, text = fn(typed, ctx)
            else:
                outcome, text = fn(typed)
        except PathGuardError as e:
            return done(OUTCOME_FAILURE, str(e))
        except Exception as e:
            # 兜底：handler 里任何意外都不能把 HTTP 线程带走
            return done(OUTCOME_FAILURE, f"(失败: {type(e).__name__}: {e})")
        if outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_UNKNOWN):
            outcome = OUTCOME_FAILURE
        text = text if isinstance(text, str) else str(text)
        return done(outcome, _truncate_output(text) if text.strip() else "(无输出)")

    # 占位符替换：{ts} 是时间戳，其余按 params 声明的键取值
    vals = resolve_args(name, args_dict)
    ts = str(int(time.time()))
    cmd = []
    for seg in TOOLS[name]["cmd"]:
        seg = seg.replace("{ts}", ts)
        for k, v in vals.items():
            seg = seg.replace("{" + k + "}", v)
        cmd.append(seg)

    timeout = agent_cfg("stepTimeout")
    with USAGE_LOCK:
        USAGE["toolCalls"] += 1

    # 必须让工具跑在独立进程组里，超时后杀整组。
    # 直接用 subprocess.run(timeout=...) 的话，超时只会 kill 掉 `sh -c` 这层父进程，
    # 而 termux-* 命令背后那个 `libexec/termux-api XxxBroadcast` 广播子进程还挂在
    # Android 那头干等，被 init 收养成孤儿（实测：一轮探测后 ps 里有 12 行残留）。
    # 孤儿越攒越多，还会加剧 termux-api 的广播竞争，让后续调用更容易超时。
    popen_kw = {}
    if os.name == "posix":
        popen_kw["start_new_session"] = True   # setsid，子进程自成一个进程组

    def _kill_tree(p):
        """杀掉整个进程组。杀不掉也认了，但不能让它把异常抛到上层。"""
        if os.name != "posix":
            try:
                p.kill()
            except Exception:
                pass
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    p = None
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, **popen_kw)
        try:
            stdout, stderr = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            # 杀完还要回收，否则留僵尸；communicate 此时会立即返回
            try:
                p.communicate(timeout=5)
            except Exception:
                pass
            # 广播类工具超时 = 通道大概率被卡住了。打开熔断窗口，
            # 让接下来的广播调用快速失败，而不是每个都白等一个 stepTimeout
            # （实测：一次 location 超时能让 battery 成功率从 5/5 掉到 1/5）。
            if _is_broadcast_tool(name):
                broadcast_open()
            # unknown 而非 failure：命令可能已经生效，模型不该重放
            return done(OUTCOME_UNKNOWN,
                        f"(执行超过 {timeout}s 被中止；命令可能已部分生效，请勿盲目重试)")
        out = (stdout or "").strip() or (stderr or "").strip()
        if p.returncode == 0:
            return done(OUTCOME_SUCCESS, _truncate_output(out) if out else "(无输出)")
        # 非零退出：命令跑了但失败。带上退出码，模型才知道该换路子还是重试
        detail = _truncate_output(out) if out else "(无输出)"
        return done(OUTCOME_FAILURE, f"(退出码 {p.returncode}) {detail}")
    except FileNotFoundError:
        return done(OUTCOME_FAILURE,
                    "(命令不存在：Termux:API 未安装或不在手机上运行)")
    except Exception as e:
        # 兜底：任何意外都要确保子进程不残留
        if p is not None and p.poll() is None:
            _kill_tree(p)
        return done(OUTCOME_FAILURE, f"(失败: {type(e).__name__}: {e})")


def parse_tool_calls(message):
    """解析原生 Function Calling 的 message.tool_calls。

    返回 [{"id", "name", "args"}]，按模型给出的原始顺序（结果必须按这个顺序
    回灌，否则 tool_call_id 对不上）。arguments 是 JSON 字符串，解析失败时
    退化成空 dict 而不是抛异常 —— 让工具自己报「缺必填参数」，比整轮崩掉好。
    """
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(calls, list):
        return []
    out = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except (json.JSONDecodeError, ValueError):
                args = {}
        else:
            args = {}
        name = fn.get("name") or ""
        if not name:
            continue
        out.append({"id": c.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                    "name": name, "args": args})
    return out


def dispatch_tools(calls, max_parallel=None, ctx=None):
    """执行一批工具调用，返回与 calls 等长、同序的结果列表。

    并行策略：maxParallel>1 时用线程池并发，但**结果严格按 calls 原始顺序
    返回**（对齐桌面端「保守并行调度：结果始终按模型原始 tool-call 顺序提交」）。
    单个工具抛异常不会波及其他工具 —— 出错槽位用合成的 failure 结果闭合，
    保证返回长度永远等于输入长度，回灌时 tool_call_id 不会错位。

    ctx 透传给 run_tool（needs_ctx 的 handler 要用 sid）。
    """
    if not calls:
        return []
    limit = int(max_parallel if max_parallel is not None else agent_cfg("maxParallel"))
    limit = max(1, min(limit, len(calls)))

    if limit == 1 or len(calls) == 1:
        return [run_tool(c["name"], c["args"], ctx) for c in calls]

    results = [None] * len(calls)
    with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as ex:
        fut2idx = {ex.submit(run_tool, c["name"], c["args"], ctx): i
                   for i, c in enumerate(calls)}
        for fut in concurrent.futures.as_completed(fut2idx):
            i = fut2idx[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                # 合成失败结果闭合这个槽位，绝不留 None（回灌时会是 null content）
                results[i] = {"tool": calls[i]["name"], "args": calls[i]["args"],
                              "outcome": OUTCOME_FAILURE,
                              "result": f"(执行异常: {type(e).__name__}: {e})", "ms": 0}
    return results


def dispatch_exclusive(calls, max_parallel=None, ctx=None):
    """带排他语义的工具分发。返回 (results, exclusive_hit)。

    exclusive_hit 是命中的排他调用 {"name","args","result"}，没命中则 None。

    规则：本批 calls 里只要出现排他工具（EXCLUSIVE_TOOLS），就**只执行它**，
    其余调用一律合成 not_executed 闭合槽位。not_executed 而非 failure，
    因为「没执行」是事实，failure 会让模型以为工具坏了去重试或换路子。

    多个排他工具同批出现时只认第一个（模型本就不该这么干），
    其余排他调用同样标 not_executed，理由写清是「已有排他调用」。
    """
    if not calls:
        return [], None

    hit_idx = None
    for i, c in enumerate(calls):
        if c.get("name") in EXCLUSIVE_TOOLS:
            hit_idx = i
            break
    if hit_idx is None:
        return dispatch_tools(calls, max_parallel, ctx), None

    hit = calls[hit_idx]
    results = []
    for i, c in enumerate(calls):
        if i == hit_idx:
            results.append(run_tool(c["name"], c.get("args"), ctx))
            continue
        reason = ("同一轮里已经有排他调用 %s，本调用未执行" % hit["name"]
                  if c.get("name") in EXCLUSIVE_TOOLS
                  else "本轮已有 ask_user 排他提问，其余工具一律不执行（等用户回答后再说）")
        results.append({"tool": c["name"], "args": c.get("args") or {},
                        "outcome": OUTCOME_NOT_EXECUTED, "result": f"({reason})",
                        "ms": 0})
    return results, {"name": hit["name"], "args": hit.get("args") or {},
                     "result": results[hit_idx]}


# ========== TTS ==========
# termux-tts-speak 是阻塞式播放。保存句柄以便 /tts/stop 能真的打断它。
TTS_PROC = None
TTS_LOCK = threading.Lock()


def tts_speak(text):
    """在当前线程阻塞播放，播完返回。返回 (ok, message)。"""
    global TTS_PROC
    t = str(text or "").strip()
    if not t:
        return False, "empty"
    cfg = SETTINGS.get("tts", {})
    cmd = ["termux-tts-speak"]
    try:
        rate = float(cfg.get("rate", 1.0))
        cmd += ["-r", str(round(rate, 2))]
    except (TypeError, ValueError):
        pass
    try:
        pitch = float(cfg.get("pitch", 1.0))
        cmd += ["-p", str(round(pitch, 2))]
    except (TypeError, ValueError):
        pass
    lang = str(cfg.get("language") or "").strip()
    if lang:
        cmd += ["-l", lang[:20]]
    cmd.append(t[:800])
    try:
        with TTS_LOCK:
            TTS_PROC = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE)
            proc = TTS_PROC
        _, err = proc.communicate(timeout=60)
        with TTS_LOCK:
            TTS_PROC = None
        if proc.returncode != 0:
            detail = (err or b"").decode(errors="replace").strip()[:200]
            return False, detail or f"exit {proc.returncode}"
        return True, "ok"
    except FileNotFoundError:
        with TTS_LOCK:
            TTS_PROC = None
        return False, "termux-tts-speak 不存在（需在 Termux 内运行并安装 Termux:API）"
    except subprocess.TimeoutExpired:
        tts_stop()
        return False, "播放超时"
    except Exception as e:
        with TTS_LOCK:
            TTS_PROC = None
        return False, str(e)


def tts_stop():
    global TTS_PROC
    with TTS_LOCK:
        proc, TTS_PROC = TTS_PROC, None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # termux-tts-speak 只是客户端，引擎侧要单独叫停
        try:
            subprocess.run(["termux-tts-speak", "-s"], timeout=5,
                           capture_output=True)
        except Exception:
            pass
        return True
    return False


# ========== 会话管理 ==========
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def _new_session(mode=None):
    sid = str(uuid.uuid4())[:8]
    if mode is None:
        try:
            mode = SETTINGS.get("chat", {}).get("defaultMode", DEFAULT_MODE)
        except NameError:
            mode = DEFAULT_MODE
    return sid, {"id": sid, "title": "新对话", "messages": [], "created": time.time(),
                 "mode": normalize_mode(mode)}


LEGACY_ERROR_PREFIXES = ("[API ", "[错误]", "[请求失败]")


def migrate_error_messages(sessions):
    """把旧版本误当正文存下的错误串迁移成 error 标记。

    早期 LLMClient.chat() 失败时返回 (msg, None, msg)，content 和 error 是同一个串，
    _handle_send 就把它当 assistant 正文落了库。后果：
      1. 错误串永久留在历史里，刷新还在；
      2. 构造上下文时被当成对话内容发给模型，污染后续回复。
    这里按前缀识别并原地转成 {content:"", error:...}。幂等：已是 error 的跳过。
    返回迁移条数。
    """
    n = 0
    for s in sessions.values():
        if not isinstance(s, dict):
            continue
        for m in (s.get("messages") or []):
            if not isinstance(m, dict) or m.get("error"):
                continue
            if m.get("role") != "assistant":
                continue
            c = m.get("content")
            if isinstance(c, str) and c.startswith(LEGACY_ERROR_PREFIXES):
                m["error"] = c
                m["content"] = ""
                n += 1
    return n


def load_sessions():
    """读取会话。解析失败时把坏文件改名保留，绝不静默吞掉历史。"""
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                fixed = migrate_error_messages(data)
                if fixed:
                    print(f"⚠ 迁移了 {fixed} 条被误存为正文的错误消息（转 error 标记）")
                    save_sessions(data)
                return data
        except Exception:
            try:
                bak = SESSIONS_FILE.with_name(f"sessions.json.corrupt-{int(time.time())}")
                os.replace(SESSIONS_FILE, bak)
                print(f"⚠ sessions.json 解析失败，已保留副本: {bak.name}")
            except OSError:
                pass
    sid, s = _new_session()
    return {sid: s}


def save_sessions(sessions):
    """tmp 写入 + os.replace 原子替换，避免写入中断产生半截 JSON。"""
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSIONS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sessions, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SESSIONS_FILE)
        return True
    except OSError as e:
        print(f"⚠ 会话保存失败: {e}")
        return False


SESSIONS = load_sessions()
CURRENT_SID = list(SESSIONS.keys())[0] if SESSIONS else None

# 锁拆分：
#   STORE_LOCK 只保护 SESSIONS 结构与落盘，持锁时间是毫秒级。
#   LLM 网络调用与 run_tool 一律在锁外执行，否则一次慢请求会冻结整个服务。
#   INFLIGHT 记录「哪个会话正在生成」，同会话重复请求直接 409，不排队。
#   ABORT 记录「哪个会话被用户要求中止」，agent loop 每轮开头检查它并优雅退出。
STORE_LOCK = threading.Lock()
INFLIGHT = {}

# ---- 实时增量推送（思考过程看得见）----
# agent loop 每收到一片增量就丢进订阅队列，GET /chat/<sid>/stream 的线程
# 再取出来写成 SSE。没有订阅者时，代价只是一次空的列表遍历。
STREAM_SUBS = {}            # sid -> set(queue.Queue)
STREAM_LOCK = threading.Lock()
STREAM_MAX_SECONDS = 900    # 单条 SSE 连接的兜底时长，防止线程挂死


def stream_subscribe(sid):
    q = queue.Queue(maxsize=4000)
    with STREAM_LOCK:
        STREAM_SUBS.setdefault(sid, set()).add(q)
    return q


def stream_unsubscribe(sid, q):
    with STREAM_LOCK:
        subs = STREAM_SUBS.get(sid)
        if subs is not None:
            subs.discard(q)
            if not subs:
                STREAM_SUBS.pop(sid, None)


def stream_publish(sid, kind, text):
    # 广播一片增量；没有人订就什么都不做。
    if not text:
        return
    with STREAM_LOCK:
        subs = list(STREAM_SUBS.get(sid) or ())
    for q in subs:
        try:
            q.put_nowait((kind, text))
        except Exception:
            pass

ABORT = {}

# loop 最近一次运行的统计，供设置面板 Agent tab 的状态区展示（阶段5 用）
LAST_RUN = {}
LAST_RUN_LOCK = threading.Lock()


def is_aborted(sid):
    """用户是否点了中止。只读，不清标记（清由 loop 收尾时统一做）。"""
    with STORE_LOCK:
        return bool(ABORT.get(sid))


def clear_abort(sid):
    with STORE_LOCK:
        ABORT.pop(sid, None)


def request_abort(sid):
    """置中止标记。只有真在跑的会话才有意义，但置了也无害。"""
    with STORE_LOCK:
        ABORT[sid] = True
    return True


class Throttle:
    """相邻 LLM 调用的最小间隔控制器。

    中转站限流 40 rpm，多轮 loop 会把请求数成倍放大 —— 不节流的话一个
    4 轮任务就能吃掉 4 次配额，几个会话并发就直接撞 554。
    第一次调用不等待（用户已经在等了），之后每次至少间隔 interval 秒。
    """

    def __init__(self, interval_s):
        self.interval = max(0.0, float(interval_s))
        self.last = 0.0

    def wait(self, sid=None):
        if self.interval <= 0:
            return
        # 等待期间也要能响应中止，否则用户点了停止还得干等一个间隔
        deadline = self.last + self.interval
        while True:
            now = time.time()
            remain = deadline - now
            if remain <= 0:
                break
            if sid is not None and is_aborted(sid):
                break
            time.sleep(min(remain, 0.2))
        self.last = time.time()


def enabled_tool_names(mode=None):
    """当前开关下真正可用的工具名（按 TOOLS 声明顺序）。

    loop 期间应当冻结这份清单：中途改开关会让前后轮的 tools 数组不一致，
    模型可能引用一个已经消失的工具。

    mode 用于插件工具的模式过滤：桌面端 PluginTool.modes 声明该工具只在
    某些模式出现（取值 learn/code/work）。内置工具没有 modes 键 = 全模式可用。
    不传 mode 时不过滤（保持既有行为，验证脚本与单元测试依赖这一点）。
    """
    t = SETTINGS.get("tools", {})
    out = []
    for n, spec in TOOLS.items():
        if not t.get(n, True):
            continue
        modes = spec.get("modes")
        if mode and isinstance(modes, list) and modes and mode not in modes:
            continue
        out.append(n)
    return out


def _loop_ctx(sid, history, mode=None):
    """构造本次 loop 的工具调用上下文。

    这份 ctx 一路传到 run_tool → 插件工具的 PluginToolContext（桌面端契约）。
    内置 handler 只读 sid，多出来的键对它们无害。

    为什么在 loop 开头构造一次并冻结，而不是每轮重算：
      · userQuery 语义是「本轮用户提出的问题」，整个 loop 里不变。
        若每轮从 work 里取最后一条 user 消息，中途注入的「[系统] 检测到重复调用」
        这类合成消息会被当成用户问题传给插件 —— 那是错的。
      · runId 必须在整个 loop 里稳定，否则插件无法把多次工具调用关联成一次运行。
    """
    user_query = ""
    for m in reversed(history if isinstance(history, list) else []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                user_query = c
            break
    if mode is None:
        s = _session_get(sid)
        mode = (s or {}).get("mode") if isinstance(s, dict) else None
    return {
        "sid": sid,                       # 内置 handler 用（update_todo / ask_user）
        "mode": mode,
        "userQuery": user_query,
        "runId": uuid.uuid4().hex[:12],
    }


def call_signature(name, args):
    """工具调用的指纹，用于死循环检测。"""
    try:
        blob = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        blob = str(args)
    return f"{name}|{blob}"


# loop 的终止原因（对齐桌面端 CyreneHarness 的 4 种终止状态）
END_SUCCESS = "success"        # 模型不再调工具，主动收尾
END_CAPPED = "capped"          # 撞到 maxTurns
END_ABORTED = "cancelled"      # 用户点了中止
END_TIMEOUT = "timeout"        # 撞到 totalTimeout
END_ERROR = "error"            # LLM 报错
END_LOOPSTUCK = "loop_stuck"   # 同一调用连续重复，判定死循环
END_ASK_USER = "awaiting_user"  # 模型调了 ask_user，主动交还控制权等用户回答


def run_agent_loop(sid, client, prompt, history, allow_tools, cfg=None):
    """多轮 Agent 循环：观察 → 行动 → 观察，直到模型收尾或撞到终止条件。

    这是整个 agent 能力的心脏。此前手机端只有单轮（if tool_line: 跑一次工具
    再调一次 LLM 就结束），复杂任务根本推不动。

    参数
      sid           会话 id，用于查中止标记
      client        LLMClient（单元测试可注入假对象，只要它有 chat_ex）
      prompt        系统提示
      history       已构造好的历史消息（只含 role/content）
      allow_tools   模式是否允许工具（chat=False，双层拦截的第二层）
      cfg           覆盖 agent 配置，缺省读 SETTINGS["agent"]

    返回 dict
      content / reasoning / steps / error / turns / endReason / protocol
      capped / aborted / timedOut / lengthHit / toolCalls
      ask / awaitingUser     ← ask_user 命中时的结构化提问载荷
      tool_line / tool_result   ← 旧字段，兼容现有前端与验收脚本

    设计要点（对齐桌面端 CyreneHarness）
      · assistant 消息每轮**无条件**写回 work 上下文 —— 漏了模型下一轮就看不到
        自己上一步说了什么，loop 立刻崩。这是桌面端明写的铁律。
      · 工具结果按 calls 的**原始顺序**回灌，tool_call_id 一一对应，绝不错位。
      · 中间轮次只存在于本次 loop 的 work 上下文里，**不落库、不跨请求回灌**：
        最终正文已经总结了工具结果，回灌全过程只是白烧 token。跨请求历史因此
        只有 user / assistant 两种 role，前端与历史裁剪逻辑都不用改。
      · 超时归 outcome=unknown 而非 failure，因为命令可能已生效，不能诱导模型重放。
      · 排他工具（ask_user）命中即 break 主循环，同批次其余调用合成
        not_executed 闭合槽位（见 dispatch_exclusive），endReason=awaiting_user。
    """
    cfg = cfg if isinstance(cfg, dict) else {}

    def C(k):
        return cfg[k] if k in cfg else agent_cfg(k)

    max_turns = max(1, int(C("maxTurns")))
    max_parallel = max(1, int(C("maxParallel")))
    total_timeout = max(1, int(C("totalTimeout")))
    throttle_ms = max(0, int(C("throttleMs")))
    protocol = C("protocol")
    repeat_limit = int(cfg.get("repeatLimit", 2))   # 同一调用连撞几次判死循环

    throttle = Throttle(throttle_ms / 1000.0)
    t_start = time.time()

    # 工具清单在 loop 期间**冻结**：中途改开关会让前后轮 tools 数组不一致，
    # 模型可能引用一个已经消失的工具，端点直接 400。
    #
    # 会话模式也在 loop 开头取一次：插件工具可以声明 modes（只在某些模式出现），
    # 过滤依据必须与 tools_payload 用同一个 mode 值，否则会出现「schema 里
    # 声明了某工具、执行时又被过滤掉」的错位。
    _sess = _session_get(sid)
    loop_mode = (_sess or {}).get("mode") if isinstance(_sess, dict) else None
    tool_names = enabled_tool_names(loop_mode) if allow_tools else []
    use_fc = bool(tool_names)
    tools_payload = tool_schemas(tool_names) if use_fc else None

    # 工具调用上下文同样冻结（见 _loop_ctx 的说明：userQuery/runId 整个 loop 不变）
    loop_ctx = _loop_ctx(sid, history, loop_mode)

    work = list(history)     # 本次 loop 的完整工作上下文（含中间轮次）
    steps = []               # 落库用的中间步骤
    seen = {}                # 调用指纹 → 连续重复次数
    last_sigs = None

    content = ""
    reasoning = None
    err = None
    turns = 0          # 主循环轮数（每轮一次带工具的 LLM 调用）
    llm_calls = 0      # 真实发出的 LLM 请求数，含触顶/halted 后的收尾调用
    tool_call_total = 0
    end_reason = END_SUCCESS
    length_hit = False
    fc_downgraded = False
    legacy_line = None
    legacy_result = None
    ask_payload = None       # 命中排他工具（ask_user）时的问题载荷，前端渲染提问卡用

    def elapsed_over():
        return (time.time() - t_start) > total_timeout

    while True:
        # ---- 轮前终止检查：中止 / 超时 / 轮数上限 ----
        if is_aborted(sid):
            end_reason = END_ABORTED
            break
        if elapsed_over():
            end_reason = END_TIMEOUT
            break
        if turns >= max_turns:
            end_reason = END_CAPPED
            break

        throttle.wait(sid)
        if is_aborted(sid):
            end_reason = END_ABORTED
            break

        # 本次请求是否带了 tools 的快照。降级分支会在本轮内把 use_fc 置 False，
        # 而回灌 role 要按「发出请求时的形态」决定，所以先留一份快照。
        req_fc = use_fc
        r = client.chat_ex_stream([{"role": "system", "content": prompt}] + work,
                           tools=tools_payload if use_fc else None,
                                  on_delta=lambda _k, _t: stream_publish(sid, _k, _t),
                                  cancel=lambda: is_aborted(sid))
        turns += 1
        llm_calls += 1

        if r.get("error"):
            err = r["error"]
            end_reason = END_ERROR
            break

        # auto 档：端点明确拒收 tools → 本次 loop 剩余轮次不再带 tools，
        # 退化成纯文本对话。fc 档不降级（用户显式要求，撞错也要撞给他看）。
        if use_fc and r.get("fc_rejected") and protocol != "fc":
            use_fc = False
            tools_payload = None
            fc_downgraded = True

        if r.get("reasoning") and not reasoning:
            reasoning = r["reasoning"]
        if r.get("length_hit"):
            length_hit = True

        content = r.get("content") or ""
        calls = list(r.get("tool_calls") or [])

        # Chat 模式二次拦截：即使模型无视提示硬调工具，这里也剥掉，只留正文。
        # 不依赖提示词自律。
        if not allow_tools:
            calls = []

        if not calls:
            # 模型主动收尾 —— 唯一正常的 success 出口
            end_reason = END_SUCCESS
            break

        # ---- 有工具调用 ----
        # assistant 消息必写回（漏了 loop 就崩，见函数 docstring）
        asst = {"role": "assistant", "content": content}
        raw_tcs = (r.get("message") or {}).get("tool_calls")
        # 只有请求真带了 tools 时才能回写 tool_calls 字段，否则端点会 400
        if req_fc and raw_tcs:
            asst["tool_calls"] = raw_tcs
        work.append(asst)

        # 死循环检测：同一批调用指纹连续重复 repeat_limit 次就掐断。
        # 模型偶尔会卡在「调同一个工具拿同样结果」上，不拦就会烧光配额。
        sigs = tuple(call_signature(c["name"], c.get("args")) for c in calls)
        if sigs == last_sigs:
            seen[sigs] = seen.get(sigs, 1) + 1
        else:
            seen[sigs] = 1
        last_sigs = sigs
        if seen[sigs] > repeat_limit:
            end_reason = END_LOOPSTUCK
            # 已经把 assistant 写回了但没执行工具，补一条说明让上下文闭合
            work.append({"role": "user",
                         "content": "[系统] 检测到重复的相同工具调用，已中止循环。"
                                    "请根据已有信息直接回答，不要再重复调用。"})
            break

        results, excl_hit = dispatch_exclusive(calls, max_parallel, loop_ctx)
        tool_call_total += len(calls)
        if excl_hit is not None:
            # 只有 ask_user **校验通过**才算真命中：参数不合法时 handler 返回
            # failure，这时候 loop 应该继续，让模型看到报错自己改问题重试。
            # 否则一个格式错的提问就把整轮对话卡死在「等用户回答」上。
            if (excl_hit["result"].get("outcome") == OUTCOME_SUCCESS
                    and excl_hit["name"] == "ask_user"):
                qs, _, _ = _normalize_ask(excl_hit["args"].get("questions"))
                ask_payload = {"questions": qs}
            else:
                excl_hit = None

        step_tools = []
        for c, res in zip(calls, results):
            # 请求带了 tools 才用 role=tool 回灌（tool_call_id 必须与 assistant
            # 的 tool_calls 一一对应）；否则退化成 user 消息包一层文字。
            if req_fc:
                work.append({"role": "tool", "tool_call_id": c["id"],
                             "content": res.get("result", "")})
            else:
                work.append({"role": "user",
                             "content": f"[工具结果] {c['name']}: {res.get('result', '')}"})
            step_tools.append({
                "id": c["id"], "tool": res.get("tool", c["name"]),
                "args": res.get("args", c.get("args")) or {},
                "outcome": res.get("outcome", OUTCOME_FAILURE),
                "result": res.get("result", ""),
                "ms": res.get("ms", 0),
            })
            # 旧字段：取最后一次工具调用，兼容现有前端与 _mode_accept.py
            legacy_line = f"{c['name']} " + " ".join(
                str(v) for v in (res.get("args") or {}).values())
            legacy_result = res.get("result", "")

        steps.append({"turn": turns, "text": content, "tools": step_tools})

        # ---- 排他工具：ask_user 命中即结束本轮 loop ----
        # 桌面端 Ask 互斥的移植：一边问用户一边把活干完，提问就没有意义了。
        # 排他工具本身照常执行（要它校验并格式化问题），同批次其余调用
        # 在 dispatch_exclusive 里已被合成 not_executed，槽位全部闭合，
        # FC 的 tool_call_id 不会错位。
        if ask_payload is not None:
            end_reason = END_ASK_USER
            break

        # 非幂等工具命中 unknown（多半是超时）→ 暂停后续调用，防止自动重放副作用
        halted = any(t["outcome"] == OUTCOME_UNKNOWN and
                     TOOLS.get(t["tool"], {}).get("risk") not in ("safe", None)
                     for t in step_tools)
        if halted:
            work.append({"role": "user",
                         "content": "[系统] 有工具执行结果不确定（可能已生效），"
                                    "已暂停后续工具调用。请直接向用户说明情况。"})
            # 再要一次正文收尾，但不给工具
            throttle.wait(sid)
            r2 = client.chat_ex([{"role": "system", "content": prompt}] + work, tools=None)
            llm_calls += 1     # 这次不算主循环轮次，只是补一句交代
            if not r2.get("error"):
                content = r2.get("content") or content
                if r2.get("reasoning") and not reasoning:
                    reasoning = r2["reasoning"]
                end_reason = END_SUCCESS
            else:
                err = r2["error"]
                end_reason = END_ERROR
            break

    # ---- 撞到轮数上限：再给一次「不带工具」的收尾机会 ----
    # 否则用户会看到工具跑了一堆却没有任何回答。这次调用不计入 turns。
    if end_reason == END_CAPPED and not is_aborted(sid) and not elapsed_over():
        work.append({"role": "user",
                     "content": f"[系统] 已达到工具调用轮数上限（{max_turns} 轮），"
                                "请根据目前已获得的信息直接回答用户，不要再调用工具。"})
        throttle.wait(sid)
        r3 = client.chat_ex([{"role": "system", "content": prompt}] + work, tools=None)
        llm_calls += 1     # 不计入 turns：这是触顶后的补救收尾，不是工具轮
        if not r3.get("error"):
            content = r3.get("content") or content
            if r3.get("reasoning") and not reasoning:
                reasoning = r3["reasoning"]
        else:
            # 收尾也失败了，但工具结果是真实拿到过的，保留 endReason=capped
            err = err or r3["error"]

    # 中止 / 超时 / 死循环时，content 可能是空的或半截的 —— 给用户一个明确交代
    if end_reason == END_ABORTED and not content:
        content = "（已被中止）"
    elif end_reason == END_TIMEOUT and not content:
        content = f"（执行超过 {total_timeout}s 总时限被中止，已完成的步骤见下方）"
    elif end_reason == END_LOOPSTUCK and not content:
        content = "（检测到重复调用同一工具，已中止循环）"
    elif end_reason == END_ASK_USER and not content:
        # ask_user 命中时模型常常只发 tool_call、正文是空的。
        # 这里**不再补一次 LLM 调用**去要正文：问题本身就是要给用户看的内容，
        # 再问一次模型纯属白烧配额（还多等一个 throttle 间隔）。
        # 直接把问题拼成正文兜底，前端另有结构化提问卡（见返回值的 "ask"）。
        qs = (ask_payload or {}).get("questions") or []
        content = "\n".join(q.get("question", "") for q in qs) or "（在等你回答一个问题）"

    if length_hit and content:
        # 截断可见化：命中模型长度上限时明示，不静默砍尾
        content += "\n\n_（回复命中长度上限被截断）_"

    # 实际使用的调用协议。文本协议已删除，只剩两种可能：
    #   fc   本次 loop 真的带 tools 跑（正常路径）
    #   none 没有可用工具（chat 模式 / 工具全关）或 auto 档被端点拒收后降级
    proto_used = "fc" if (tool_names and not fc_downgraded) else "none"

    with LAST_RUN_LOCK:
        LAST_RUN.clear()
        LAST_RUN.update({
            "sid": sid, "turns": turns, "llmCalls": llm_calls,
            "toolCalls": tool_call_total,
            "endReason": end_reason, "protocol": proto_used,
            "downgraded": fc_downgraded, "ms": int((time.time() - t_start) * 1000),
            "at": time.time(),
        })

    return {
        "content": content,
        "reasoning": reasoning,
        "steps": steps,
        "error": err,
        "turns": turns,
        "llmCalls": llm_calls,
        "toolCalls": tool_call_total,
        "endReason": end_reason,
        "capped": end_reason == END_CAPPED,
        "aborted": end_reason == END_ABORTED,
        "timedOut": end_reason == END_TIMEOUT,
        "loopStuck": end_reason == END_LOOPSTUCK,
        "lengthHit": length_hit,
        # ask_user 命中时的结构化提问载荷（前端渲染成可点选的提问卡）。
        # 未命中恒为 None，前端据此判断要不要画卡片。
        "ask": ask_payload,
        "awaitingUser": end_reason == END_ASK_USER,
        "protocol": proto_used,
        "downgraded": fc_downgraded,
        "elapsedMs": int((time.time() - t_start) * 1000),
        # 旧字段，兼容现有前端与验收脚本
        "tool_line": legacy_line,
        "tool_result": legacy_result,
    }


def sessions_summary():
    """侧边栏只需要这些字段。messages 不外发。"""
    out = {}
    for sid, s in SESSIONS.items():
        msgs = s.get("messages") or []
        out[sid] = {
            "id": sid,
            "title": s.get("title", "新对话"),
            "created": s.get("created", 0),
            "count": len(msgs),
        }
    return out


# ========== Web 页面骨架 ==========
# 样式与逻辑全部外置到 static/，这里只留结构。
# 好处：改前端不用动 Python，也不会再把 50KB 字符串塞进源码里。
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh" data-ui-theme="charcoal-pink">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content">
<meta name="theme-color" content="#141414">
<title>昔涟</title>
<link rel="stylesheet" href="/static/hljs-theme.css">
<link rel="stylesheet" href="/static/app.css">
<link rel="stylesheet" href="/static/settings.css">
</head>
<body>

<div id="sidebar">
  <div class="sidebar-header">
    <h2>昔涟 ♪</h2>
    <button id="new-chat-btn" type="button">+ 新对话</button>
    <button id="sidebar-close" class="icon-btn" type="button" aria-label="关闭侧边栏">✕</button>
  </div>
  <div id="session-list"></div>
  <div class="sidebar-footer">
    <div id="model-info"></div>
    <button id="settings-btn" class="icon-btn" type="button" aria-label="打开设置" title="设置">⚙</button>
  </div>
</div>
<div id="scrim"></div>

<div id="main">
  <div id="topbar">
    <button id="menu-btn" class="icon-btn" type="button" aria-label="打开侧边栏">☰</button>
    <button id="mode-btn" type="button" aria-label="切换对话模式" aria-haspopup="true" aria-expanded="false">
      <span id="mode-btn-icon">💬</span><span id="mode-btn-label">聊天</span><span class="mode-caret">▾</span>
    </button>
    <span class="title" id="chat-title">新对话</span>
    <span id="model-badge"></span>
    <button id="top-settings-btn" class="icon-btn" type="button" aria-label="打开设置" title="设置">⚙</button>
    <div id="status-dot"></div>
  </div>
  <div id="mode-menu" role="menu" aria-label="对话模式"></div>
  <div id="chat"></div>
  <div id="input-bar">
    <textarea id="input" placeholder="和昔涟说点什么..." rows="1" enterkeyhint="send"></textarea>
    <button id="send-btn" type="button" aria-label="发送">➤</button>
  </div>
</div>

<div id="settings-scrim"></div>
<aside id="settings" aria-label="设置">
  <div class="settings-titlebar">
    <span class="settings-titlebar__title">设置</span>
    <span class="settings-titlebar__hint">昔涟 v8</span>
    <button id="settings-close" class="icon-btn" type="button" aria-label="关闭设置">✕</button>
  </div>
  <nav class="settings-nav" id="settings-nav"></nav>
  <div class="settings-body" id="settings-body"></div>
  <div class="settings-status">
    <span class="save-status" id="save-status" role="status" aria-live="polite"></span>
    <button id="settings-save" class="btn btn--primary btn--sm" type="button">保存</button>
  </div>
</aside>

<script src="/static/marked.min.js"></script>
<script src="/static/highlight.min.js"></script>
<script src="/static/app.js"></script>
</body>
</html>"""


# ========== HTTP Handler ==========
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive，手机端少握手
    timeout = 75                    # 空闲连接自动回收

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    # ---- 响应（HTTP/1.1 下每条都必须带 Content-Length，否则浏览器会挂住）----
    def _send(self, body, ctype, code=200, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, data, code=200):
        self._send(json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", code)

    def _html(self, html):
        self._send(html.encode(), "text/html; charset=utf-8")

    def _send_extra(self, body, ctype, extra_headers, code=200):
        """带自定义响应头的 _send。

        为什么不直接给 _send 加参数：_send 被静态资源、JSON、HTML 三处共用，
        改签名要动所有调用点，回归面大。插件面板是唯一需要额外头（CSP）的地方，
        单开一个方法把影响面锁死在新增代码里。
        """
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _static(self, name):
        ctype = static_ctype(name)   # 白名单精确匹配；素材子目录另按前缀放行
        if ctype is None:
            self._json({"error": "not found"}, 404); return
        data = load_static(name)
        if data is None:
            self._json({"error": "asset missing on disk"}, 404); return
        cache = ("public, max-age=86400"
                 if name in STATIC_FILES and name not in STATIC_NOCACHE else "no-store")
        self._send(data, ctype, 200, cache)

    def _stream_chat(self, sid):
        # 实时增量推送：SSE 长连接。
        # 网页等一轮回复时订这里，模型吐一片就立刻填进「正在想」那一行。
        # 客户端关了、或挂太久，就在这里收摊；平时由前端在 /send 返回后关闭。
        q = stream_subscribe(sid)
        started = time.time()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            idle = 0.0
            while True:
                if time.time() - started > STREAM_MAX_SECONDS:
                    break
                try:
                    kind, text = q.get(timeout=1.0)
                    idle = 0.0
                except queue.Empty:
                    idle += 1.0
                    if idle >= 15.0:
                        idle = 0.0
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    # 这一轮早跑完了、又一直没有增量：收摊，别占着线程
                    if idle >= 30.0 and not INFLIGHT.get(sid):
                        break
                    continue
                payload = json.dumps({"kind": kind, "text": text}, ensure_ascii=False)
                self.wfile.write(b"data: " + payload.encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            stream_unsubscribe(sid, q)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
        except OSError:
            return {}
        try:
            j = json.loads(raw.decode("utf-8"))
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _seg(path):
        return [p for p in path.split("/") if p]

    def _is_local_client(self):
        """请求是否来自本机。用于 /service/* 的权限门。

        服务默认 bind 0.0.0.0 且无鉴权，若不加这道限制，同一 WiFi 下任意设备
        都能远程把服务关掉。经 adb forward 或手机本机浏览器访问时，
        client_address 都是回环地址，正常使用不受影响。
        """
        try:
            ip = self.client_address[0]
        except Exception:
            return False
        # IPv6 回环有时映射成 IPv4 形式，一并放行
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or ip.startswith("127.")

    # ---- 路由 ----
    def do_GET(self):
        try:
            self._route_get(urlparse(self.path).path)
        except Exception as e:
            self._json({"error": f"server: {e}"}, 500)

    def do_POST(self):
        try:
            self._route_post(urlparse(self.path).path)
        except Exception as e:
            self._json({"error": f"server: {e}"}, 500)

    def do_DELETE(self):
        try:
            self._route_delete(urlparse(self.path).path)
        except Exception as e:
            self._json({"error": f"server: {e}"}, 500)

    def do_HEAD(self):
        self.do_GET()

    def _route_get(self, path):
        seg = self._seg(path)
        if path in ("/", "/index.html"):
            self._html(HTML_PAGE)
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        elif path == "/health":
            self._json({"ok": True,
                        "model": SETTINGS.get("model", {}).get("model", ""),
                        "inflight": len(INFLIGHT)})
        elif path == "/sessions":
            with STORE_LOCK:
                snap = sessions_summary()
                cur = CURRENT_SID
            self._json({"sessions": snap, "current": cur,
                        "model": SETTINGS.get("model", {}).get("model", "")})
        elif path == "/sessions/export":
            with STORE_LOCK:
                self._json({"exportedAt": time.time(), "sessions": SESSIONS})
        elif len(seg) == 3 and seg[0] == "chat" and seg[2] == "stream":
            self._stream_chat(seg[1])
        elif len(seg) == 2 and seg[0] == "chat":
            with STORE_LOCK:
                s = SESSIONS.get(seg[1])
                msgs = list(s.get("messages") or []) if s else []
                title = s.get("title", "新对话") if s else "新对话"
                mode = normalize_mode(s.get("mode")) if s else \
                    SETTINGS.get("chat", {}).get("defaultMode", DEFAULT_MODE)
                # 工作笔记随会话一起回传：刷新页面 / 切换会话后进度条不丢
                todos = list(s.get("todos") or []) if s else []
                ask = s.get("pendingAsk") if s else None
                # inflight：这一轮是否还在后台生成。前端据此决定「刷新后要不要
                # 轮询重载」—— 用户中止/刷新时后端可能还没落库，此刻读到的
                # messages/todos/ask 是旧的；告诉前端「还在跑」，它就能等
                # 后端真正落库后重新拉一次，避免选项/进度条凭空消失。
                inflight = bool(INFLIGHT.get(seg[1]))
            # settleBudget：前端该等 inflight 落 false 多久（秒）。由后端按
            # totalTimeout + request_timeout + 余量算，前端不硬编码 —— 硬编码的
            # 90s 兜不住默认 180s 的 totalTimeout，长轮次会提前放弃轮询。
            # 放锁外：只读 SETTINGS，不碰会话结构。
            budget = settle_budget()
            self._json({"messages": msgs, "title": title, "mode": mode,
                        "todos": todos, "ask": ask, "inflight": inflight,
                        "settleBudget": budget})
        elif path == "/modes":
            self._json({
                "modes": [{"id": k, "label": v["label"], "icon": v["icon"],
                           "desc": v["desc"], "tools": v["tools"]}
                          for k, v in MODES.items()],
                "default": SETTINGS.get("chat", {}).get("defaultMode", DEFAULT_MODE),
            })
        elif path == "/settings":
            self._json(self._public_settings())
        elif path == "/tools":
            self._json({"tools": self._tool_list()})
        elif path == "/skills":
            self._json({"skills": self._skill_list()})
        elif path == "/plugins":
            self._json(self._plugin_overview())
        elif path == "/plugins/market":
            # ?refresh=1 强制刷新，绕过 5 分钟缓存
            q = urllib.parse.parse_qs(urlparse(self.path).query)
            force = _bool_arg((q.get("refresh") or ["0"])[0], False)
            self._handle_plugin_market(force)
        elif len(seg) == 4 and seg[0] == "plugins" and seg[1] == "market" \
                and seg[3] == "detail":
            self._handle_plugin_market_detail(seg[2])
        elif len(seg) == 3 and seg[0] == "plugins" and seg[1] == "install-progress":
            self._json(_progress_get(seg[2]))
        elif len(seg) == 3 and seg[0] == "plugins" and seg[2] == "panel":
            self._handle_plugin_panel(seg[1])
        elif len(seg) == 3 and seg[0] == "plugins" and seg[2] == "logs":
            self._handle_plugin_logs(seg[1])
        elif path == "/usage":
            self._json(usage_snapshot())
        elif path == "/config":
            # 兼容旧端点。api_key 只给布尔，不给明文
            m = SETTINGS.get("model", {})
            safe = {k: v for k, v in m.items() if k != "api_key"}
            safe["api_key_set"] = bool(m.get("api_key"))
            safe.update(SETTINGS.get("server", {}))
            self._json(safe)
        else:
            self._json({"error": "not found"}, 404)

    def _public_settings(self):
        """设置全量镜像，但 api_key 替换成 api_key_set 布尔。"""
        s = json.loads(json.dumps(SETTINGS))
        m = s.get("model", {})
        m["api_key_set"] = bool(m.pop("api_key", ""))

        # ⚠ 插件密钥脱敏。s 是 SETTINGS 的全量深拷贝，plugins.secrets 里存的是
        # 插件通过 ctx.deps.secrets.set() 写入的真实凭据（OpenWeather key 之类）。
        # 不脱敏就等于把密钥原样吐给任何能打开设置面板的人 —— 而这个服务默认
        # bind 在局域网可达的地址上。
        # 只回传 key 名 + 是否已设置，值一律替换成占位符。
        # ⚠ 占位符必须用 PLUGIN_SECRET_MASK 常量：deep_merge_settings 靠比对
        #   同一个常量来识别「前端没改这个值」，两边字面量漂移就会真写进配置。
        pl = s.get("plugins")
        if isinstance(pl, dict):
            raw_sec = pl.get("secrets") if isinstance(pl.get("secrets"), dict) else {}
            pl["secrets"] = {
                pid: {k: PLUGIN_SECRET_MASK for k in kv.keys()} if isinstance(kv, dict) else {}
                for pid, kv in raw_sec.items()
            }
            # registry 里的 lastError 可能含路径片段，但那是排障必需的，保留；
            # sha256 不是秘密，保留供前端比对市场版本。

        s["info"] = {
            "data_dir": str(DATA_DIR),
            "skills_dir": str(SKILLS_DIR),
            "session_count": len(SESSIONS),
            "prompt_chars": len(get_system_prompt()),
            # 各模式系统提示字数，便于前端「模式」页展示差异
            "prompt_chars_by_mode": {m: len(get_system_prompt(m)) for m in MODES},
            "version": "8.0.0",
            # Agent loop 运行态，供设置面板 Agent tab 的状态区展示
            "agent": dict(LAST_RUN),
            "agent_protocols": list(TOOL_PROTOCOLS),
            "agent_ranges": {k: {"min": v[0], "max": v[1], "default": v[2]}
                             for k, v in AGENT_RANGES.items()},
            "tool_count": len(TOOLS),
            # 插件运行态。node_available=False 时前端应把 Node 插件标 unsupported，
            # 而不是让用户点了启用再失败。
            "plugins": {
                "dir": str(PLUGINS_DIR),
                "node_bin": find_node_binary(),
                "node_available": bool(find_node_binary()),
                "api_version": PLUGIN_API_VERSION,
                "host_shell_exists": PLUGIN_HOST_JS.is_file(),
                "running": PLUGIN_MANAGER.running_ids(),
                "installed": len(plugin_registry()),
            },
        }
        s["usage"] = usage_snapshot()
        return s

    def _tool_list(self):
        t = SETTINGS.get("tools", {})
        out = []
        for tid, spec in TOOLS.items():
            cmd = spec.get("cmd") or []
            if cmd:
                preview = " ".join(cmd[:2]) + (" …" if len(cmd) > 2 else "")
            elif spec.get("handler"):
                # handler 型工具没有命令行，面板上标出它是进程内 Python 实现
                preview = f"python:{spec['handler']}"
            else:
                preview = ""
            params = spec.get("params") or {}
            out.append({
                "id": tid, "desc": spec["desc"], "icon": spec.get("icon", "⚙"),
                "enabled": bool(t.get(tid, True)),
                "readonly": bool(spec.get("readonly")),
                "risk": spec.get("risk", "safe"),
                "cmd_preview": preview,
                # 参数签名，供设置面板展示「这个工具要传什么」
                "params": [{"name": p, "type": s.get("type", "string"),
                            "required": bool(s.get("required")),
                            "desc": s.get("desc", "")}
                           for p, s in params.items()],
            })
        return out

    def _skill_list(self):
        out = []
        for info in get_skills():
            out.append({
                "id": info["id"], "name": info["name"],
                "description": info["description"], "version": info["version"],
                "autoInject": info["autoInject"], "modes": info["modes"],
                "enabled": skill_enabled(info["id"], info),
                "defaultEnabled": info["defaultEnabled"],
                "body_len": info["body_len"],
            })
        return out

    def _plugin_overview(self):
        """GET /plugins 的载荷：宿主环境 + 每个插件的状态。

        顺手做崩溃巡检：进程可能已经死了而内存状态还写着 running。
        面板打开的瞬间就把真相刷出来，而不是等用户调工具时才发现。
        巡检放在 snapshot() 之前 —— snapshot 读的就是巡检后的状态。
        """
        try:
            PLUGIN_MANAGER.patrol()
        except Exception:
            pass            # 巡检失败不该让整个面板 404
        node_bin = find_node_binary()
        return {
            "host": {
                "enabled": bool(plugins_cfg("enabled", True)),
                # 插件安全检测开关（导入时静态扫高危调用）。前端据此渲染开关与黄标。
                "securityScan": bool(plugins_cfg("securityScan", True)),
                "dir": str(PLUGINS_DIR),
                "dirExists": PLUGINS_DIR.is_dir(),
                "nodeBin": node_bin,
                "nodeAvailable": bool(node_bin),
                "apiVersion": PLUGIN_API_VERSION,
                "hostShell": str(PLUGIN_HOST_JS),
                "hostShellExists": PLUGIN_HOST_JS.is_file(),
                "running": PLUGIN_MANAGER.running_ids(),
                "states": list(PLUGIN_STATES),
                # 面板靠这个值决定要不要显示「不支持，请先装 Node」的横幅
                "unsupportedReason": "" if node_bin else "未找到 node 可执行文件",
                # 导入面板要的三件事：inbox 在哪（用户得知道把 ZIP 放哪儿）、
                # 体积上限（前端好提前拦下过大的包）、市场源（顶部标出来）
                "inboxDir": str(PLUGIN_INBOX_DIR),
                "inboxExists": PLUGIN_INBOX_DIR.is_dir(),
                "zipMaxMb": PLUGIN_ZIP_MAX_MB,
                "marketSources": [{"name": n, "base": b} for n, b in PLUGIN_MARKET_SOURCES],
                "apiVersionRequired": PLUGIN_API_VERSION,
            },
            "plugins": PLUGIN_MANAGER.snapshot(),
            # 插件工具已经并进 TOOLS，这里单独列一份，前端不必自己从 /tools 里挑
            "tools": [
                {"id": tid, "plugin": spec.get("plugin"),
                 "desc": spec.get("desc", ""), "risk": spec.get("risk", "safe"),
                 "modes": spec.get("modes"),
                 "enabled": bool(SETTINGS.get("tools", {}).get(tid, True))}
                for tid, spec in TOOLS.items() if spec.get("plugin")
            ],
        }

    def _handle_plugin_logs(self, pid):
        if not PLUGIN_ID_RE.match(pid or ""):
            self._json({"error": f"非法插件 id: {pid}"}, 400); return
        q = urllib.parse.parse_qs(urlparse(self.path).query)
        raw = (q.get("limit") or [None])[0]
        self._json(PLUGIN_MANAGER.logs(pid, raw))

    # ---------- 插件面板 HTML（settingsPanel，P7） ----------

    def _handle_plugin_panel(self, pid):
        """serve 插件 manifest 里声明的 settingsPanel HTML。

        前端用 <iframe sandbox="allow-scripts"> 承载（不给 allow-same-origin），
        这里再加一道 CSP：插件 HTML 是第三方内容，两层限制缺一不可。
        iframe 沙箱防它摸同源存储，CSP 防它往外发数据 / 加载远程脚本。
        """
        if not PLUGIN_ID_RE.match(pid or ""):
            self._json({"error": f"非法插件 id: {pid}"}, 400); return
        mf = PLUGIN_MANAGER.manifest(pid)
        if not isinstance(mf, dict):
            self._json({"error": f"插件未安装或 manifest 不可读: {pid}"}, 404); return
        rel = mf.get("settingsPanel")
        if not rel:
            self._json({"error": f"插件 {pid} 没有声明 settingsPanel"}, 404); return
        # rel 来自插件自己的 manifest —— 不可信输入，按 entry 同一套规则校验：
        # 必须是目录内的裸文件名，不许分隔符与 ..
        if not isinstance(rel, str) or "/" in rel or "\\" in rel or ".." in rel:
            self._json({"error": f"settingsPanel 必须是插件目录内的裸文件名: {rel!r}"},
                       400); return
        p = PLUGIN_MANAGER.plugin_dir(pid) / rel
        # 落点再验一次在插件目录内（防 rel 是奇怪形状时拼出目录外路径）
        try:
            root = PLUGIN_MANAGER.plugin_dir(pid).resolve()
            resolved = p.resolve()
            inside = (resolved.is_relative_to(root)
                      if hasattr(resolved, "is_relative_to")
                      else root.parts == resolved.parts[:len(root.parts)])
        except OSError:
            inside = False
        if not inside or not p.is_file():
            self._json({"error": f"面板文件不存在: {rel}"}, 404); return
        try:
            data = p.read_bytes()
        except OSError as e:
            self._json({"error": f"面板文件读取失败: {e}"}, 500); return
        ctype = "text/html; charset=utf-8" if p.suffix.lower() in (".html", ".htm") \
            else "text/plain; charset=utf-8"
        self._send_extra(data, ctype, {
            # unsafe-inline 是必须的：插件面板通常把 <script> 内联在 HTML 里。
            # connect-src 'self' 让它只能打回本服务，不能把数据发到外站。
            "Content-Security-Policy":
                "default-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'",
            "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff",
        })

    def _handle_plugin_ipc(self, pid, body):
        """POST /plugins/<id>/ipc —— 插件面板的 IPC 通道（给 iframe 用）。

        为什么需要它：桌面端面板靠 Electron 的 ipcRenderer 拿数据，手机端
        没有 Electron，面板一句 require 就抛错、整页白屏。转换脚本会把面板
        里的 ipcRenderer 换成打本端点的 shim，面板代码一行都不用改。

        CORS：面板跑在 sandbox="allow-scripts" 的 iframe 里，origin 是 opaque，
        fetch 本端点会带 Origin: null；不放 ACAO 响应就读不到，面板照样空着。
        放通跨源后风险与本机任意页面相当，所以同样挂本机门。
        """
        if not PLUGIN_ID_RE.match(pid or ""):
            self._json({"error": f"非法插件 id: {pid}"}, 400); return
        if not self._is_local_client():
            self._json({"error": "仅本机可调插件面板通道"}, 403); return
        channel = _as_str(body.get("channel"), "")[:120]
        if not channel:
            self._json({"error": "缺少 channel"}, 400); return
        args = body.get("args")
        args = args if isinstance(args, list) else []
        hdr = {"Access-Control-Allow-Origin": "*"}
        try:
            result = PLUGIN_MANAGER.invoke_ipc(pid, channel, args)
        except PluginHostError as e:
            self._send_extra(json.dumps(
                {"ok": False, "error": str(e), "code": e.code},
                ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8", hdr)
            return
        except Exception as e:
            self._send_extra(json.dumps(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8", hdr)
            return
        self._send_extra(json.dumps(
            {"ok": True, "result": result}, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8", hdr)

    # ---------- 插件市场（P5） ----------

    def _handle_plugin_market(self, force_refresh=False):
        try:
            data = market_overview(force_refresh=force_refresh)
        except Exception as e:
            self._json({"ok": False, "plugins": [],
                        "error": f"市场读取失败: {type(e).__name__}: {e}"}, 502)
            return
        if not data.get("ok"):
            # 源不可达不是客户端错误也不是服务端 bug，用 502（上游不可用）
            self._json(data, 502); return
        self._json(data)

    def _handle_plugin_market_detail(self, pid):
        if not PLUGIN_ID_RE.match(pid or ""):
            self._json({"error": f"非法插件 id: {pid}"}, 400); return
        try:
            data = market_detail(pid)
        except Exception as e:
            self._json({"ok": False, "id": pid,
                        "error": f"详情读取失败: {type(e).__name__}: {e}"}, 502)
            return
        self._json(data, 200 if data.get("ok") else 404)

    # ---------- 导入（P6）：三条通道 ----------

    def _plugin_write_gate(self, action):
        """导入类操作的统一本机门。返回 True 表示放行。

        为什么导入必须加本机门（而 enable/disable 不加）：导入 = 往手机写文件
        + 之后可以执行其中的任意 Node 代码，等价于远程代码执行。服务默认
        bind 0.0.0.0 且无鉴权，同一 WiFi 下任意设备都能打到这个端点。
        enable/disable 的破坏力只等同既有的 /tools/<tid> 开关（那个历史上就
        没加本机门），加了反而让面板行为不一致。
        """
        if self._is_local_client():
            return True
        self._json({"error": f"仅本机可{action}插件（会写入文件并可执行其中的代码，"
                             f"不开放给局域网其他设备）"}, 403)
        return False

    def _start_install_job(self, kind, label, worker):
        """把一个安装动作丢到后台线程，立刻回 job key。

        为什么不直接在请求线程里做完：市场下载 9.1 MB 的包 + 解包 + 可能还要
        spawn node 做 register，实测能到几十秒。浏览器 fetch 没有超时设置，
        但用户会以为卡死了；而且请求线程被占住期间 ThreadingHTTPServer 还要
        再开线程服务其他请求，手机上没必要这么浪费。

        统一异步还有个好处：前端只有一条「拿 job key → 轮询 install-progress」
        的路径，四条导入通道共用同一套进度条代码。
        """
        job = f"{kind}-{uuid.uuid4().hex[:12]}"
        _progress_set(job, "downloading", 0, 0, f"{label} 排队中")

        def run():
            try:
                ok, info = worker(job)
                stage = "done" if ok else "failed"
                msg = ""
                if isinstance(info, dict):
                    msg = str(info.get("error") or info.get("id") or "")
                elif info:
                    msg = str(info)
                # info 传给 _progress_set 让它落到进度记录里，前端轮询时能拿到
                # 完整安装结果（含 risky / sha256 / verified）
                _progress_set(job, stage, message=msg or ("成功" if ok else "失败"),
                              info={"ok": bool(ok),
                                    "info": info if isinstance(info, (dict, str)) else str(info)})
                if ok:
                    # TOOLS 变了，系统提示里的工具清单是文本快照，必须重建
                    rebuild_system_prompt()
            except Exception as e:
                _progress_set(job, "failed",
                              message=f"{type(e).__name__}: {e}",
                              info={"ok": False, "info": f"{type(e).__name__}: {e}"})

        t = threading.Thread(target=run, name=f"plg-import-{job}", daemon=True)
        t.start()
        self._json({"ok": True, "job": job, "kind": kind, "label": label})

    def _handle_plugin_import(self, body):
        """POST /plugins/import —— 浏览器选文件，base64 JSON 上传。

        ⚠ 与原设计的偏离（已在计划里标注）：原计划写 multipart/form-data。
        手机端 _body() 只解析 JSON，multipart 要自己写 boundary 解析器。
        改用 base64 JSON 能复用现有管道与前端 apiPost，代价是体积涨 4/3，
        所以这里的上限按「base64 字符串长度」算，不是按原始字节算。
        """
        if not self._plugin_write_gate("导入"):
            return
        b64 = body.get("dataBase64")
        if not isinstance(b64, str) or not b64:
            self._json({"error": "缺少 dataBase64 字段（ZIP 的 base64 编码）"}, 400); return
        # 先按 base64 长度粗筛：解码前就拒掉超大的，避免白白 decode 一次 43 MB
        max_b64 = int(PLUGIN_ZIP_MAX_MB * 1024 * 1024 * 4 / 3) + 4096
        if len(b64) > max_b64:
            self._json({"error": f"上传内容超过上限 {PLUGIN_ZIP_MAX_MB} MB"
                                 f"（base64 长度 {len(b64)}）"}, 413); return
        try:
            data = base64.b64decode(b64, validate=False)
        except (ValueError, TypeError) as e:
            self._json({"error": f"base64 解码失败: {e}"}, 400); return
        if not data:
            self._json({"error": "解码后是空的（0 字节）"}, 400); return
        if len(data) > PLUGIN_ZIP_MAX_MB * 1024 * 1024:
            self._json({"error": f"压缩包 {len(data)//(1024*1024)} MB 超过上限 "
                                 f"{PLUGIN_ZIP_MAX_MB} MB"}, 413); return

        fn = _as_str(body.get("filename"), "upload.zip")[:120]
        want_sha = _as_str(body.get("sha256"), "").strip().lower() or None
        enable = _bool_arg(body.get("enable"), False)

        def worker(job):
            return PLUGIN_MANAGER.import_zip_bytes(
                data, source="upload", expect_sha256=want_sha,
                enable=enable, progress_key=job)

        self._start_install_job("upload", fn, worker)

    def _handle_plugin_import_url(self, body):
        """POST /plugins/import-url —— 贴 ZIP 直链，后端流式下载再装。"""
        if not self._plugin_write_gate("导入"):
            return
        url = _as_str(body.get("url"), "").strip()
        if not url:
            self._json({"error": "缺少 url 字段"}, 400); return
        try:
            _assert_http_url(url)
        except NetError as e:
            self._json({"error": f"URL 不被接受 {e}"}, 400); return
        want_sha = _as_str(body.get("sha256"), "").strip().lower() or None
        enable = _bool_arg(body.get("enable"), False)
        label = url.rsplit("/", 1)[-1][:60] or url[:60]

        def worker(job):
            tmp = None
            try:
                PLUGIN_TMP_ROOT.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix="url_", suffix=".zip",
                                            dir=str(PLUGIN_TMP_ROOT))
                os.close(fd)
                tmp = Path(name)
                try:
                    _market_download_stream(
                        url, tmp, PLUGIN_ZIP_MAX_MB * 1024 * 1024, progress_key=job)
                except (NetError, ValueError) as e:
                    _progress_set(job, "failed", message=str(e))
                    return False, f"下载失败 {e}"
                return PLUGIN_MANAGER.import_zip_file(
                    tmp, source="url", expect_sha256=want_sha,
                    enable=enable, progress_key=job)
            except OSError as e:
                return False, f"临时文件失败: {e}"
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

        self._start_install_job("url", label, worker)

    def _handle_plugin_scan_inbox(self, body):
        """POST /plugins/scan-inbox —— 只列出候选，不安装（安装走 import-inbox）。

        用 POST 而不是 GET：与其它导入端点保持同一方法，前端一组代码；
        且它没有副作用，重复点无妨。
        """
        if not self._plugin_write_gate("导入"):
            return
        try:
            data = PLUGIN_MANAGER.scan_inbox()
        except OSError as e:
            self._json({"error": f"扫描失败: {e}"}, 500); return
        data["ok"] = True
        # 顺手把上限告诉前端，让它能直接标出「这个包太大装不了」
        data["maxMb"] = PLUGIN_ZIP_MAX_MB
        self._json(data)

    def _handle_plugin_import_inbox(self, body):
        """POST /plugins/import-inbox —— 装 inbox 里指定的那个 ZIP。"""
        if not self._plugin_write_gate("导入"):
            return
        fn = _as_str(body.get("filename"), "").strip()
        if not fn:
            self._json({"error": "缺少 filename 字段"}, 400); return
        # filename 会拼进路径，必须过白名单：只准裸文件名，且必须真的在 inbox 里。
        # 不做这一步的话 "?filename=../../.config.json" 就能读到配置文件。
        if "/" in fn or "\\" in fn or ".." in fn or fn.startswith("."):
            self._json({"error": f"非法文件名: {fn!r}"}, 400); return
        p = PLUGIN_INBOX_DIR / fn
        try:
            inside = p.resolve().is_relative_to(PLUGIN_INBOX_DIR.resolve()) \
                if hasattr(p.resolve(), "is_relative_to") else True
        except OSError:
            inside = False
        if not inside:
            self._json({"error": f"文件名越出 inbox 目录: {fn!r}"}, 400); return
        if not p.is_file():
            self._json({"error": f"inbox 里没有这个文件: {fn}"
                                 f"（目录 {PLUGIN_INBOX_DIR}）"}, 404); return
        want_sha = _as_str(body.get("sha256"), "").strip().lower() or None
        enable = _bool_arg(body.get("enable"), False)
        delete_after = _bool_arg(body.get("deleteAfter"), False)

        def worker(job):
            ok, info = PLUGIN_MANAGER.import_zip_file(
                p, source="inbox", expect_sha256=want_sha,
                enable=enable, progress_key=job)
            # 装完删源文件：inbox 是「待装队列」，装成功还留着会让用户
            # 下次扫描又看到它、以为没装上。失败则保留，方便重试。
            if ok and delete_after:
                try:
                    p.unlink()
                except OSError:
                    pass
            return ok, info

        self._start_install_job("inbox", fn, worker)

    def _handle_plugin_market_install(self, pid, from_source, body):
        """POST /plugins/market/<id>/install[-from-source]。"""
        if not self._plugin_write_gate("从市场安装"):
            return
        if not PLUGIN_ID_RE.match(pid or ""):
            self._json({"error": f"非法插件 id: {pid}"}, 400); return
        enable = _bool_arg(body.get("enable"), False)
        force = _bool_arg(body.get("refresh"), False)
        if from_source:
            def worker(job):
                return market_install_from_source(
                    pid, enable=enable, progress_key=job, force_refresh=force)
            self._start_install_job("market-src", pid, worker)
        else:
            def worker2(job):
                return market_install(
                    pid, enable=enable, progress_key=job, force_refresh=force)
            self._start_install_job("market", pid, worker2)

    def _route_post(self, path):
        global CURRENT_SID
        body = self._body()
        seg = self._seg(path)

        if path == "/chat/new":
            want_mode = normalize_mode(body.get("mode"))
            sid, s = _new_session(want_mode)
            with STORE_LOCK:
                SESSIONS[sid] = s
                CURRENT_SID = sid
                save_sessions(SESSIONS)
            self._json({"sid": sid, "mode": want_mode})
            return

        if len(seg) == 3 and seg[0] == "chat" and seg[2] == "send":
            self._handle_send(seg[1], body)
            return

        # 中止正在跑的多轮 loop。不强杀线程，让当前工具自然结束后优雅退出。
        if len(seg) == 3 and seg[0] == "chat" and seg[2] == "abort":
            self._handle_abort(seg[1])
            return

        # 切换某会话的模式。模式绑定在会话上，切换后下一条消息即用新系统提示。
        if len(seg) == 3 and seg[0] == "chat" and seg[2] == "mode":
            self._handle_set_mode(seg[1], body)
            return

        if path == "/settings":
            self._handle_settings(body)
            return

        # ⚠ 顺序要紧：/tools/bulk 必须排在 /tools/{tid} 之前。
        # 反过来的话 seg=["tools","bulk"] 先命中 len==2 分支，被当成
        # 「开关一个叫 bulk 的工具」→ unknown tool → 404。
        # 这个 bug 此前一直存在：前端 bulk 调用的 .then 链没有 .catch，
        # 点了「全部开启 / 只留只读工具」只是静默失败，看不出任何异常。
        if path == "/tools/bulk":
            self._handle_tools_bulk(body)
            return
        if len(seg) == 2 and seg[0] == "tools":
            self._handle_tool_toggle(seg[1], body)
            return
        if len(seg) == 2 and seg[0] == "skills":
            self._handle_skill_toggle(seg[1], body)
            return

        # 插件面板 IPC：POST /plugins/<id>/ipc
        # ⚠ 必须排在下面那条通用 <id>/<action> 之前 —— 两者段数相同，谁先匹配谁赢。
        # 通用那条对未知 action 会直接回 404「unknown action」，轮不到这里。
        if len(seg) == 3 and seg[0] == "plugins" and seg[2] == "ipc":
            self._handle_plugin_ipc(seg[1], body)
            return

        # 插件启停/卸载：POST /plugins/<id>/<action>
        # 三段路径，与上面两段的 tools/skills 分支不会撞。
        if len(seg) == 3 and seg[0] == "plugins":
            self._handle_plugin_action(seg[1], seg[2], body)
            return

        # 插件导入：四条两段路径，全是精确匹配，彼此不会撞，也不会被上面那条
        # len==3 的 <id>/<action> 截胡（段数不同）。
        if path == "/plugins/import":
            self._handle_plugin_import(body)
            return
        if path == "/plugins/import-url":
            self._handle_plugin_import_url(body)
            return
        if path == "/plugins/scan-inbox":
            self._handle_plugin_scan_inbox(body)
            return
        if path == "/plugins/import-inbox":
            self._handle_plugin_import_inbox(body)
            return
        # 市场安装：四段路径 /plugins/market/<id>/install[-from-source]
        if len(seg) == 4 and seg[0] == "plugins" and seg[1] == "market" \
                and seg[3] in ("install", "install-from-source"):
            self._handle_plugin_market_install(seg[2], seg[3] == "install-from-source", body)
            return

        if path == "/tts":
            ok, msg = tts_speak(body.get("text", ""))
            self._json({"ok": ok, "message": msg})
            return
        if path == "/tts/stop":
            self._json({"ok": True, "stopped": tts_stop()})
            return

        if path == "/usage/reset":
            usage_reset()
            self._json({"ok": True, "usage": usage_snapshot()})
            return

        # 服务开关：结束 / 重启。仅本机可调用（见 _is_local_client）。
        # stop = 杀守护器 + 关自己（彻底停，网页断开需去 Termux 重开）；
        # restart = 只关自己，守护器 3 秒后拉起新实例（网页轮询自动恢复）。
        if path == "/service/stop" or path == "/service/restart":
            if not self._is_local_client():
                self._json({"error": "仅本机可操作服务（防止局域网其他设备远程关服）"}, 403)
                return
            mode = "stop" if path == "/service/stop" else "restart"
            # 先回响应让前端拿到 {ok}，再延迟关闭；否则响应随连接一起断掉
            self._json({"ok": True, "mode": mode})
            _delayed_service_action(mode)
            return

        if path == "/sessions/clear":
            with STORE_LOCK:
                SESSIONS.clear()
                sid, s = _new_session()
                SESSIONS[sid] = s
                CURRENT_SID = sid
                save_sessions(SESSIONS)
            self._json({"ok": True, "current": sid})
            return

        if path == "/config":
            # 兼容旧端点，转发到分层结构
            self._handle_settings({"model": {k: v for k, v in body.items()
                                             if k in LEGACY_MODEL_KEYS},
                                   "server": {k: v for k, v in body.items()
                                              if k in LEGACY_SERVER_KEYS}})
            return

        self._json({"error": "not found"}, 404)

    def _handle_plugin_action(self, pid, action, body):
        """POST /plugins/<id>/<action> —— enable / disable / uninstall。

        ⚠ 三个动作之后都必须 rebuild_system_prompt()：TOOLS 变了，而系统提示里的
        「=== 可用工具 ===」清单是**文本快照**。不重建的话模型在提示里看不到新工具
        （FC schema 是每轮现算的，所以工具其实能调 —— 但提示与 schema 不一致
        会让模型困惑，也可能因为提示里明写「只能用上面列出的工具名」而拒绝调用）。
        """
        if not PLUGIN_ID_RE.match(pid or ""):
            self._json({"error": f"非法插件 id: {pid}"}, 400); return

        if action not in ("enable", "disable", "uninstall"):
            self._json({"error": f"unknown action: {action}"
                                 f"（支持 enable / disable / uninstall）"}, 404); return

        # 卸载会删文件，不可逆。与 /service/* 一样只允许本机发起 ——
        # 服务默认 bind 0.0.0.0 且无鉴权，同一 WiFi 下任意设备都能打到这里。
        # enable/disable 不在此列：它们的破坏力等同于既有的 /tools/<tid> 开关
        # （那个端点历史上就没加本机门），加了反而会让面板行为不一致。
        if action == "uninstall" and not self._is_local_client():
            self._json({"error": "仅本机可卸载插件（删除文件不可逆）"}, 403); return

        if action == "enable":
            if not PLUGIN_MANAGER.plugin_dir(pid).is_dir():
                self._json({"error": f"插件未安装: {pid}"}, 404); return
            ok, info = PLUGIN_MANAGER.start(pid)
            rebuild_system_prompt()
            self._json({"ok": ok, "id": pid, "action": action,
                        # 失败时带上 error：前端 pluginErrMsg 优先读它，用户看到
                        # 的是「插件启动失败：<真实原因>」，而不是兜底的 HTTP 502。
                        "error": "" if ok else f"插件启动失败：{info}",
                        "info": info if ok else str(info),
                        "state": PLUGIN_MANAGER.state(pid),
                        "toolCount": len(TOOLS)},
                       200 if ok else 502)
            return

        if action == "disable":
            ok, info = PLUGIN_MANAGER.stop(pid, keep_registry=True)
            rebuild_system_prompt()
            self._json({"ok": ok, "id": pid, "action": action,
                        "info": info if isinstance(info, dict) else str(info),
                        "state": PLUGIN_MANAGER.state(pid),
                        "toolCount": len(TOOLS)})
            return

        # uninstall：removeData=true 才连 data/ 与 secrets 一起删（对齐桌面端语义，
        # 默认保留数据，重装插件不丢配置）。
        remove_data = _bool_arg(body.get("removeData"), False)
        ok, info = PLUGIN_MANAGER.uninstall(pid, remove_data=remove_data)
        rebuild_system_prompt()
        self._json({"ok": ok, "id": pid, "action": action,
                    "info": info if isinstance(info, dict) else str(info),
                    "toolCount": len(TOOLS)},
                   200 if ok else 500)

    def _handle_settings(self, body):
        with SETTINGS_LOCK:
            merged = deep_merge_settings(SETTINGS, body)
            old_key = SETTINGS.get("model", {}).get("api_key", "")
            # 前端不回显 key，空串表示「不修改」，不能把已存的抹掉
            if merged.get("model", {}).get("api_key", "") == "" and old_key:
                merged["model"]["api_key"] = old_key
            SETTINGS.clear()
            SETTINGS.update(merged)
            saved = save_settings_to_disk()
        # 提示词依赖 model/tools/skills/appearance，任一变更都要重建
        rebuild_system_prompt()
        restart = ("server" in body) or ("model" in body and "web_port" in (body.get("server") or {}))
        self._json({"ok": saved, "restart_required": bool(restart),
                    "settings": self._public_settings()})

    def _handle_tool_toggle(self, tid, body):
        if tid not in TOOLS:
            self._json({"error": f"unknown tool: {tid}"}, 404); return
        want = body.get("enabled")
        if not isinstance(want, bool):
            self._json({"error": "enabled must be boolean"}, 400); return
        with SETTINGS_LOCK:
            SETTINGS.setdefault("tools", {})[tid] = want
            saved = save_settings_to_disk()
        rebuild_system_prompt()
        self._json({"ok": saved, "id": tid, "enabled": want})

    def _handle_tools_bulk(self, body):
        want = body.get("enabled")
        if not isinstance(want, bool):
            self._json({"error": "enabled must be boolean"}, 400); return
        only = body.get("only")
        targets = TOOLS.keys()
        if isinstance(only, list):
            targets = [t for t in only if t in TOOLS]
        with SETTINGS_LOCK:
            SETTINGS.setdefault("tools", {})
            for tid in targets:
                SETTINGS["tools"][tid] = want
            saved = save_settings_to_disk()
        rebuild_system_prompt()
        self._json({"ok": saved, "changed": list(targets)})

    def _handle_skill_toggle(self, sid, body):
        want = body.get("enabled")
        if not isinstance(want, bool):
            self._json({"error": "enabled must be boolean"}, 400); return
        known = {i["id"] for i in get_skills(force=True)}
        if sid not in known:
            self._json({"error": f"unknown skill: {sid}"}, 404); return
        with SETTINGS_LOCK:
            SETTINGS.setdefault("skills", {})[sid] = want
            saved = save_settings_to_disk()
        rebuild_system_prompt()
        self._json({"ok": saved, "id": sid, "enabled": want})

    def _handle_set_mode(self, sid, body):
        mode = normalize_mode(body.get("mode"))
        with STORE_LOCK:
            s = SESSIONS.get(sid)
            if s is None:
                self._json({"error": "no such session"}, 404); return
            s["mode"] = mode
            save_sessions(SESSIONS)
        self._json({"ok": True, "sid": sid, "mode": mode,
                    "tools": MODES[mode].get("tools", False)})

    def _handle_send(self, sid, body):
        msg = str(body.get("message", "")).strip()
        if not msg:
            self._json({"error": "empty"}, 400); return
        is_retry = bool(body.get("retry"))

        max_history = SETTINGS.get("model", {}).get("max_history", 30)
        show_reasoning = SETTINGS.get("reasoning", {}).get("showInChat", True)
        with STORE_LOCK:
            # 同会话已有请求在跑 → 直接 409，不排队不冻结服务
            if INFLIGHT.get(sid):
                self._json({"error": "busy", "aborted": True}, 409); return
            INFLIGHT[sid] = True
            # 清掉上一轮可能残留的中止标记，且必须与 INFLIGHT 置位在同一把锁内原子完成。
            # 若放到锁外（构造 client/prompt 之后）再清，会留一个窗口：用户恰好在
            # 「INFLIGHT 已置位、clear_abort 尚未执行」之间点了停止，那次 abort 会被
            # 这里的 clear 吞掉，loop 再也收不到中止信号，一路空转到 totalTimeout。
            # 直接操作 ABORT（不调 clear_abort）——STORE_LOCK 是不可重入 Lock，锁内
            # 再调会死锁。
            ABORT.pop(sid, None)
            s = SESSIONS.get(sid)
            if not s:
                s = {"id": sid, "title": "新对话", "messages": [], "created": time.time(),
                     "mode": SETTINGS.get("chat", {}).get("defaultMode", DEFAULT_MODE)}
                SESSIONS[sid] = s
            mode = normalize_mode(s.get("mode"))
            msgs = s.setdefault("messages", [])

            if is_retry:
                # 重试：删掉末尾那条 error 空壳，user 消息复用库里已有的那条，
                # 不重复追加，否则历史里会出现两条一模一样的用户消息。
                if msgs and msgs[-1].get("error"):
                    msgs.pop()
            else:
                if len(msgs) == 0:
                    s["title"] = msg[:20]
                msgs.append({"role": "user", "content": msg})
                # 用户开口了 = 上一轮的提问已被回答，撤掉待答标记。
                # 不撤的话前端会一直挂着提问卡，用户答完还能再点一次。
                s.pop("pendingAsk", None)

            maxlen = int(max_history) * 2
            if len(msgs) > maxlen:
                del msgs[:-maxlen]
            # 历史里可能带 reasoning 字段（上一轮存下的），发给模型时剥掉，
            # 避免思考链污染上下文、白白吃 token。
            # 带 error 的消息是失败轮次的空壳，整条跳过——否则模型会看到
            # "[API 554]" 这类错误串，把它当成对话内容继续编。
            history = [{"role": m.get("role", "user"), "content": m.get("content", "")}
                       for m in msgs
                       if not m.get("error")]

        mode_allows_tools = MODES[mode].get("tools", False)
        show_steps = agent_cfg("showSteps")

        # 以下全部在锁外执行：多轮 loop 最坏能跑 totalTimeout 秒，
        # 若持锁会把整个服务冻住。
        try:
            client = LLMClient(SETTINGS)
            prompt = get_system_prompt(mode)

            # 残留中止标记已在上面置 INFLIGHT 的同一把锁内清掉（见 ABORT.pop）。
            # 这里不再清 —— 否则又会开出「INFLIGHT 已置位、标记被清」的窗口，
            # 用户在这中间点的停止会被吞掉。

            res = run_agent_loop(sid, client, prompt, history,
                                 mode_allows_tools)

            resp = res["content"]
            reasoning = res["reasoning"]
            err = res["error"]
            steps = res["steps"] if show_steps else []

            # 落库：assistant 消息带 reasoning + steps（steps 仅供前端渲染，
            # 构造历史时不回灌，避免污染上下文与白烧 token）。
            # err 存在时存 error 空壳；但工具已真跑过的步骤照样留着，
            # 用户刷新后仍能看到「工具跑了，只是最后总结失败了」。
            with STORE_LOCK:
                s = SESSIONS.get(sid)
                if s is not None:
                    entry = {"role": "assistant", "content": resp if not err else ""}
                    if err:
                        entry["error"] = err
                    else:
                        entry["reasoning"] = reasoning
                    if steps:
                        entry["steps"] = steps
                    # loop 元信息：让前端能显示「用了 3 轮 / 被轮数上限截断」
                    entry["turns"] = res["turns"]
                    entry["llmCalls"] = res["llmCalls"]
                    entry["endReason"] = res["endReason"]
                    # 提问载荷落进这条消息：刷新页面后提问卡还在，用户仍能点选回答。
                    # 同时挂一份到 session.pendingAsk，前端 loadChat 时直接拿来渲染。
                    if res.get("ask"):
                        entry["ask"] = res["ask"]
                        s["pendingAsk"] = res["ask"]
                    s["messages"].append(entry)
                    save_sessions(SESSIONS)
                # ⚠ 这里绝不能再写 with STORE_LOCK —— 已经在外层锁内，而
                #   STORE_LOCK 是不可重入的 threading.Lock，同线程二次 acquire
                #   会永久死锁：handler 卡在落库、到不了 finally，INFLIGHT 永不
                #   清零，前端 settle 轮询永远等不到 inflight=false，中止/刷新后
                #   提问卡选项与进度条就再也回不来（本 bug 的最底层根因）。
                #   s 即 SESSIONS.get(sid)，与重新取一次是同一个对象，直接复用。
                todos_out = list((s or {}).get("todos") or [])

            self._json({
                "response": resp,
                "usage": usage_snapshot(),
                "mode": mode,
                "reasoning": (reasoning if show_reasoning else None),
                "steps": steps,
                "turns": res["turns"],
                "llmCalls": res["llmCalls"],
                "toolCalls": res["toolCalls"],
                "endReason": res["endReason"],
                "turnsCapped": res["capped"],
                "aborted": res["aborted"],
                "timedOut": res["timedOut"],
                "loopStuck": res["loopStuck"],
                "lengthHit": res["lengthHit"],
                "protocol": res["protocol"],
                "elapsedMs": res["elapsedMs"],
                # 阶段 2c：结构化提问载荷 + 工作笔记，前端渲染提问卡与进度条
                "ask": res.get("ask"),
                "awaitingUser": res.get("awaitingUser", False),
                "todos": todos_out,
                # 旧字段，兼容现有前端与 _mode_accept.py / _e2e_work_retry.py
                "tool_line": res["tool_line"],
                "tool_result": res["tool_result"],
                "error": err,
                "failed": bool(err),
            })
        finally:
            with STORE_LOCK:
                INFLIGHT.pop(sid, None)
            clear_abort(sid)

    def _handle_abort(self, sid):
        """用户点「停止」：置中止标记，loop 在下一轮开头优雅退出。

        不强杀线程 —— 正在跑的工具（比如拍照）需要自然结束，
        强杀会留下孤儿进程，上轮 termux-api 的教训。
        """
        with STORE_LOCK:
            running = bool(INFLIGHT.get(sid))
        request_abort(sid)
        self._json({"ok": True, "sid": sid, "wasRunning": running})

    def _route_delete(self, path):
        global CURRENT_SID
        seg = self._seg(path)
        if len(seg) == 2 and seg[0] == "chat":
            sid = seg[1]
            with STORE_LOCK:
                if sid in SESSIONS:
                    del SESSIONS[sid]
                INFLIGHT.pop(sid, None)
                if not SESSIONS:
                    nsid, ns = _new_session()
                    SESSIONS[nsid] = ns
                if CURRENT_SID == sid or CURRENT_SID not in SESSIONS:
                    CURRENT_SID = list(SESSIONS.keys())[0]
                save_sessions(SESSIONS)
                cur = CURRENT_SID
            self._json({"ok": True, "current": cur})
        else:
            self._json({"error": "not found"}, 404)


# ========== 主入口 ==========
# main() 启动后指向 ThreadingHTTPServer 实例，供 /service/* 端点优雅关闭。
# 模块级声明，Handler 里的 _delayed_service_action 才能拿到它调 shutdown()。
HTTP_SERVER = None


def _delayed_service_action(mode):
    """延迟执行服务关闭动作，让当前 HTTP 响应先回到浏览器。

    mode="stop"    彻底停止：先杀守护器 cyrene-web（否则它 3 秒后把服务拉回来），再关自己。
    mode="restart" 软重启：只关自己，守护器 while-true 循环会自动拉起新实例。

    用 threading.Timer 延迟 0.4s：此时 _json 响应已 flush 到浏览器，前端能先拿到
    {ok:true} 再进入断连提示页。HTTP_SERVER.shutdown() 会阻塞直到 serve_forever
    退出，放 Timer 线程里跑，不占用正在处理请求的 HTTP 线程（否则自锁）。
    shutdown 后 main() 的 finally 会做 server_close，进程正常退出。
    """
    def act():
        try:
            if mode == "stop" and os.name == "posix":
                # 守护器是 `while true; python3 runtime/cyrene_web.py; sleep 3; done`，
                # 不杀它的话 python 退出后 3 秒就被拉回来，"结束服务"就成了摆设。
                # pattern 用带连字符的 bin/cyrene-web，不会误伤下划线的 cyrene_web.py
                # （与 deploy_web_v8.py 杀守护器同款，已验证可靠）。
                subprocess.run(["pkill", "-f", "bin/cyrene-web"],
                               capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            if HTTP_SERVER is not None:
                HTTP_SERVER.shutdown()
        except Exception:
            pass

    t = threading.Timer(0.4, act)
    t.daemon = True
    t.start()


def main():
    global HTTP_SERVER
    # SYSTEM_PROMPTS 已在模块加载时按四种模式预建，无需在此重算
    print("=" * 50)
    print("  昔涟 · 手机版 Web Agent  v8")
    print("  主题: charcoal-pink / pearl-white（桌面端真值）")
    print("=" * 50)
    m = SETTINGS.get("model", {})
    sv = SETTINGS.get("server", {})
    print(f"模型: {m.get('model')}")
    print(f"API:  {m.get('api_base')}")
    print(f"Key:  {'已配置' if m.get('api_key') else '未配置!'}")

    missing = [n for n in STATIC_FILES if load_static(n) is None]
    if missing:
        print(f"⚠ 静态资源缺失: {', '.join(missing)}")
        if "app.js" in missing or "app.css" in missing:
            print("  核心前端文件缺失，页面将无法工作。请重新部署 runtime/static/")
            return 1
        print("  Markdown 将降级为纯文本显示，其余功能正常")
    else:
        print(f"静态资源: {len(STATIC_FILES)} 个已就位")

    tools_on = sum(1 for t in TOOLS if SETTINGS.get("tools", {}).get(t, True))
    skills = get_skills(force=True)
    skills_on = sum(1 for i in skills if skill_enabled(i["id"], i) and i.get("autoInject"))
    print(f"工具: {tools_on}/{len(TOOLS)} 启用   技能: {skills_on}/{len(skills)} 注入")
    mode_chars = {k: len(v) for k, v in SYSTEM_PROMPTS.items()}
    print("四大模式提示词字数: " +
          "  ".join(f"{k}={mode_chars[k]}" for k in ("chat", "work", "code", "learn")))
    rz = SETTINGS.get("reasoning", {})
    print(f"外观: {SETTINGS.get('appearance', {}).get('theme')}   "
          f"分段: {SETTINGS.get('appearance', {}).get('mobileMessageSegmentation')}   "
          f"自动朗读: {'开' if SETTINGS.get('tts', {}).get('autoSpeak') else '关'}")
    print(f"默认模式: {SETTINGS.get('chat', {}).get('defaultMode')}   "
          f"思考链: {'开(' + str(rz.get('effort')) + ')' if rz.get('enabled') else '关'}")
    print("")

    port = int(sv.get("web_port", 28443))
    host = str(sv.get("bind_host", "0.0.0.0"))

    ThreadingHTTPServer.address_family = socket.AF_INET
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True

    try:
        server = ThreadingHTTPServer((host, port), Handler)
        HTTP_SERVER = server
    except OSError as e:
        print(f"✗ 端口 {port} 启动失败: {e}")
        print("  可能有旧实例在跑，先执行: pkill -f cyrene_web.py")
        return 1

    print(f"✓ Web 服务已启动: http://{host}:{port}")
    if host in ("0.0.0.0", "::"):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
            probe.close()
            print(f"  局域网: http://{ip}:{port}")
        except OSError:
            pass
        print("  ⚠ 已对局域网开放，且 shell 工具可执行任意命令")
        print("    不需要外部访问时，在设置 → 服务里改成 127.0.0.1")

    # 插件拉起必须在 server 建好之后、serve_forever 之前：
    #   · 之前 —— 插件 register 时宿主会回调 storage/secrets（进程内直调，
    #     不依赖 HTTP），但若插件失败，前端要能立刻 GET /plugins 拿到原因，
    #     HTTP 还没监听就什么都看不到。
    #   · 异步 —— 见 plugin_boot_async 的 docstring：守护器判活的超时是固定的，
    #     node 冷启动 1-3s，同步做会连累 /health。
    plugin_boot_async()

    print("\n按 Ctrl+C 停止\n")
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n再见啦♪")
    finally:
        tts_stop()
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
