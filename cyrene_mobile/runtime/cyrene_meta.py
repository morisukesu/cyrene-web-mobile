# -*- coding: utf-8 -*-
"""cyrene_meta —— Agent 元工具层（`cyrene_web` 拆出来的第六块）。

它原来是什么
------------
「不碰手机硬件、只操作 Agent 自身状态」的那批工具，外加一整套命令执行：

  会话工作笔记   `normalize_todos` + `update_todo`（整表替换、单一 in_progress、
                 落 session.todos 后立刻落盘）
  提问           `_normalize_ask` + `ask_user`（排他工具；真正「终止本轮 loop」
                 由宿主的 run_tool 读 `EXCLUSIVE_TOOLS` 决定）
  技能按需加载   `invoke_skill` / `read_skill_reference` —— 只读 skills/ 下的
                 SKILL.md 与 references/，路径守卫拦穿越
  命令执行       **`shell`（run_shell 本体）+ `shell_job`（查/停后台任务）**：
                 进程组隔离（start_new_session + killpg）、前后台双模、
                 后台任务登记簿与日志尾部回读

它依赖宿主什么
--------------
由宿主经 `bind_deps()` 注入：

  会话
    · `sessions_get(sid)` / `sessions_set_todos(sid, items)`
      注意：`_session_get` / `_session_set_todos` **本体留在宿主**（它们是会话层的
      访问器，宿主自己也有两处在用），这里拿到的只是注入进来的函数。

  技能
    · `skills_root()` / `skills_list(force)` / `skills_parse(text)`

  工具层
    · `outcome_success` / `outcome_failure` / `outcome_unknown`
    · `int_arg` / `bool_arg` / `decode_bytes` / `truncate_output`
    · `agent_cfg(key, fb)`   读 agent 段配置（依赖宿主的 AGENT_RANGES 默认值表，
                             所以注入的是**函数**，不是值）

  文件层
    · `guard_path` / `path_guard_error` / `max_file_bytes()`

⚠ 缺了会**静默走偏**的那几个（`guard_path` / `path_guard_error` / 三个 skills /
  两个 sessions）列在 `deps_ready()` 里，启动自检要盯；其余都有内置兜底。

红线同前：**绝不 import cyrene_web**，方向只有「宿主 → 内核 → 子系统」。
"""

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path


__all__ = [
    "META_TODO_MAX", "META_TODO_TEXT", "TODO_STATUSES",
    "ASK_MAX_QUESTIONS", "ASK_MAX_OPTIONS", "ASK_TEXT_LIMIT", "ASK_OPT_LIMIT",
    "EXCLUSIVE_TOOLS",
    "SHELL_JOBS", "SHELL_JOBS_LOCK", "SHELL_JOB_DIRNAME", "SHELL_JOB_TAIL",
    "SHELL_JOB_KEEP", "SHELL_JOB_MAX_SEC", "SHELL_TIMEOUT_MS_RANGE",
    "normalize_todos", "_normalize_ask",
    "_h_update_todo", "_h_ask_user",
    "_skills_root", "_guard_skill_path", "_find_skill_dir", "_list_skill_refs",
    "_h_invoke_skill", "_h_read_skill_reference",
    "_shell_job_dir", "_job_snapshot", "_job_kill", "_job_reap", "_jobs_prune",
    "_shell_timeout", "_shell_bg_log_path", "_shell_kill_tree",
    "_h_shell", "_h_shell_job",
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


def _def_bool_arg(v, fb=False):
    if isinstance(v, bool):
        return v
    if v is None:
        return fb
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return fb


def _def_decode_bytes(raw):
    """字节 → 文本的兜底解码，返回 (text, encoding)。"""
    if isinstance(raw, str):
        return raw, "str"
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, AttributeError):
            continue
    return "", "?"


def _def_truncate_output(text, limit=None):
    """兜底：原样返回（不知道 maxOutputChars 就不擅自截）。"""
    return text


def _def_agent_cfg(key, fb=None):
    return fb


def _def_max_file_bytes():
    """文件读取上限的兜底（4 MiB，与 FS_MAX_FILE_BYTES 同量级）。"""
    return 4 * 1024 * 1024


class _Deps:
    """宿主注入的资源袋。"""

    __slots__ = ("settings",
                 "outcome_success", "outcome_failure", "outcome_unknown",
                 "int_arg", "bool_arg", "decode_bytes", "truncate_output",
                 "agent_cfg",
                 "guard_path", "path_guard_error", "max_file_bytes",
                 "skills_root", "skills_list", "skills_parse",
                 "sessions_get", "sessions_set_todos")

    def __init__(self):
        self.settings = _def_settings
        self.outcome_success = "success"
        self.outcome_failure = "failure"
        self.outcome_unknown = "unknown"
        self.int_arg = _def_int_arg
        self.bool_arg = _def_bool_arg
        self.decode_bytes = _def_decode_bytes
        self.truncate_output = _def_truncate_output
        self.agent_cfg = _def_agent_cfg
        # 下面这七个缺了会静默走偏（守卫失效 / 技能与 todo 用不了），不给兜底
        self.guard_path = None
        self.path_guard_error = None
        self.max_file_bytes = _def_max_file_bytes
        self.skills_root = None
        self.skills_list = None
        self.skills_parse = None
        self.sessions_get = None
        self.sessions_set_todos = None

    def missing(self):
        need = ("guard_path", "path_guard_error",
                "skills_root", "skills_list", "skills_parse",
                "sessions_get", "sessions_set_todos")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()
_FRESH_DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入。未知的键直接报错，不静默吞掉。"""
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_meta.bind_deps 不认识这些参数：%s"
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
    """内核在 config / tools / fs / sessions / skills 五个服务齐了之后调这里。"""
    cfg = ctx.require("config")
    tools = ctx.require("tools")
    host = ctx.require("fs")
    sess = ctx.require("sessions")
    skl = ctx.require("skills")

    bind_deps(
        settings=cfg,
        outcome_success=tools.outcome_success,
        outcome_failure=tools.outcome_failure,
        outcome_unknown=tools.outcome_unknown,
        int_arg=tools.int_arg,
        bool_arg=tools.bool_arg,
        decode_bytes=tools.decode_bytes,
        truncate_output=tools.truncate_output,
        agent_cfg=tools.agent_cfg,
        guard_path=host.guard_path,
        path_guard_error=host.path_guard_error,
        max_file_bytes=tools.max_file_bytes,
        skills_root=skl.root,
        skills_list=skl.list,
        skills_parse=skl.parse,
        sessions_get=sess.get,
        sessions_set_todos=sess.set_todos,
    )
    ctx.effect(_reset_deps, desc="meta:deps")
    ctx.log("已接到内核（依赖 config / tools / fs / sessions / skills）")


PLUGIN = {
    "id": "meta",
    "requires": ["config", "tools", "fs", "sessions", "skills"],
    "setup": _plugin_setup,
}


# ===========================================================================
# 以下正文由 _make_meta_module.py 从 cyrene_web.py 逐字抽出
# ===========================================================================


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


def _h_update_todo(a, ctx=None):
    """update_todo：整表替换当前会话的工作笔记。

    语义对齐桌面端 mutable working notebook：传进来的是**完整新表**，
    不是增量。模型每轮把全量 todos 重写一遍，Runtime 负责校验与落盘。
    """
    sid = (ctx or {}).get("sid")
    items, notes, err = normalize_todos(a.get("todos"))
    if err:
        return DEPS.outcome_failure, err

    s = DEPS.sessions_get(sid)
    if s is None:
        # 会话不在内存里（单元测试直接调 handler、或会话刚被删）。
        # 不能静默成功 —— 模型会以为笔记存下来了，下一轮接着引用。
        return DEPS.outcome_failure, (
            "(当前没有可用会话上下文，todo 未能保存。"
            "这通常发生在脱离对话流程直接调用工具时)")

    if not DEPS.sessions_set_todos(sid, items):
        return DEPS.outcome_failure, "(todo 写入会话失败：会话不存在或落盘失败)"

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
    return DEPS.outcome_success, "\n".join(lines)


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
        return DEPS.outcome_failure, err
    kinds = {"single_select": "单选", "multi_select": "多选", "text": "自由填写"}
    lines = ["（问题已提交给用户，本轮工具循环终止，等用户回答后再继续）"]
    for q in qs:
        lines.append(f"- [{kinds.get(q['type'], q['type'])}] {q['question']}")
        for o in q["options"] or []:
            lines.append(f"    · {o['label']}")
    if notes:
        lines.append("格式修正：" + "；".join(notes))
    return DEPS.outcome_success, "\n".join(lines)


# ---------- 技能按需加载 ----------
def _skills_root():
    """技能根目录。每次现算，方便单元测试改模块全局。"""
    return Path(DEPS.skills_root())


def _guard_skill_path(rel, want="file"):
    """技能目录专用路径守卫，入参是相对 DEPS.skills_root() 的路径。

    为什么不复用 guard_path：后者的 root 是 FS_ROOT，可被 CYRENE_FS_ROOT
    改写。本机跑验证脚本时沙箱把 FS_ROOT 指到 tempfile 目录，技能目录立刻
    整体「越界」，工具假失败。技能是只读资源，守卫职责只有两条：
    必须落在 DEPS.skills_root() 内、必须真是文件/目录。
    """
    root = _skills_root()
    if not isinstance(rel, str) or not rel.strip():
        raise DEPS.path_guard_error("(路径为空)")
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
        raise DEPS.path_guard_error(f"(路径无法解析: {e})")
    if ap != root and root not in ap.parents:
        raise DEPS.path_guard_error(f"(路径越界：只允许访问技能目录 {root} 之内，拒绝 {ap})")
    try:
        rp = Path(os.path.realpath(str(ap)))
        real_root = Path(os.path.realpath(str(root)))
    except (OSError, ValueError) as e:
        raise DEPS.path_guard_error(f"(路径无法解析: {e})")
    if rp != real_root and real_root not in rp.parents:
        raise DEPS.path_guard_error(f"(路径经符号链接指向技能目录之外，已拒绝: {rp})")
    if want == "file" and not ap.is_file():
        raise DEPS.path_guard_error(f"(文件不存在或不是普通文件: {ap})")
    if want == "dir" and not ap.is_dir():
        raise DEPS.path_guard_error(f"(目录不存在: {ap})")
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
    for info in DEPS.skills_list():
        if info.get("id") == sid:
            d = root / sid
            if d.is_dir():
                return d, None
    known = [i["id"] for i in DEPS.skills_list()]
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
        return DEPS.outcome_failure, err
    sk = d / "SKILL.md"
    refs = _list_skill_refs(d)
    if not sk.is_file():
        return DEPS.outcome_failure, (
            f"(技能 {d.name} 没有 SKILL.md"
            + (f"，但有附件：{', '.join(refs)}" if refs else "") + ")")
    try:
        text = sk.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return DEPS.outcome_failure, f"(读取技能失败: {e})"

    meta, body = DEPS.skills_parse(text)
    body = body.strip()
    if not body:
        return DEPS.outcome_failure, f"(技能 {d.name} 的 SKILL.md 正文为空)"

    head = [f"=== Skill: {meta.get('name') or d.name} ==="]
    if meta.get("description"):
        head.append(str(meta["description"]))
    if meta.get("version"):
        head.append(f"version: {meta['version']}")
    head.append(f"正文 {len(body)} 字" + (f" · 附件 {len(refs)} 个" if refs else ""))
    if refs:
        head.append("可用附件（用 read_skill_reference 读）：" + ", ".join(refs))
    out = "\n".join(head) + "\n\n" + body
    max_chars = DEPS.int_arg(a.get("max_chars"), 0, 0, 200000)
    if max_chars and len(out) > max_chars:
        dropped = len(out) - max_chars
        out = out[:max_chars] + f"\n…（按 max_chars 截断，丢弃 {dropped} 字）"
    return DEPS.outcome_success, out


def _h_read_skill_reference(a):
    """read_skill_reference：读技能 references/ 下的附件，按行返回带行号。"""
    d, err = _find_skill_dir(a.get("skill_id"))
    if err:
        return DEPS.outcome_failure, err
    ref = str(a.get("ref") or "").strip()
    if not ref:
        refs = _list_skill_refs(d)
        return DEPS.outcome_failure, (
            "(缺少 ref 文件名。"
            + (f"该技能可用附件：{', '.join(refs)}" if refs else "该技能没有附件")
            + ")")
    try:
        # 守卫按「技能目录名/references/ref」整体校验，ref 里的 ../ 被 abspath 拦下
        p = _guard_skill_path(str(Path(d.name) / "references" / ref), want="file")
    except DEPS.path_guard_error as e:
        return DEPS.outcome_failure, str(e)
    try:
        size = p.stat().st_size
        if size > DEPS.max_file_bytes():
            return DEPS.outcome_failure, f"(附件太大 {size} 字节，上限 {DEPS.max_file_bytes()})"
        data = p.read_bytes()
    except OSError as e:
        return DEPS.outcome_failure, f"(读取失败: {e})"
    if b"\x00" in data[:8192]:
        return DEPS.outcome_failure, "(这是二进制文件，不能当文本读)"

    text, enc_note = DEPS.decode_bytes(data)
    lines = text.split("\n")
    total = len(lines)
    # offset **不夹到 total**：夹了的话模型传 offset=999 会静默拿到最后一行，
    # 它会以为读到了第 999 行附近的内容 —— 静默错位比报错危险得多。
    # 与 _h_read_file 同一套语义（越界直接 failure，让模型改 offset 重读）。
    offset = DEPS.int_arg(a.get("offset"), 1, 1, 10 ** 9)
    limit = DEPS.int_arg(a.get("limit"), 400, 1, 20000)
    if offset > total:
        return DEPS.outcome_failure, (
            f"(offset={offset} 超出范围：附件共 {total} 行。"
            f"要续读请用 offset<={total})")
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return DEPS.outcome_failure, f"(offset={offset} 超出范围，文件共 {total} 行)"
    width = len(str(offset - 1 + len(chunk)))
    body = "\n".join(f"{offset + i:>{width}} | {ln}" for i, ln in enumerate(chunk))
    left = total - (offset - 1 + len(chunk))
    head = (f"附件: {d.name}/references/{p.name}  共 {total} 行"
            f"  本次 {offset}-{offset + len(chunk) - 1}"
            + (f"  （还有 {left} 行，可加大 offset 续读）" if left > 0 else "")
            + (enc_note or ""))
    return DEPS.outcome_success, head + "\n" + body


# ---------- 后台 shell 任务 ----------
def _shell_job_dir():
    """后台任务日志目录（FS_ROOT/shell_jobs），不存在则创建。"""
    d = DEPS.guard_path(SHELL_JOB_DIRNAME, want="any")
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
        txt, _ = DEPS.decode_bytes(raw)
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
    ms = DEPS.int_arg(a.get("timeout_ms"), 0, 0, SHELL_TIMEOUT_MS_RANGE[1])
    if ms > 0:
        lo, hi = SHELL_TIMEOUT_MS_RANGE
        return max(lo, min(hi, ms)) / 1000.0
    return float(DEPS.agent_cfg("stepTimeout"))


def _shell_bg_log_path(jid):
    """后台任务日志文件路径。目录不可用时回落系统临时目录，绝不因日志写不了就不跑命令。"""
    try:
        return _shell_job_dir() / f"{jid}.log"
    except DEPS.path_guard_error:
        return Path(tempfile.gettempdir()) / f"cyrene_{jid}.log"


def _h_shell(a):
    """shell：执行一条命令。前台阻塞拿输出，或后台起任务立刻返回 jobId。

    进程组隔离与 run_tool 的 cmd 分支同款（start_new_session + killpg）：
    超时只 kill `sh -c` 那层的话，它 fork 出来的孙子进程会变孤儿继续跑，
    上轮 termux-api 探测实测攒了 12 行残留。
    """
    cmd_str = str(a.get("cmd") or "").strip()
    if not cmd_str:
        return DEPS.outcome_failure, "(缺少 cmd)"
    bg = DEPS.bool_arg(a.get("run_in_background"), False)
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
            return DEPS.outcome_failure, f"(后台日志文件打不开: {e})"
        try:
            p = subprocess.Popen(["sh", "-c", cmd_str], stdout=logf,
                                 stderr=subprocess.STDOUT, **popen_kw)
        except (OSError, ValueError) as e:
            try:
                logf.close()
            except OSError:
                pass
            return DEPS.outcome_failure, f"(后台任务启动失败: {type(e).__name__}: {e})"
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
        return DEPS.outcome_success, json.dumps({
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
            return DEPS.outcome_unknown, (
                f"(执行超过 {timeout:.1f}s 被中止，已杀整个进程组；"
                f"命令可能已部分生效，请勿盲目重试。"
                f"长任务请改用 run_in_background=true)")
        out = (stdout or "").strip()
        err_out = (stderr or "").strip()
        if p.returncode == 0:
            return DEPS.outcome_success, DEPS.truncate_output(out) if out else "(无输出)"
        detail = out or err_out or "(无输出)"
        if out and err_out:
            detail = out + "\n[stderr] " + err_out
        return DEPS.outcome_failure, f"(退出码 {p.returncode}) {DEPS.truncate_output(detail)}"
    except FileNotFoundError:
        return DEPS.outcome_failure, "(sh 不存在：这不是一个 POSIX shell 环境)"
    except Exception as e:
        if p is not None and p.poll() is None:
            _shell_kill_tree(p)
        return DEPS.outcome_failure, f"(失败: {type(e).__name__}: {e})"


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
        return DEPS.outcome_failure, f"(action 只能是 status 或 stop，收到 {action!r})"
    jid = str(a.get("job_id") or "").strip()
    if not jid:
        return DEPS.outcome_failure, "(缺少 job_id。它是 run_in_background 返回的那个 id)"

    with SHELL_JOBS_LOCK:
        j = SHELL_JOBS.get(jid)
        if j is None:
            known = sorted(SHELL_JOBS.keys())
            return DEPS.outcome_failure, (
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
            return DEPS.outcome_success, json.dumps({
                "action": "stop", "job": snap,
                "note": ("（已按进程组终止，孙子进程一并清掉）" if was_running
                         else f"（任务早已是 {snap['status']}，本次没杀任何东西）"),
            }, ensure_ascii=False)
        _jobs_prune()
    return DEPS.outcome_success, json.dumps({"action": "status", "job": snap},
                                       ensure_ascii=False)


