# -*- coding: utf-8 -*-
"""cyrene_session —— 会话状态层（`cyrene_web` 拆出来的第八块）。

它原来是什么
------------
「服务在内存里存着什么、怎么落盘、怎么推给浏览器、怎么被叫停」这一整套：

  会话存储     `SESSIONS`（sid → 会话 dict）+ `STORE_LOCK` + `load_sessions` /
               `save_sessions`（tmp 写入 + `os.replace` 原子替换；坏文件改名保留，
               绝不静默吞掉历史）
  互斥与中断   `INFLIGHT`（同会话重复请求直接 409，不排队）、`ABORT`
               （用户点停止 → agent loop 每轮开头检查并优雅退出）
  两条事件流   `STREAM_SUBS`（按会话推增量，SSE 增量推送）
               + `UI_SUBS` / `UI_REV`（整页级推送：皮肤热改这类「界面要立刻变」）
  运行态       `LAST_RUN`（loop 最近一次运行的统计，设置面板 Agent tab 用）

它依赖宿主什么
--------------
由宿主经 `bind_deps()` 注入：

  · `settings()`        配置读取（`_new_session` 要拿默认模式）
  · `default_mode()`    默认对话模式
  · `normalize_mode(m)` 模式名归一
  · `sessions_file()`   会话库路径（data/sessions.json）

⚠ `settings` / `default_mode` / `sessions_file` 缺了会静默走偏（新会话带着错的模式、
  或者写到别的文件去），`deps_ready()` 把它们列为必需项。

⚠ 导出给宿主的读函数 `ui_rev()`：`UI_REV` 是**整数**，值转发会让宿主永远读到初始值
  （这是跨模块共享可变状态里最容易被忽略的一种），所以给个读函数。

红线同前：**绝不 import cyrene_web**，方向只有「宿主 → 内核 → 子系统」。
"""

import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

# 兜底用：与宿主的 BASE_DIR 同址（cyrene_mobile/）
_BASE_DIR = Path(__file__).parent.parent


__all__ = [
    "SESSIONS", "STORE_LOCK", "INFLIGHT", "ABORT", "LAST_RUN", "LAST_RUN_LOCK",
    "STREAM_SUBS", "STREAM_LOCK", "STREAM_MAX_SECONDS",
    "UI_SUBS", "UI_LOCK", "UI_MAX_SECONDS",
    "LEGACY_ERROR_PREFIXES",
    "_new_session", "migrate_error_messages", "load_sessions", "save_sessions",
    "ui_subscribe", "ui_unsubscribe", "ui_bump_rev", "ui_rev",
    "stream_subscribe", "stream_unsubscribe", "stream_publish",
    "is_aborted", "clear_abort", "request_abort",
    "sessions_summary",
    "bind_deps", "deps_ready", "DEPS", "PLUGIN",
]


def ui_rev():
    """界面配置版本号。

    它是个**整数** —— 转发值的话，宿主拿到的永远是赋值那一刻的快照，
    之后模块里 `+= 1` 它看不见。所以这里给的是读函数。
    """
    return UI_REV


def _def_settings():
    """配置还没注入时的空配置。"""
    return {}


def _def_default_mode():
    return "chat"


def _def_normalize_mode(m):
    return m


def _def_sessions_file():
    """会话库路径兜底：与宿主的 BASE_DIR/data 同址，不必 import 宿主。"""
    return _BASE_DIR / "data" / "sessions.json"


def _def_data_dir():
    """数据目录兜底（段首「确保目录存在」用）。"""
    return _BASE_DIR / "data"


class _Deps:
    """宿主注入的资源袋。"""

    __slots__ = ("settings", "default_mode", "normalize_mode",
                 "sessions_file", "data_dir")

    def __init__(self):
        self.settings = _def_settings
        self.default_mode = _def_default_mode
        self.normalize_mode = _def_normalize_mode
        self.sessions_file = _def_sessions_file
        self.data_dir = _def_data_dir

    def missing(self):
        # 这三个缺了会静默走偏：新会话带错模式、或写到别的文件去
        need = ("settings", "default_mode", "normalize_mode")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()
_FRESH_DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入。未知的键直接报错，不静默吞掉。"""
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_session.bind_deps 不认识这些参数：%s"
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
    """内核在 tools 服务齐了之后调这里。"""
    tools = ctx.require("tools")

    bind_deps(
        settings=tools.settings,
        default_mode=tools.default_mode,
        normalize_mode=tools.normalize_mode,
        sessions_file=tools.sessions_file,
        data_dir=tools.data_dir,
    )
    ctx.effect(_reset_deps, desc="session:deps")
    ctx.log("已接到内核（依赖 tools）")


PLUGIN = {
    "id": "session",
    "requires": ["tools"],
    "setup": _plugin_setup,
}


# ===========================================================================
# 以下正文由 _make_session_module.py 从 cyrene_web.py 逐字抽出
# ===========================================================================


# ========== 会话管理 ==========
try:
    DEPS.data_dir().mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def _new_session(mode=None):
    sid = str(uuid.uuid4())[:8]
    if mode is None:
        try:
            mode = DEPS.settings().get("chat", {}).get("defaultMode", DEPS.default_mode())
        except NameError:
            mode = DEPS.default_mode()
    return sid, {"id": sid, "title": "新对话", "messages": [], "created": time.time(),
                 "mode": DEPS.normalize_mode(mode)}


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
    if DEPS.sessions_file().exists():
        try:
            data = json.loads(DEPS.sessions_file().read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                fixed = migrate_error_messages(data)
                if fixed:
                    print(f"⚠ 迁移了 {fixed} 条被误存为正文的错误消息（转 error 标记）")
                    save_sessions(data)
                return data
        except Exception:
            try:
                bak = DEPS.sessions_file().with_name(f"sessions.json.corrupt-{int(time.time())}")
                os.replace(DEPS.sessions_file(), bak)
                print(f"⚠ sessions.json 解析失败，已保留副本: {bak.name}")
            except OSError:
                pass
    sid, s = _new_session()
    return {sid: s}


def save_sessions(sessions):
    """tmp 写入 + os.replace 原子替换，避免写入中断产生半截 JSON。"""
    try:
        DEPS.sessions_file().parent.mkdir(parents=True, exist_ok=True)
        tmp = DEPS.sessions_file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sessions, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DEPS.sessions_file())
        return True
    except OSError as e:
        print(f"⚠ 会话保存失败: {e}")
        return False


SESSIONS = load_sessions()

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

# ---- UI 事件流（皮肤热改这类「界面要立刻变」的事）----
# 跟上面那套同一形状，只是订阅的是整页而不是单个会话。开着页面的每一个
# 标签各订一条；没人订的时候，publish 的代价只是一次空列表遍历。
UI_SUBS = set()             # set(queue.Queue)
UI_LOCK = threading.Lock()
UI_REV = 0                  # 每次界面配置变化 +1，前端拿它判断要不要重画
UI_MAX_SECONDS = 1800


def ui_subscribe():
    q = queue.Queue(maxsize=200)
    with UI_LOCK:
        globals()["UI_SUBS"].add(q)
    return q


def ui_unsubscribe(q):
    with UI_LOCK:
        globals()["UI_SUBS"].discard(q)


def ui_bump_rev():
    """界面配置变了：版本号 +1，并叫醒所有开着的页面。"""
    global UI_REV
    with UI_LOCK:
        UI_REV += 1
        rev = UI_REV
        subs = list(UI_SUBS)
    for q in subs:
        try:
            q.put_nowait({"kind": "skin", "rev": rev})
        except Exception:
            pass
    return rev


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


