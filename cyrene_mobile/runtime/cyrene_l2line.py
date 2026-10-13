# -*- coding: utf-8 -*-
"""cyrene_l2line —— 二期 L2 记忆接线（`cyrene_web` 拆出来的第九块）。

它原来是什么
------------
把 `cyrene_l2store` / `cyrene_l2write` / `cyrene_l2maintain` 三个子系统接进
对话流程的那一层胶水，外加给面板与排错口用的几个读取函数：

  judge 链       按轮次攒够就调模型抽取候选（`_l2_judge`），失败一律记账
                 （`_L2_LLM_ERRORS` / `_L2_LAST_LLM`）——「她什么都没记住却
                 哪儿都不报错」时，就看这两份
  召回与注入     `build_l2_injection` 把召回到的条目拼成注入块，
                 `l0_calling_note` 把「称呼他 X」拼在最前面
  面板与遗忘     `memory_panel` / `memory_profile_update` / `l2_forget`
  维护回调       `_l2_resolve` / `_l2_compress` / `_l2_decay` / `_l2_after_turn`

它依赖宿主什么
--------------
由宿主经 `bind_deps()` 注入：

  · `settings()`        配置读取（取值函数，SETTINGS 换版也读得到新的）
  · `data_dir()`        数据目录（L2 库文件落在它下面）
  · `sessions()` / `store_lock()`   会话表与它的锁（judge 要读对话原文）
     —— 由 sessions 服务的 `raw_sessions()` / `raw_lock()` 取（这两个取的是
        `cyrene_session` 模块里的**同一个对象**，不走宿主转发名，避免时序问题）
  · `llm_client`        模型客户端类（judge 用它调一次）
  · `memory_cfg` / `memory_status`  记忆配置读取与世界书状态

⚠ `sessions` / `store_lock` / `settings` 缺了会静默走偏（judge 读到空对话、
  或者按默认配置跑），`deps_ready()` 把它们列为必需项。

⚠ 导出 `l2_sched()` 读函数：`_L2_SCHED` 是**可变绑定**（scheduler 单例，惰性构造），
  值转发会让宿主永远读到 None —— 这是跨模块共享可变状态里最容易漏的一种。

红线同前：**绝不 import cyrene_web**，方向只有「宿主 → 内核 → 子系统」。
"""

import threading
from pathlib import Path

# 兜底用：与宿主的 BASE_DIR 同址（cyrene_mobile/）
_BASE_DIR = Path(__file__).parent.parent


__all__ = [
    "_L2_LOCK", "_L2_ROUND", "_L2_CUR", "_L2_JUDGE",
    "_L2_LLM_ERRORS", "_L2_JUDGE_STAT", "_L2_LAST_LLM",
    "L2_INJECT_HEADER", "L2_INJECT_PREAMBLE", "L0_CALLING_KEYS",
    "L2_RECALL_LOCK_TIMEOUT", "L0_EDITABLE", "L1_EDITABLE",
    "_l2s", "_l2w", "_l2m",
    "l2_sched",
    "l2_store_path", "_l2_note_llm_error", "_l2_call_llm", "_l2_load_store",
    "_l2_turns_from_session", "_l2_judge", "_l2_ensure_scheduler",
    "l0_calling_note", "l2_available", "build_l2_injection", "l2_round",
    "l2_panel_data", "l2_forget", "memory_profile_update", "memory_panel",
    "_l2_maint_store", "_l2_resolve", "_l2_compress", "_l2_decay",
    "_l2_after_turn",
    "bind_deps", "deps_ready", "DEPS", "PLUGIN",
]


def l2_sched():
    """当前 scheduler（惰性构造的单例）。

    `_L2_SCHED` 是可变量绑定：宿主那边 `getattr(_L2_SCHED, ...)` 若沿用转发值，
    只会拿到赋初值那一刻的 None。所以要读就读这个函数。
    """
    return _L2_SCHED


def _def_settings():
    """配置还没注入时的空配置。"""
    return {}


def _def_data_dir():
    """数据目录兜底：与宿主的 BASE_DIR/data 同址。"""
    return _BASE_DIR / "data"


def _def_sessions():
    """会话表兜底：空表（judge 会判「没对话」，不产出候选）。"""
    return {}


def _def_store_lock():
    return _STORE_LOCK_FALLBACK


def _def_llm_client(*a, **kw):
    raise RuntimeError("cyrene_l2line：模型客户端还没注入（内核没起来？）")


def _def_memory_cfg(key, fb=None):
    return fb


def _def_memory_status():
    return {}


_STORE_LOCK_FALLBACK = threading.Lock()


class _Deps:
    """宿主注入的资源袋。"""

    __slots__ = ("settings", "data_dir", "sessions", "store_lock",
                 "llm_client", "memory_cfg", "memory_status")

    def __init__(self):
        self.settings = _def_settings
        self.data_dir = _def_data_dir
        self.sessions = _def_sessions
        self.store_lock = _def_store_lock
        self.llm_client = _def_llm_client
        self.memory_cfg = _def_memory_cfg
        self.memory_status = _def_memory_status

    def missing(self):
        # 这三个缺了会静默走偏：judge 读到空对话 / 按默认配置跑 / 写错目录
        need = ("sessions", "store_lock", "settings")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()
_FRESH_DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入。未知的键直接报错，不静默吞掉。"""
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_l2line.bind_deps 不认识这些参数：%s"
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
    """内核在 config / tools / sessions 三个服务齐了之后调这里。"""
    cfg = ctx.require("config")
    tools = ctx.require("tools")
    sess = ctx.require("sessions")

    bind_deps(
        settings=cfg,
        data_dir=tools.data_dir,
        # ⚠ 这两个传的是**取值函数**（sessions 服务给的就是 lambda），
        #   正文里按 `DEPS.sessions()` / `DEPS.store_lock()` 调用 —— 两头必须一致。
        sessions=sess.raw_sessions,
        store_lock=sess.raw_lock,
        llm_client=tools.llm_client,
        memory_cfg=tools.memory_cfg,
        memory_status=tools.memory_status,
    )
    ctx.effect(_reset_deps, desc="l2line:deps")
    ctx.log("已接到内核（依赖 config / tools / sessions）")


PLUGIN = {
    "id": "l2line",
    "requires": ["config", "tools", "sessions"],
    "setup": _plugin_setup,
}


# ===========================================================================
# 以下正文由 _make_l2line_module.py 从 cyrene_web.py 逐字抽出
# ===========================================================================


# ========== 二期 L2 接线（P2-6 / P3）==========
# 与上面的 worldbook 块同处一个文件，但模块导入放在这里：sys.path 已在文件头部补过，
# 所以直接 import。两个模块任一拿不到就整体降级为 None —— 记忆是附加能力，坏了不能拖住对话。
try:
    import cyrene_l2store as _l2s
    import cyrene_l2write as _l2w
    import cyrene_l2maintain as _l2m        # P4：维护链路（resolver / 压缩 / 衰减 / 护栏）
except Exception as _l2_err:                                # pragma: no cover
    _l2s = None
    _l2w = None
    _l2m = None
    print(f"⚠ L2 模块导入失败（二期记忆停用）: {_l2_err}")

# L2 用独立的锁，不复用 DEPS.store_lock() —— 两者覆盖的数据不同，共用一把只会互相拖慢。
# ⚠ 前台（_handle_send）与后台维护线程都会碰它，所以前台取锁必须带超时（见 l2_round）。
_L2_LOCK = threading.Lock()
_L2_SCHED = None            # MemoryScheduler | None
_L2_ROUND = {"n": 0}        # 轮数缓存。选它而不另开一个 roundCount.json：
                            # store 的 l1.roundCount 本就是这个字段（DEFAULT_L1 里就有），
                            # 跟 memory.json 同生共死，少一个文件、少一个落盘点。
_L2_CUR = {"store": None}   # 正在处理的 store；judge 回调从这里取
_L2_JUDGE = {"obj": None}   # MemoryJudge 实例的懒加载容器


def l2_store_path():
    """L2 库路径：data/l2/memory.json；配了 l2StatePath 就用它（与一期 statePath 同构）。"""
    # 配了 l2StatePath 就用它（与一期 statePath 同构）；留空回落 data/l2/memory.json。
    # 这一条不只是给用户留口子 —— 冒烟脚本靠它把库指到临时目录，不碰项目数据。
    p = str(DEPS.memory_cfg("l2StatePath", "") or "").strip()
    return Path(p) if p else (DEPS.data_dir() / "l2" / "memory.json")


# 维护链路的「最近失败原因」。为什么要单独留一份：
#   judge 的错进的是 l2write.JUDGE_ERRORS（进程内），而 LLM 这一层的 error 原先
#   被**静默吞掉** —— 结果就是「她什么都没记住，可哪儿都不报错」，只能靠猜。
#   这两份再由 /memory/status 的 l2Debug 段露出来，一眼就能定位。
_L2_LLM_ERRORS = []
_L2_JUDGE_STAT = {"runs": 0, "turns": 0, "candidates": 0, "afterFilter": 0}
# 模型最近一次吐了什么（截前 800 字）。「候选为 0」时，只有看原文才知道
# 是模型真觉得没什么可记，还是输出格式不对被解析器丢了。
_L2_LAST_LLM = {"len": 0, "text": ""}


def _l2_note_llm_error(msg):
    _L2_LLM_ERRORS.append(str(msg)[:200])
    del _L2_LLM_ERRORS[:-10]                    # 只留最近 10 条


def _l2_call_llm(messages):
    """judge 用的 LLM 回调，签名对齐 LLMClient.chat()：返回 (content, reasoning, error)。

    ⚠ 这里不重试、不抛异常：judge 失败就让这一轮不产出候选，下一轮还会再来。
      超时沿用 DEPS.settings().model.request_timeout 的同一套配置。
    ⚠ 失败**必须记一笔**（`_L2_LLM_ERRORS`），否则上层看到的就是「无事发生」。
    """
    try:
        content, _reasoning, error = DEPS.llm_client(DEPS.settings()).chat(messages)
        if error:
            _l2_note_llm_error("chat 返回 error：%s" % error)
            return ""
        if not (content or "").strip():
            _l2_note_llm_error("chat 返回空正文（模型没吐内容）")
        _L2_LAST_LLM["len"] = len(content or "")
        _L2_LAST_LLM["text"] = (content or "")[:800]
        return content or ""
    except Exception as e:                                  # pragma: no cover
        _l2_note_llm_error("调用异常：%r" % e)
        print(f"⚠ L2 judge 调用 LLM 失败: {e}")
        return ""


def _l2_load_store():
    """读 store 并修 schema：文件不存在 / 读坏了都返回一份全新的 store。"""
    path = l2_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = None
    try:
        if path.exists():
            raw = _l2s.read_memory_file(str(path))
    except Exception as e:
        print(f"⚠ L2 库读取失败（按空库继续）: {e}")
    return _l2s.repair_migrations(raw)


def _l2_turns_from_session(sid, limit=8):
    """从落盘的会话历史里取最近 limit 轮（user/assistant 成对）。

    为什么要它：judge 要看的是「他俩最近聊了什么」，而会话历史是落盘的；
    scheduler 内存里那份 `_recent_turns` 一重启就清零 —— 手机上服务常重启，
    那会让 judge 手里只剩三五轮，甚至把很短的上下文当成全部来判。

    ⚠ 落库的 user 内容是**注入世界书之前**的原文（见 _handle_send 里 entry_u 的位置），
      所以这里取到的就是他真正说过的话。
    ⚠ 只扫尾部（3 倍 limit 条）：会话可能上百条，没必要全遍历。
    """
    if not sid or int(limit) <= 0:
        return []
    try:
        with DEPS.store_lock():
            s = DEPS.sessions().get(sid)
            msgs = list((s or {}).get("messages") or [])
    except Exception:
        return []
    pairs = []
    pending = None
    for m in msgs[-max(2, int(limit) * 3):]:
        role = m.get("role")
        if role == "user":
            pending = str(m.get("content") or "")
        elif role == "assistant" and pending is not None:
            pairs.append({"userInput": pending,
                          "assistantReply": str(m.get("content") or "")})
            pending = None
    return pairs[-int(limit):]


def _l2_judge(turns, conversation_id):
    """scheduler 调 judge 的入口：拿到本轮 store，抽取候选并落库。"""
    store = _L2_CUR.get("store")
    if store is None:
        return
    # 优先用落盘的会话历史（重启动不动都在）；拿不到才退回 scheduler 内存里那份。
    # ⚠ 这里拿 DEPS.store_lock() 是安全的：加锁顺序只有「_L2_LOCK → DEPS.store_lock()」这一个方向，
    #   主线程落库那条路只用 DEPS.store_lock()，不成环，不会死锁。
    try:
        _limit = int(getattr(_L2_SCHED, "context_turns", 8) or 8)
    except Exception:
        _limit = 8
    from_session = _l2_turns_from_session(conversation_id, _limit)
    if from_session:
        turns = from_session
    judge = _L2_JUDGE.get("obj")
    if judge is None:
        judge = _l2w.MemoryJudge(_l2_call_llm, policy_care=_l2w.load_policy_care())
        _L2_JUDGE["obj"] = judge
    result = judge.judge(turns, conversation_id)
    raw_cands = result.get("candidates") or []
    candidates = _l2w.post_filter_candidates(raw_cands)
    _l2w.write_candidates(store, candidates, conversation_id)
    # 统计落一份：判几轮、抽到几条、过滤后剩几条 —— 全 0 也能看出卡在哪一段
    _L2_JUDGE_STAT["runs"] += 1
    _L2_JUDGE_STAT["turns"] = len(turns or [])
    _L2_JUDGE_STAT["candidates"] = len(raw_cands)
    _L2_JUDGE_STAT["afterFilter"] = len(candidates)


def _l2_ensure_scheduler():
    """scheduler 的单例构造；节流值是 6/8/5/20/50，全部走配置。"""
    global _L2_SCHED
    if _L2_SCHED is None:
        _L2_SCHED = _l2w.MemoryScheduler({
            "judge_fn": _l2_judge,
            # P4：三个维护钩子。调度器按 5 / 20 / 50 轮触发，全部跑在后台线程里。
            "resolve_fn": _l2_resolve,
            "compress_fn": _l2_compress,
            "decay_fn": _l2_decay,
            "get_round_count": lambda: int(_L2_ROUND.get("n") or 0),
            "set_round_count": lambda n: _L2_ROUND.__setitem__("n", int(n)),
            # 节流值走配置（P3-5）。0 = 关掉那一项；源码默认 6 / 50。
            "judge_interval": int(DEPS.memory_cfg("judgeInterval", 6) or 0),
            "decay_interval": int(DEPS.memory_cfg("decayInterval", 50) or 0),
            # resolve / compress / decay 由 P4 接线；先留 None，scheduler 会自动跳过。
        })
    return _L2_SCHED


# ---------- P3 召回与注入 ----------
# L2 进 prompt 的文案：**源码没取到这一段** —— 桌面端拼 L2 注入的地方不在
# `src/main/memory/` 里（P0 只拉了那个目录），所以下面是我们自定的措辞，
# 沿用一期世界书的方括号风格。要改口吻就改这一处，别散到别处去。
L2_INJECT_HEADER = "【关于他的记忆】"
L2_INJECT_PREAMBLE = (
    "以下是你记得的、关于他的事。它们来自过去的对话，可能已经过时或记岔了 ——"
    "拿不准就自然地问一句，不要当成刚刚发生的事来说。"
)

# L0 画像里「该怎么称呼他」也得进上下文。
# 为什么单拎出来：L0 是「关于他是谁」的核心画像，之前只显示在记忆面板上，
# **从没进过 prompt** —— 用户在面板里改了称呼，她依然不会用；那句写死的
# 「用户昵称：（可在设置中自定义）」也只是个占位，设置里根本没有这一项。
# 这里只注入称呼这一个字段，别的 L0 字段一律不碰：注入面越小越不容易跟 L2 打架。
L0_CALLING_KEYS = ("preferredName", "nickname")


def l0_calling_note():
    """从 L0 取「该怎么称呼他」，拼成一小句；取不到返回空串。

    只读不写、永不上抛：记忆是附加能力，坏了也不能拖住这一轮对话。
    """
    if _l2s is None:
        return ""
    try:
        store = _l2_load_store()
        l0 = store.get("l0") if isinstance(store, dict) else None
        if not isinstance(l0, dict):
            return ""
        name = ""
        for k in L0_CALLING_KEYS:
            name = str(l0.get(k) or "").strip()
            if name:
                break
        if not name:
            return ""
        return "【关于他】称呼他「%s」。" % name[:30]
    except Exception:
        return ""

# 前台召回与后台维护（judge 要调 LLM，慢）共用一把锁。前台只等这么久，
# 拿不到就跳过本轮注入：记忆是附加能力，宁可这一轮不注入，也不能堵住回复。
L2_RECALL_LOCK_TIMEOUT = 0.15


def l2_available():
    """L2 能不能工作：两个模块都导进来了 + memory.enabled + memory.l2Enabled。

    ⚠ 二级开关的关系：`enabled` 是总闸（关掉整个记忆系统），`l2Enabled` 只关 L2。
      两个都开才跑 —— 与 worldbook 那边的 `worldbookEnabled` 是同一种结构。
    """
    if _l2s is None or _l2w is None:
        return False
    if not DEPS.memory_cfg("enabled", False):
        return False
    return bool(DEPS.memory_cfg("l2Enabled", False))


def build_l2_injection(active, max_chars=1200):
    """把取到的 L2 拼成注入文本。

    超限**按条截断**（与一期同款）：宁可少一条，也不把一句话的后半截塞进 prompt。
    例外是第一条就超限 —— 那也截断它，否则长度设小了会一条都拿不到。
    """
    items = []
    used = 0
    limit = max(0, int(max_chars or 0))
    for l2 in (active or []):
        content = str((l2 or {}).get("content") or "").strip()
        if not content:
            continue
        line = "- " + content
        if limit and used + len(line) > limit:
            if items:
                break
            line = line[:limit]
        items.append(line)
        used += len(line) + 1
    if not items:
        return ""
    return L2_INJECT_HEADER + "\n" + L2_INJECT_PREAMBLE + "\n" + "\n".join(items)


def l2_round(user_text):
    """一轮的 L2 处理：词法召回 → DMAE 更新 → 落盘 → 返回可注入文本。

    ⚠ 与 `_l2_after_turn`（后台线程，会调 LLM）共用 `_L2_LOCK`。这里的等待**带超时**：
      后台正在 judge 时前台不等，直接跳过本轮注入（下一轮再来）。
    ⚠ `model_text` 传空串：请求还没发出去，不知道她会怎么回。一期世界书那条链路
      也是这么做的（memory_round 里 model_hit 恒 False）。拿她的回复再补一轮激活，
      是 P4 维护链路的事。
    ⚠ 全过程不抛异常：附加能力坏了也不能拖住对话（与一期同一条纪律）。
    """
    if not l2_available():
        return ""
    if not _L2_LOCK.acquire(timeout=L2_RECALL_LOCK_TIMEOUT):
        return ""
    try:
        store = _l2_load_store()
        l2_list = _l2s.get_all_l2(store)
        if not l2_list:
            return ""
        text = str(user_text or "")
        recalled = _l2s.recall_l2_ids(text, l2_list,
                                      top_k=int(DEPS.memory_cfg("l2TopK", 8) or 8))
        mgr = _l2s.L2DmaeManager(store)
        mgr.update_activation(l2_list, text, "", recalled)
        active = mgr.get_active_l2_for_prompt(
            l2_list, max_count=int(DEPS.memory_cfg("l2InjectLimit", 4) or 4))
        note = build_l2_injection(active, DEPS.memory_cfg("l2MaxInjectChars", 1200))
        try:
            _l2s.write_memory_file(str(l2_store_path()), store)
        except Exception as e:
            print(f"⚠ L2 状态落盘失败（本轮照常注入）: {e}")
        return note
    except Exception as e:
        print(f"⚠ L2 召回失败（已跳过注入）: {e}")
        return ""
    finally:
        _L2_LOCK.release()


# ---------- P5 面板数据 ----------
# 面板一次拿全（状态 / L0 / L1 / L2 / 反思日志），前端不用拼好几个请求。
# 形状照着「她的事要能被看见」定：L2 每条给出 weight + 三态 + 时间，
# 反思日志给 type + 摘要 + 时间。
def l2_panel_data(limit_logs=50):
    """L2 库的只读快照。拿不到锁时如实返回 busy，不假装是空库。"""
    empty = {"available": False, "busy": False, "items": [], "counts": {}, "logs": []}
    if _l2s is None:
        return empty
    # ⚠ 后台维护（judge / resolver 都要调 LLM）可能正持有这把锁，
    #   面板是只读展示，等不到就下次再看，绝不阻塞。
    if not _L2_LOCK.acquire(timeout=L2_RECALL_LOCK_TIMEOUT):
        busy = dict(empty)
        busy["busy"] = True
        return busy
    try:
        store = _l2_load_store()
        states = {}
        for s in (store.get("l2DmaeStates") or []):
            if isinstance(s, dict):
                states[s.get("l2Id")] = s
        items = []
        counts = {"active": 0, "aging": 0, "archived": 0, "total": 0}
        for l2 in _l2s.get_all_l2(store):
            st = states.get(l2.get("id")) or {}
            status = str(l2.get("status") or "")
            counts["total"] += 1
            if status in counts:
                counts[status] += 1
            items.append({
                "id": l2.get("id"),
                "content": str(l2.get("content") or ""),
                "weight": l2.get("weight"),
                "status": status,
                "activation": round(float(st.get("activation") or 0), 2),
                "isPinned": bool(l2.get("isPinned")),
                "recallCount": l2.get("recallCount"),
                "createdAt": l2.get("createdAt"),
            })
        items.sort(key=lambda x: x.get("createdAt") or 0, reverse=True)
        logs = []
        for log in (_l2s.get_reflection_logs(store) or [])[-int(limit_logs):]:
            if not isinstance(log, dict):
                continue
            logs.append({
                "type": log.get("type"),
                "summary": str(log.get("summary") or ""),
                "details": str(log.get("details") or ""),
                "createdAt": log.get("createdAt"),
            })
        logs.reverse()                      # 新的在前
        return {"available": True, "busy": False, "items": items,
                "counts": counts, "logs": logs}
    except Exception as e:
        print(f"⚠ L2 面板数据读取失败: {e}")
        return empty
    finally:
        _L2_LOCK.release()


def l2_forget(l2_id):
    """删掉一条 L2（库里的条目 + 它的 DMAE 状态行），返回「是否真删到了」。

    ⚠ 状态行不能留孤儿：条目没了还挂着一行 activation/silence，面板三态会数错，
      以后同 id 复用还会串味。
    ⚠ 抽成模块级函数（而不是埋在 HTTP handler 里）是为了能被离线冒烟直接调 ——
      handler 只剩参数校验与响应。
    """
    if _l2s is None:
        return False
    if not _L2_LOCK.acquire(timeout=2.0):
        raise TimeoutError("记忆库正忙")
    try:
        store = _l2_load_store()
        if not _l2s.delete_l2(store, l2_id):
            return False
        states = store.get("l2DmaeStates")
        if isinstance(states, list):
            store["l2DmaeStates"] = [
                s for s in states
                if not (isinstance(s, dict) and s.get("l2Id") == l2_id)]
        _l2s.write_memory_file(str(l2_store_path()), store)
        return True
    finally:
        _L2_LOCK.release()


# L0 / L1 允许手改的字段白名单（面板上那个「改」按钮走这里）。
# ⚠ 为什么不放开整张表：里面还有 updatedAt / generatedAt / roundCount 这类由程序
#   维护的字段，让它们被手改会把时间线和轮数弄乱。isPinned 单独放行（bool）。
L0_EDITABLE = ("nickname", "preferredName", "occupation", "longTermInterests",
               "language", "permanentNote", "isPinned")
L1_EDITABLE = ("recentGoals", "recentPreferences", "currentProject")


def memory_profile_update(patch):
    """改 L0 画像 / L1 近况的字段（面板上的编辑）。

    patch 形如 `{"l0": {"preferredName": "宝宝"}, "l1": {"recentGoals": "…"}}`。

    ⚠ 白名单之外一律**忽略**（不报错也不写）：宁可少写，也不能让程序维护的字段
      被手改坏。若一个都没命中，如实回「没有可改的字段」。
    ⚠ 与所有写库操作一样持 `_L2_LOCK`（带超时）；手改也算一次画像变动，记一条
      反思日志，这样「反思日志」里看得见「谁在什么时候改过什么」。
    """
    if _l2s is None:
        return {"ok": False, "error": "记忆模块未加载"}
    patch = patch if isinstance(patch, dict) else {}
    l0 = patch.get("l0") if isinstance(patch.get("l0"), dict) else {}
    l1 = patch.get("l1") if isinstance(patch.get("l1"), dict) else {}
    if not _L2_LOCK.acquire(timeout=2.0):
        return {"ok": False, "error": "记忆库正忙，稍后再试"}
    try:
        store = _l2_load_store()
        applied = {"l0": [], "l1": []}
        for k, v in l0.items():
            if k not in L0_EDITABLE:
                continue
            if k == "isPinned":
                _l2s.upsert_l0_field(store, k, bool(v))
            else:
                _l2s.upsert_l0_field(store, k, str(v if v is not None else "")[:2000])
            applied["l0"].append(k)
        for k, v in l1.items():
            if k not in L1_EDITABLE:
                continue
            _l2s.replace_l1_field(store, k, str(v if v is not None else "")[:2000])
            applied["l1"].append(k)
        if not applied["l0"] and not applied["l1"]:
            return {"ok": False, "error": "没有可改的字段（都在白名单之外）"}
        _l2s.append_reflection_log(store, {
            "type": "l0_update" if applied["l0"] else "l1_update",
            "summary": "手动编辑：" + "、".join(
                ["L0." + k for k in applied["l0"]] + ["L1." + k for k in applied["l1"]]),
        })
        _l2s.write_memory_file(str(l2_store_path()), store)
        return {"ok": True, "applied": applied}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _L2_LOCK.release()


def memory_panel():
    """GET /memory/panel：面板要的四块一次给全。

    ⚠ 状态块复用一期的 DEPS.memory_status()（世界书条目数与三态、策略摘要、开关都在里面）；
      L2 与反思日志来自 l2_panel_data()。两者都不回显任何密钥。
    ⚠ L0/L1 读的是同一个 store：没开 L2 时给默认空壳，面板不会因为空库报错。
    """
    st = DEPS.memory_status()
    l2 = l2_panel_data()
    l0, l1 = {}, {}
    if _l2s is not None:
        try:
            store = _l2_load_store()
            l0 = dict(_l2s.create_default_l0())
            l0.update(store.get("l0") or {})
            l1 = dict(_l2s.DEFAULT_L1)
            l1.update(store.get("l1") or {})
        except Exception as e:
            print(f"⚠ L0/L1 读取失败（面板按空画像显示）: {e}")
    return {
        "status": st,
        "l0": l0,
        "l1": l1,
        "l2": l2["items"],
        "counts": l2["counts"],
        "reflectionLogs": l2["logs"],
        "l2Available": l2["available"],
        "l2Busy": l2["busy"],
    }


# ---------- P4 维护链路的三个回调 ----------
# 它们由 MemoryScheduler 按节流点调用（默认 每 5 / 20 / 50 轮），跑在 `_l2_after_turn`
# 的后台线程里 —— 那时 _L2_LOCK 已经持有、_L2_CUR["store"] 就是本轮 store。
# ⚠ 这三个函数**不要自己取锁**：_L2_LOCK 是不可重入的 threading.Lock，再取一次会死锁。
# ⚠ _l2m 导入失败时整段降级成 no-op：维护链路停用，但对话与召回照常。


def _l2_maint_store():
    """取本轮正在处理的 store；拿不到就返回 None（说明不在维护上下文里）。"""
    if _l2m is None or _l2s is None:
        return None
    return _L2_CUR.get("store")


def _l2_resolve(_count):
    """每 5 轮：处理冲突队列，LLM 判定两条记忆的关系并落库。"""
    store = _l2_maint_store()
    if store is None:
        return
    try:
        # ⚠ 每次最多处理 3 条：resolve 是「一条冲突一次 LLM」。用模块默认的 20，
        #   队列满时会在这个后台线程里串行打 20 次，持着 _L2_LOCK 几十秒到几分钟 ——
        #   那期间前台 l2_round 全部超时跳过，表现为「她突然记不起事」。
        #   剩下的留到下一个 5 轮，反正队列不会跑。
        _l2m.MemoryResolver(_l2_call_llm).resolve_once(store, limit=3)
    except Exception as e:
        print(f"⚠ L2 冲突裁决失败: {e}")


def _l2_compress(_count):
    """每 20 轮：相似 L2 合并 + 画像反思（L0/L1 更新）。"""
    store = _l2_maint_store()
    if store is None:
        return
    try:
        _l2m.MemoryCompressor(_l2_call_llm).compress_once(store)
    except Exception as e:
        print(f"⚠ L2 压缩失败: {e}")


def _l2_decay(_count):
    """每 50 轮：权重衰减一轮，随后跑一次体积护栏。

    ⚠ 顺序不能反：先衰减把冷条目降下去，护栏再按 weight 归档才挑得准。
    ⚠ 护栏上限用 maintain 模块的默认值（L2_MAX_ENTRIES = 300，手机端自定）。
    """
    store = _l2_maint_store()
    if store is None:
        return
    try:
        _l2m.run_decay(store)
        _l2m.enforce_l2_limit(store)
    except Exception as e:
        print(f"⚠ L2 衰减失败: {e}")


def _l2_after_turn(user_text, assistant_text, sid):
    """一轮结束后登记进 scheduler，命中节流点才调 LLM。**跑在后台线程，不阻塞回复。**

    只在总闸与 L2 开关都开着时才做事；任一关着就直接 return，行为与改动前一致。
    全程持 _L2_LOCK（与 DEPS.store_lock() 分开），做完把 store 的 l1.roundCount 落盘。
    """
    # ⚠ 判据用 l2_available()（总闸 + L2 开关 + 模块都在），不是只判总闸：
    #   只开总闸、不开 L2 时，后台照样会跑 judge —— 每次都调 LLM、写 L2 库，
    #   而注入侧又不注入，等于白花额度、还留下一份面板上看不见的数据。
    if not l2_available():
        return
    try:
        with _L2_LOCK:
            store = _l2_load_store()
            _L2_ROUND["n"] = int((store.get("l1") or {}).get("roundCount") or 0)
            _L2_CUR["store"] = store
            try:
                _l2_ensure_scheduler().after_turn(
                    str(user_text or ""), str(assistant_text or ""), str(sid or ""))
            finally:
                _L2_CUR["store"] = None
            store["l1"]["roundCount"] = int(_L2_ROUND.get("n") or 0)
            _l2s.write_memory_file(str(l2_store_path()), store)
    except Exception as e:
        print(f"⚠ L2 后台维护失败: {e}")


