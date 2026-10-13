# -*- coding: utf-8 -*-
"""cyrene_search —— 联网搜索子系统（cyrene_web 拆出来的第一块）。

它原来是什么
------------
原 `cyrene_web.py` 里，搜索被拆在三处不相邻的地方，彼此隔着上千行：

  ① 常量与后端实现（原 1054–1308）：结果缓存、博查 / Tavily 客户端、
     Bing / DDG 的 HTML 解析与后端表；
  ② 编排与工具入口（原 2167–2298）：通道表、硬钉判定、抓取降级链、
     `web_search` 这个工具 handler；
  ③ 配置归一（原 4073–4124）：数值区间、引擎白名单、`search_cfg`、
     `search_engine`。

现在它们合成一份，能整块挂上、整块摘下。

它依赖宿主什么
--------------
本模块 **绝不 import cyrene_web** —— 宿主会 import 它，反向 import 会立刻
形成循环依赖（和 `cyrene_core.py` 同一条红线）。

它需要的东西由宿主在启动时经 `bind_deps()` 一次性注入，共四类：

  · 网络通道：`net_error` / `net_open` / `decode_body` / `net_fetch_max`
  · 请求参数：`net_ua` / `net_timeout` / `default_results` / `max_results`
  · 工具层：`int_arg` / `outcome_success` / `outcome_failure`
  · 配置：`settings`（给引用即可，读的时候取最新值）

**没绑就启动不了，报错清楚** —— 而不是跑到一半才莫名 NoneType 崩掉。
这一层显式边界是刻意的：以后把它换成内核服务（`ctx.get("net")` 之类）时，
只要换掉 `bind_deps` 的实现，下面的逻辑一行都不用动。

搬家的纪律
----------
这一刀**只搬不改**：函数体、注释、错误文案、常量数值全部逐字保留。
"先搬后改"是硬规矩，搬迁 commit 与改造 commit 分开，出问题才定位得了。
"""
import html as _html_mod
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

__all__ = [
    # 常量
    "BOCHA_SEARCH_URL", "TAVILY_SEARCH_URL", "SEARCH_CACHE_MAX",
    "SEARCH_RESULT_MAX_BYTES", "SEARCH_FALLBACK_TIMEOUT",
    "SEARCH_RANGES", "SEARCH_ENGINES", "SEARCH_LAST",
    # 依赖注入
    "bind_deps", "deps_ready", "DEPS",
    # 缓存
    "search_cache_get", "search_cache_put", "search_cache_clear",
    "search_cache_count",
    # 后端
    "bocha_search", "tavily_search",
    # 配置
    "normalize_search", "search_cfg", "search_engine",
    # 工具入口与降级链
    "handle_web_search", "fetch_search_fallback",
    # 内核挂载点
    "PLUGIN",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BOCHA_SEARCH_URL = "https://api.bochaai.com/v1/web-search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SEARCH_CACHE_MAX = 64                        # 内存条数上限（与视觉缓存同款考虑）
SEARCH_RESULT_MAX_BYTES = 2 * 1024 * 1024    # 搜索响应读取上限，防 OOM
SEARCH_FALLBACK_TIMEOUT = 8                  # API 失败后落回抓取时的超时封顶（秒）

# search 段的数值区间 (lo, hi, fallback)，与 VISION_RANGES 同款：越界一律夹回。
SEARCH_RANGES = {
    "resultCount": (1, 15, 8),
    "snippetChars": (100, 1000, 500),
    "request_timeout": (5, 60, 20),
    "cacheTtlMin": (0, 240, 30),
}
# 只认这四个值，其余一律回落 auto（前端下拉也只给这四个）。
SEARCH_ENGINES = ("auto", "bocha", "tavily", "off")

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

_search_cache = {}                   # key -> (ts, items)；dict 保序，天然 FIFO 淘汰
_search_cache_lock = threading.Lock()

# 上一次搜索的结果，供 /search/status 与设置页自检显示（**不含 key 明文**）
SEARCH_LAST = {}


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------

class _Deps:
    """宿主注入的资源袋。

    每项在没绑定时是 None；`_need()` 取用会抛出带名字的错误，省得追一个
    没有栈信息的 `NoneType is not callable`。
    """

    __slots__ = ("net_error", "net_open", "decode_body", "net_fetch_max",
                 "net_ua", "net_timeout", "int_arg",
                 "outcome_success", "outcome_failure",
                 "default_results", "max_results", "settings")

    def __init__(self):
        self.net_error = None
        self.net_open = None
        self.decode_body = None
        self.net_fetch_max = None
        self.net_ua = ""
        self.net_timeout = 20
        self.int_arg = None
        self.outcome_success = "success"
        self.outcome_failure = "failure"
        self.default_results = 8
        self.max_results = 15
        self.settings = {}

    def missing(self):
        """返回还没绑上的必需项名字列表。"""
        need = ("net_error", "net_open", "decode_body", "net_fetch_max",
                "net_ua", "int_arg", "settings")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入它提供的能力。未知的键**直接报错**，不静默吞掉。

    返回本模块自己，方便宿主写成一行：

        import cyrene_search as search
        search.bind_deps(net_error=NetError, net_open=_net_open, ...)
    """
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_search.bind_deps 不认识这些参数：%s"
                         % ", ".join(sorted(unknown)))
    for k, v in kw.items():
        setattr(DEPS, k, v)
    return DEPS


def deps_ready():
    """宿主自检用：返回还缺哪些注入项（空列表 = 齐了）。"""
    return DEPS.missing()


# ---------------------------------------------------------------------------
# 结果缓存
# ---------------------------------------------------------------------------

def _search_cache_ttl():
    """当前生效的结果缓存 TTL（秒）。0 = 不缓存（用户把 cacheTtlMin 拉到 0）。"""
    try:
        return max(0, int(search_cfg("cacheTtlMin", 30) or 0)) * 60
    except (TypeError, ValueError):
        return 30 * 60


def search_cache_get(key):
    """读结果缓存。命中返回 items，否则 None。

    与视觉缓存同款：TTL 按**读取时**的当前配置判，用户在面板上调了立刻生效。
    """
    if not key:
        return None
    ttl = _search_cache_ttl()
    if ttl <= 0:
        return None
    now = time.time()
    with _search_cache_lock:
        item = _search_cache.get(key)
        if item is None:
            return None
        ts, items = item
        if now - ts > ttl:
            _search_cache.pop(key, None)
            return None
        return items


def search_cache_put(key, items):
    """写结果缓存。只缓存成功结果 —— 错误缓存下来等于把一次网络抖动固化半小时。"""
    if not key or not items:
        return
    with _search_cache_lock:
        while len(_search_cache) >= SEARCH_CACHE_MAX:
            try:
                _search_cache.popitem(last=False)
            except KeyError:
                break
        _search_cache[key] = (time.time(), list(items))


def search_cache_clear():
    """清空结果缓存，返回清掉的条数。"""
    with _search_cache_lock:
        n = len(_search_cache)
        _search_cache.clear()
        return n


def search_cache_count():
    with _search_cache_lock:
        return len(_search_cache)


# ---------------------------------------------------------------------------
# 结构化搜索后端（博查 / Tavily）
# ---------------------------------------------------------------------------
# 桌面端那份的要点照搬：engine 由设置注入、POST 一小段 JSON、Bearer key、
# 结果统一成 {title,url,snippet,source}、snippet 截断、30 分钟 TTL 缓存。
# 手机端两点差别：
#   ① 只用 urllib + json（零第三方依赖）；
#   ② API 通道失败要能落回下面那套 HTML 抓取，所以每个 client 只负责
#      「拿到就返回」，「什么时候该降级」留在 handle_web_search 里决定。
# 两条 API 通道共用超时、响应上限与结果缓存 —— 差别只在端点、请求体、响应字段。

def _net_post_json(url, payload, headers, max_bytes, timeout):
    """POST 一段 JSON 并把响应按 JSON 解回来，返回 dict。

    为什么另写一个而不给 _net_open 加参数：_net_open 是 GET-only 的读通道
    （fetch_url 那条工具全靠它），把 POST / 自定义头揉进去会让那条路径多出一堆
    分支；搜索这边只需要「发一小段 JSON、收一小段 JSON」，独立成函数更清楚。
    错误口径与 _net_open 一致：一律抛 NetError，调用方只管降级。
    """
    if not isinstance(url, str) or not url.strip():
        raise DEPS.net_error("(URL 为空)")
    url = url.strip()
    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise DEPS.net_error("(只支持 http/https 网址)")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"User-Agent": DEPS.net_ua, "Content-Type": "application/json",
            "Accept": "application/json"}
    for k, v in (headers or {}).items():
        hdrs[str(k)] = str(v)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        # 401/403（key 不对）最常见，把状态码原样带出来，用户能自己判断
        raise DEPS.net_error(f"(HTTP {e.code} 错误：{e.reason})")
    except urllib.error.URLError as e:
        raise DEPS.net_error(f"(连不上目标：{getattr(e, 'reason', e)})")
    except Exception as e:
        raise DEPS.net_error(f"(请求失败：{type(e).__name__}: {e})")
    if len(raw) > max_bytes:
        raise DEPS.net_error("(响应过大，已放弃)")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        raise DEPS.net_error("(响应不是合法 JSON)")
    if not isinstance(data, dict):
        raise DEPS.net_error("(响应结构不认识)")
    return data


def _bocha_items(data, snippet_chars):
    """把博查响应映射成统一的 [(title, url, snippet, source)]。

    两种形态都收：文档里的 data.webPages.value，以及直接把 webPages 放在根上的
    写法（不同版本/网关都见过，收宽一点不会错）。
    """
    pages = None
    if isinstance(data.get("data"), dict):
        pages = data["data"].get("webPages")
    if not isinstance(pages, dict):
        pages = data.get("webPages")
    values = pages.get("value") if isinstance(pages, dict) else None
    items = []
    for r in (values or []):
        if not isinstance(r, dict):
            continue
        title = str(r.get("name") or r.get("title") or "").strip()
        url = str(r.get("url") or "").strip()
        snippet = re.sub(r"\s+", " ", str(r.get("summary") or r.get("snippet") or "")).strip()
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars] + "…"
        source = str(r.get("siteName") or "").strip()
        if url.startswith("http"):
            items.append((title, url, snippet, source))
    return items


def bocha_search(query, count, timeout):
    """调博查 /v1/web-search。返回 [(title,url,snippet,source)]；失败抛 NetError。"""
    key = str(search_cfg("bochaKey", "") or "").strip()
    if not key:
        raise DEPS.net_error("(没配博查 key)")
    snippet_chars = int(search_cfg("snippetChars", 500) or 500)
    data = _net_post_json(
        BOCHA_SEARCH_URL,
        {"query": query, "count": count, "summary": True},
        {"Authorization": "Bearer " + key},
        SEARCH_RESULT_MAX_BYTES, timeout)
    return _bocha_items(data, snippet_chars)


def _tavily_items(data, snippet_chars):
    """把 Tavily 响应映射成统一的 [(title, url, snippet, source)]。

    官方形态是根上的 results[]（每项 title/url/content/score/raw_content）；
    网关包装过的写法见过 data.results，收宽一点不会错（与 _bocha_items 同款思路）。
    Tavily 不返回站点名，source 用 URL 的 host —— 总比空串有用，也和博查那条路
    「有来源就填」的语义一致（抓取那条路拿不到站点名，仍是空串）。
    """
    results = None
    if isinstance(data.get("data"), dict):
        results = data["data"].get("results")
    if not isinstance(results, list):
        results = data.get("results")
    items = []
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or r.get("name") or "").strip()
        url = str(r.get("url") or "").strip()
        snippet = re.sub(r"\s+", " ",
                         str(r.get("content") or r.get("snippet") or "")).strip()
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars] + "…"
        source = ""
        try:
            source = urlparse(url).netloc
        except Exception:
            source = ""
        if url.startswith("http"):
            items.append((title, url, snippet, source))
    return items


def tavily_search(query, count, timeout):
    """调 Tavily /search。返回 [(title,url,snippet,source)]；失败抛 NetError。

    search_depth 固定 basic：advanced 更慢也更吃额度，而调用方（模型）拿到的是
    同样的 标题/链接/摘要 三元组，这里不划算。
    """
    key = str(search_cfg("tavilyKey", "") or "").strip()
    if not key:
        raise DEPS.net_error("(没配 Tavily key)")
    snippet_chars = int(search_cfg("snippetChars", 500) or 500)
    data = _net_post_json(
        TAVILY_SEARCH_URL,
        {"query": query, "max_results": count, "search_depth": "basic",
         "include_answer": False},
        {"Authorization": "Bearer " + key},
        SEARCH_RESULT_MAX_BYTES, timeout)
    return _tavily_items(data, snippet_chars)


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

# 抓取后端的尝试顺序（原来是宿主的 NET_SEARCH_BACKENDS，随搜索一起搬来）
NET_SEARCH_BACKENDS = ("bing", "cn.bing", "ddg")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def normalize_search(raw):
    """归一化联网搜索配置。白名单式：缺项回落默认值，非法值夹回合法范围。

    与 normalize_vision 同一套语义（都是偏好设置，不是用户数据）。
    """
    s = raw if isinstance(raw, dict) else {}
    eng = _as_str(s.get("engine"), "auto").strip().lower()
    out = {
        "engine": eng if eng in SEARCH_ENGINES else "auto",
        # key 不做长度截断 —— 截断会悄悄废掉一个 key，用户看到的是「鉴权失败」
        # 而不是「配置被截断了」（与 vision.api_key 同口径；两把 key 一致）。
        "bochaKey": _as_str(s.get("bochaKey"), "").strip(),
        "tavilyKey": _as_str(s.get("tavilyKey"), "").strip(),
    }
    for k, (lo, hi, fb) in SEARCH_RANGES.items():
        out[k] = int(_clamp(s.get(k), lo, hi, fb))
    return out


def _as_str(v, fb):
    """归一小工具：非字符串一律回落默认值。

    原来是宿主里与 vision 共用的那个；搜索搬出来之后自带一份，避免为一个
    字符串判断再添一个注入项。行为与原实现一致：只认 str，其余走 fb。
    """
    return v if isinstance(v, str) else fb


def _clamp(v, lo, hi, fb):
    """归一小工具：取整并夹到 [lo, hi]，非法值回落 fb。与原实现同款。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = int(fb)
    return max(lo, min(hi, n))


def _settings_dict():
    """拿到当前配置字典。

    宿主可以注入 dict 本身，也可以注入一个**取值函数** —— 后者更稳：
    SETTINGS 将来被整体替换（再跑一次 load_settings）时，这里读到的仍是最新
    那份，不会攥着一个过期快照。两种形态都收。
    """
    s = DEPS.settings
    if callable(s):
        try:
            s = s()
        except Exception:
            return {}
    return s if isinstance(s, dict) else {}


def search_cfg(key, fb=None):
    """读单个搜索配置项。配置还没就位时回落默认值（与 vision_cfg 同构）。"""
    try:
        return _settings_dict().get("search", {}).get(key, fb)
    except (NameError, AttributeError):
        return fb


def search_engine():
    """当前生效的搜索链路：'bocha' / 'tavily' / 'fetch'。只看配置齐不齐，不碰网络。"""
    eng = str(search_cfg("engine", "auto") or "auto").strip().lower()
    if eng == "off":
        return "fetch"
    if eng in _SEARCH_API_CLIENTS:
        return eng
    # auto：博查优先、其次 Tavily，谁配了 key 用谁；都没配就还是抓取 ——
    # 不填 key 的用户行为与改动前完全一致。
    if str(search_cfg("bochaKey", "") or "").strip():
        return "bocha"
    if str(search_cfg("tavilyKey", "") or "").strip():
        return "tavily"
    return "fetch"


# ---------------------------------------------------------------------------
# 编排与工具入口
# ---------------------------------------------------------------------------

# 三条结构化 API 通道的 client 与显示名。收成表是为了让 handle_web_search 里只剩
# 一条分支 —— 以后加第三条通道只改这几张表 + search_engine()，不动主流程。
# （这三张表在函数定义之后才出现在源码里，但模块级执行顺序保证拿得到函数对象。）
_SEARCH_API_CLIENTS = {"bocha": bocha_search, "tavily": tavily_search}
_SEARCH_API_LABELS = {"bocha": "博查 API", "tavily": "Tavily API"}
_SEARCH_API_SHORT = {"bocha": "博查", "tavily": "Tavily"}


def _search_engine_pinned():
    """引擎是否被用户显式硬钉（只用博查 / 只用 Tavily）。

    硬钉档失败要如实报错：他要的就是那家的结果，偷偷塞一份抓来的网页结果
    给他反而是骗人。只有 auto 档才允许落回抓取。
    """
    return str(search_cfg("engine", "auto") or "auto").strip().lower() in ("bocha", "tavily")


def fetch_search_fallback(query, n, timeout):
    """原来那套 HTML 抓取链（bing / cn.bing / ddg 轮流降级）。

    返回 (backend, items, errors)：items 非空表示某个后端拿到了结果；全挂时
    backend 为 None、items 为空、errors 里是每个后端失败的原因。
    items 元素统一成 (title, url, snippet, source)，与博查那条路同形 ——
    抓取拿不到站点名，source 一律空串。
    """
    q = urllib.parse.quote(query)
    errors = []
    for backend in NET_SEARCH_BACKENDS:
        tmpl, parser = _NET_SEARCH_SOURCES[backend]
        url = tmpl.replace("{q}", q)
        try:
            _status, _headers, body, _trunc = DEPS.net_open(
                url, DEPS.net_fetch_max, timeout)
        except DEPS.net_error as e:
            errors.append(f"{backend}: {e}")
            continue
        ctype = _headers.get("content-type", "")
        text, _ = DEPS.decode_body(body, ctype)
        try:
            parsed = parser(text)
        except Exception as e:
            errors.append(f"{backend}: 解析失败 {type(e).__name__}")
            continue
        if not parsed:
            errors.append(f"{backend}: 解析出 0 条（页面结构可能变了）")
            continue
        # 去重（按 URL）后截到 n 条
        seen, uniq = set(), []
        for title, uurl, snippet in parsed:
            if uurl in seen:
                continue
            seen.add(uurl)
            uniq.append((title, uurl, snippet, ""))
            if len(uniq) >= n:
                break
        if uniq:
            return backend, uniq, errors
        errors.append(f"{backend}: 去重后没有可用结果")
    return None, [], errors


def handle_web_search(a):
    """web_search：按设置选一条结构化 API（博查 / Tavily），失败再抓结果页。

    降级链：**API（auto 档下博查优先，其次 Tavily）→ 网页抓取**。没配任何 key
    时整条路径与改动前完全一致；engine 硬钉成 bocha / tavily 时失败就如实报错
    —— 他要的是那一家的结果，塞一份抓来的网页结果给他反而是骗人。

    （宿主侧原来的名字是 `_h_web_search`；搬出来后去掉下划线，因为它现在是
    这个模块的公开入口之一。）
    """
    query = (a.get("query") or "").strip()
    if not query:
        return DEPS.outcome_failure, "(缺少 query)"
    n = DEPS.int_arg(a.get("max_results"), DEPS.default_results, 1, DEPS.max_results)
    timeout = DEPS.net_timeout() if callable(DEPS.net_timeout) else DEPS.net_timeout

    items, head, notes = [], "", []
    provider = search_engine()
    label = ""          # 只有走了 API 通道才有值，给下面拼错误文案用

    if provider in _SEARCH_API_CLIENTS:
        label = _SEARCH_API_LABELS.get(provider, provider)
        ckey = "%s|%s|%d" % (provider, query, n)
        cached = search_cache_get(ckey)
        if cached is not None:
            items = list(cached)
            head = f"搜索「{query}」· {label}（缓存）· {len(items)} 条结果"
            notes.append("同一关键词在缓存有效期内，未重复请求")
        else:
            try:
                items = _SEARCH_API_CLIENTS[provider](
                    query, n, int(search_cfg("request_timeout", 20) or 20))
                search_cache_put(ckey, items)
                head = f"搜索「{query}」· {label} · {len(items)} 条结果"
                SEARCH_LAST.clear()
                SEARCH_LAST.update({"ok": True, "at": time.time(),
                                    "engine": provider, "count": len(items)})
            except DEPS.net_error as e:
                err = str(e)
                SEARCH_LAST.clear()
                SEARCH_LAST.update({"ok": False, "at": time.time(),
                                    "engine": provider, "error": err[:200]})
                if _search_engine_pinned():
                    short = _SEARCH_API_SHORT.get(provider, provider)
                    return DEPS.outcome_failure, (
                        "(%s搜索失败：%s。当前搜索设置是「只用%s」，没给你换成网页抓取；"
                        "想让它自动落回抓取就把引擎改成 auto)" % (short, err, short))
                notes.append("%s失败，已落回网页抓取（%s）" % (label, err))
                # 降级时把抓取超时压到 8 秒封顶：别让总等待变成两次超时相加
                timeout = min(timeout, SEARCH_FALLBACK_TIMEOUT)

    if not items:
        backend, items, errors = fetch_search_fallback(query, n, timeout)
        if not items:
            prefix = ("(%s与网页抓取都失败了。"
                      % _SEARCH_API_SHORT.get(provider, provider)
                      if notes else "(所有搜索后端都失败了。")
            return DEPS.outcome_failure, (prefix + " | ".join(errors) +
                                          "。可稍后重试，或用 fetch_url 直接抓已知网址)")
        head = f"搜索「{query}」· 后端 {backend} · {len(items)} 条结果"

    lines = [head]
    lines.extend(notes)
    for i, it in enumerate(items, 1):
        title, uurl, snippet = it[0], it[1], it[2]
        source = it[3] if len(it) > 3 else ""
        lines.append(f"{i}. {title}" + (f"（{source}）" if source else ""))
        lines.append(f"   {uurl}")
        if snippet:
            sn = snippet if len(snippet) <= 200 else snippet[:200] + "…"
            lines.append(f"   {sn}")
    return DEPS.outcome_success, "\n".join(lines)


# ---------------------------------------------------------------------------
# 内核挂载点（cyrene_core）
# ---------------------------------------------------------------------------
# 从这里开始，搜索不再由宿主逐个 `bind_deps`，而是**声明自己需要哪几个服务**，
# 等服务齐了由内核来调 setup。setup 只干一件事：把服务里的能力填进 DEPS ——
# 下面的实现一行都不用改。
#
# 服务按能力划分（对齐 cordis 的 `ctx.<key>` 风格）：
#   net     网络通道：error / open / decode_body / fetch_max / ua / timeout
#   tools   工具层通用件：int_arg / outcome_* / 结果条数上下限
#   config  配置读取（取值函数；SETTINGS 换一版也能读到新的）
#
# ⚠ 仍然是那条红线：本模块**绝不 import cyrene_web**。服务由内核送进来，
#   方向始终是「宿主 → 内核 → 子系统」，不会成环。
#
# `bind_deps` 保留着 —— 冒烟脚本与单测直接用它（不经过内核），内核不可用时
# 宿主也可以拿它兜底。两条路填的是同一份 DEPS，行为一致。

#: 一份全新的 DEPS，只用来取各项的默认值（卸载时把 DEPS 复位成它）
_FRESH_DEPS = _Deps()


def _reset_deps():
    """把 DEPS 复位成「未注入」状态。

    卸载时用。撤干净的意义在于：插件摘下来之后，这里不该继续留着别人的东西 ——
    否则下一次装配前若有人误调，会撞上一个"半截可用"的子系统。
    """
    for k in _Deps.__slots__:
        setattr(DEPS, k, getattr(_FRESH_DEPS, k))


def _plugin_setup(ctx):
    """内核在 net / tools / config 三个服务齐了之后调这里。"""
    net = ctx.require("net")
    tools = ctx.require("tools")
    cfg = ctx.require("config")

    bind_deps(
        net_error=net.error,
        net_open=net.open,
        decode_body=net.decode_body,
        net_fetch_max=net.fetch_max,
        net_ua=net.ua,
        net_timeout=net.timeout,
        int_arg=tools.int_arg,
        outcome_success=tools.outcome_success,
        outcome_failure=tools.outcome_failure,
        default_results=tools.default_results,
        max_results=tools.max_results,
        settings=cfg,
    )
    ctx.effect(_reset_deps, desc="search:deps")
    ctx.log("已接到内核（依赖 net / tools / config）")


#: 内核的挂载描述。宿主 `rt.use(PLUGIN)` 即可，依赖由内核盯。
PLUGIN = {
    "id": "search",
    "requires": ["net", "tools", "config"],
    "setup": _plugin_setup,
}
