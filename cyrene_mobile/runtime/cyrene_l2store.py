#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
昔涟 · 手机端 L2 长期记忆「纯算法层」—— 桌面端 Cyrene-Agent 的存储 / 冲突 / 画像 / L2-DMAE 移植。

对应桌面端源码（src/main/）：
  memory/memory-store.ts            存储主体（方法清单、默认值、上限）
  memory/memory-store-defaults.ts   默认 store 骨架、extractMemoryKeywords、boundMemorySnippet
  memory/memory-store-io.ts         读写（本模块改为原子写，见下）
  memory/memory-store-migrations.ts schema 迁移 repairMigrations
  memory/memory-conflict.ts         词法冲突（9 条正反义对 + STOP_TERMS + bigram 主题词）
  memory/memory-conflict-score.ts   冲突打分与 resolver 优先级
  memory/l2-dmae-manager.ts         L2 的 DMAE 参数与位次 I 梯度
  relationship/relationship-log.ts  关系画像（情绪正则 + 固定文案 + 上限截断）
  memory-manager.ts                 纠正意图关键词、L1 字段白名单与兜底正则（只搬常量与纯函数）

移植边界（为什么只搬这些）：
  只搬纯算法与纯数据结构。桌面端 MemoryStoreManager 外层那些 Electron / fs.watch /
  logger / Obsidian 回调 / 向量索引的壳全部剥掉：本模块不 import 任何第三方库、
  不读写模型文件、不发网络请求。store 与文件路径全部由调用方注入，因此本机可直接跑单测。

⚠ 两条贯穿全模块的约定，别踩：
 1) store 里的键名一律保持桌面端 JSON 的 **camelCase**（schemaVersion / lastAccessedAt /
    isPinned / l2DmaeStates …）。为的是能和桌面端 `memory.json` **逐字节互认**——改成
    snake_case 会让两边文件互不兼容。Python 侧的驼峰看着别扭，但这是刻意的。
 2) 所有以 `store` 为入参的函数都是**就地改这个 dict**（等价于桌面端 `load() → 改 → save()`
    里的「改」那一段）。落盘与否由调用方决定：桌面端每次改完必 `save()`，本模块把它拆开，
    是为了让纯算法可单独测；接线时**别忘了调 write_memory_file**，否则改动不落盘。

⚠ 与源码的三处有意偏差（都不改行为，只改实现手段）：
 1) `write_memory_file` 用「同目录临时文件 + os.replace」原子替换（源码是直接
    writeFileSync，写到一半崩就剩半个 JSON）。⚠ 临时文件必须与目标**同盘同目录**，
    否则 Windows 上 os.replace 会跨卷失败。
 2) `relationship-log` 的落盘也走同一个原子写（源码直接 writeFileSync）；语义不变。
 3) 备份文件名与源码同形（UTC、冒号与点换成横线），但**同一毫秒内连续备份会互相覆盖**
    —— 这是源码 copyFileSync 的原样行为，没顺手加后缀（加了就不是同一个命名空间了）。

⚠ 字符长度口径：源码 `String.length` / `slice` 数的是 **UTF-16 code unit**，Python `len` /
切片数的是 **code point**。纯中文与 BMP 内字符两者一致；一旦有 emoji（代理对）等
非 BMP 字符，截断长度会差 1~2 个字符。凡涉及「上限截断」的地方（quoteSnippet 300、
sourceQuote 500、compact 120/500）都受此影响。**未做**转换——要完全对齐得先做
UTF-16 编码再切，本批次没做，标为「已知偏差」。
"""

import json
import math
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

__all__ = [
    # 路径 / 版本
    "BASE_DIR", "DATA_DIR", "DEFAULT_MEMORY_FILE", "RELATIONSHIP_LOG_FILE",
    "CURRENT_MEMORY_SCHEMA_VERSION", "resolve_memory_path",
    # P1-1 存储层
    "create_default_l0", "DEFAULT_L1", "create_default_memory_store",
    "extract_memory_keywords", "bound_memory_snippet",
    "memory_file_exists", "read_memory_file", "write_memory_file", "backup_memory_file",
    "repair_migrations",
    # P1-2 L2 条目操作
    "add_l2", "add_l2_batch", "update_l2_recall_stats", "update_l2_weight",
    "pin_l2", "delete_l2", "mark_l2_sync_status", "mark_l2_conflict",
    "update_l2_content", "get_all_l2", "get_evidence_by_memory_id",
    "update_l2_status", "archive_l2_batch",
    "upsert_l0_field", "update_l0", "replace_l1_field", "update_l1",
    "append_reflection_log", "get_reflection_logs",
    "append_conflict_log", "get_conflict_logs",
    "score_conflict_log", "get_resolver_queue", "apply_resolver_resolution",
    "get_l2_dmae_state", "get_all_l2_dmae_states",
    "update_l2_dmae_state", "init_l2_dmae_state_if_missing",
    # P1-3 衰减
    "decay_l2_weights",
    # 其他照抄常量（memory-manager.ts）
    "CORRECTION_INTENT_KEYWORDS", "L1_FIELD_WHITELIST",
    "has_correction_intent", "guess_l1_field", "resolve_l1_field",
    # P1-4 词法冲突
    "CONTRADICTION_PAIRS", "STOP_TERMS",
    "extract_topic_terms", "has_shared_topic", "find_possible_conflict_candidate",
    # P1-5 冲突打分
    "RESOLVER_PRIORITY_RANK", "clamp_score", "priority_for",
    "rag_points", "evidence_points", "impact_points", "score_memory_conflict",
    # P1-6 关系画像
    "MAX_ENTRIES", "MAX_DAILY_SUMMARIES",
    "local_date", "compact", "detect_user_mood", "derive_signal",
    "summarize_date", "RelationshipLogStore",
    "record_turn", "build_context", "record_relationship_turn", "build_relationship_context",
    # P1-7 L2 的 DMAE
    "L2_DMAE_PARAMS", "L2_INTRINSIC_BY_RANK", "intrinsic_for_rank",
    "l2_to_entry", "L2DmaeManager", "HAS_CYRENE_MEMORY", "M",
]


# ========== 路径（不写死盘符，一律相对本模块推导） ==========
# cyrene_mobile/runtime/cyrene_l2store.py → BASE_DIR = cyrene_mobile/
BASE_DIR = Path(__file__).resolve().parent.parent
# 与 cyrene_web.py 的 DATA_DIR 同处（cyrene_mobile/data/），备份脚本不用多认一个目录
DATA_DIR = BASE_DIR / "data"
DEFAULT_MEMORY_FILE = DATA_DIR / "memory.json"
RELATIONSHIP_LOG_FILE = DATA_DIR / "relationship-log.json"


def resolve_memory_path() -> str:
    """
    memory.json 的默认路径。

    桌面端是 `app.getPath("userData")/memory.json`（memory-store-io.ts:6-13，Electron 主进程
    外取不到就返回 null → 放弃持久化）。[降级] 手机端没有 Electron，改为按本模块位置推导，
    **绝不写死盘符**：换设备/换部署目录不用改代码。
    """
    return str(DEFAULT_MEMORY_FILE)


# ========== 复用一期引擎（同目录 cyrene_memory.py） ==========
# ⚠ 本机 python 是嵌入式发行版，sys.path 不自带脚本目录：直接 `import cyrene_memory`
#   会 ModuleNotFoundError。必须先显式把本文件所在目录塞进 sys.path。
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import cyrene_memory as M  # 一期已验收的 DMAE 世界书引擎（纯算法）
    HAS_CYRENE_MEMORY = True
except ImportError as _err:  # pragma: no cover - 只在本模块被单独拷走时发生
    # 不静默吞掉：其余部分（存储/冲突/画像）不依赖它，仍可用；只有 L2DmaeManager 会报错。
    M = None  # type: ignore[assignment]
    HAS_CYRENE_MEMORY = False
    _CYRENE_MEMORY_IMPORT_ERROR = _err
else:
    _CYRENE_MEMORY_IMPORT_ERROR = None


# ========== 常量（数值一律来自源码，勿凭「看着合理」改动） ==========
# 来源：memory-store-defaults.ts:4
CURRENT_MEMORY_SCHEMA_VERSION = 2

# 来源：memory-store.ts:21（evidence.quoteSnippet 上限）
QUOTE_SNIPPET_MAX = 300

# 来源：memory-store.ts:22-27（resolver 优先级权重）
RESOLVER_PRIORITY_RANK: Dict[str, int] = {
    "high": 3,
    "normal": 2,
    "idle": 1,
    "none": 0,
}

# 来源：memory-store.ts:383-385 / :413-415（日志保留上限，超出 slice(-N)）
REFLECTION_LOG_MAX = 50
CONFLICT_LOG_MAX = 100

# 来源：memory-manager.ts:29（纠正意图关键词表，逐字）
CORRECTION_INTENT_KEYWORDS: Tuple[str, ...] = (
    "不是这样",
    "你记错了",
    "记错了",
    "我现在不这样",
    "现在不这样",
)

# 来源：memory-manager.ts:19（L1 合法字段白名单）
L1_FIELD_WHITELIST: Tuple[str, ...] = ("recentGoals", "recentPreferences", "currentProject")

# 来源：memory-manager.ts:19-23（L1 兜底正则，顺序即优先级）
_RE_L1_GOALS = re.compile(r"目标|想要|计划|打算")
_RE_L1_PROJECT = re.compile(r"项目|在做|开发|写")

_ALPHA36 = "0123456789abcdefghijklmnopqrstuvwxyz"


# ========== 小工具 ==========
def _now_ms() -> int:
    """毫秒时间戳，对齐桌面端 Date.now()（不是秒）。"""
    return int(time.time() * 1000)


def _rand6() -> str:
    """
    6 位 36 进制随机串，对齐源码 `Math.random().toString(36).slice(2, 8)`。

    ⚠ 不是密码学随机（源码 Math.random 也不是），只用来给 id 尾部去重；
      id 唯一性主要靠毫秒时间戳，同毫秒 + 同随机串的碰撞概率可忽略。
    """
    x = random.random()
    out = []
    for _ in range(6):
        x *= 36.0
        d = int(x)
        if d > 35:  # 浮点边界保护（x 理论 < 36，防御性夹一下）
            d = 35
        out.append(_ALPHA36[d])
        x -= d
    return "".join(out)


def _nn(value: Any, fallback: Any) -> Any:
    """
    `value ?? fallback`（仅 None 走兜底）。

    ⚠ 不能用 `value or fallback`：源码 `??` 只认 null/undefined，空字符串与 0 都是**有效值**
      （例如 nextCareCue="" 不能被换成默认文案，resolverQueuedAt=0 也不能被顶掉）。
    """
    return fallback if value is None else value


def _js_round(x: float) -> int:
    """
    `Math.round` 语义的取整。

    ⚠ Python 的 round() 是**银行家舍入**（74.5 → 74），JS 的 Math.round 是**四舍五入**
      （74.5 → 75）。这一位之差会跨过 75 的优先级门槛，所以不能直接用 round()。
      负数也对齐：Math.round(-0.5) = -0 → 0，floor(-0.5+0.5) = 0。
    """
    return int(math.floor(x + 0.5))


def _atomic_write_bytes(path: Any, data: bytes) -> None:
    """
    原子写（字节版）：同目录临时文件 → os.replace 覆盖。

    ⚠ 临时文件必须落在目标**同一目录**（同卷），否则 Windows 的 os.replace 会跨卷失败；
      这也顺手保证了 json.load 永远看不到半个文件（断电/被杀进程时的老坑）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Any, text: str) -> None:
    """
    原子写（文本版）：UTF-8 编码后走字节版。

    ⚠ 走 `encode("utf-8")` + 二进制写，**而不是**文本模式：文本模式在 Windows 上会把
      `\\n` 翻成 `\\r\\n`，文件一进 git 就与桌面端（writeFileSync，LF）产生整篇 diff。
      编码路径同时保证中文原样落盘，不需要额外开 newline 参数。
    """
    _atomic_write_bytes(path, text.encode("utf-8"))


# ========== P1-1 存储层 ==========
def create_default_l0() -> Dict[str, Any]:
    """
    默认 L0 画像（memory-store-defaults.ts:6-17）。

    ⚠ `language` 在桌面端取 `getMemoryLanguage()`（locale-context）。[降级] 手机端固定 "zh"
      ——这是本批次唯一一处「值来源不同」的默认值，已在跨查报告 4.3 标注；要改就注入 locale 回调。
    """
    return {
        "nickname": "",
        "preferredName": "",
        "occupation": "",
        "longTermInterests": "",
        "language": "zh",
        "permanentNote": "",
        "isPinned": False,
        "updatedAt": 0,
    }


# 来源：memory-store-defaults.ts:19-25
DEFAULT_L1: Dict[str, Any] = {
    "recentGoals": "",
    "recentPreferences": "",
    "currentProject": "",
    # ⚠ generatedAt 在源码里**未见任何写入点**（l2-prompts.md §5.3 标为「是否死字段：未确认」）。
    #   本模块照抄保留，不做删除也不做写入。
    "generatedAt": 0,
    "roundCount": 0,
}


# 来源：memory-store-defaults.ts:27-36（DEFAULT_STORE）。骨架在 create_default_memory_store 里
# 逐字段显式列出（不照抄浅拷，见该函数注释）。


def create_default_memory_store() -> Dict[str, Any]:
    """
    干净的空 store（memory-store-defaults.ts:38-49）。

    顶层 **9 字段**：schemaVersion / l0 / l1 / l2 / evidence / reflectionLogs /
    conflictLogs / l2DmaeStates / version(@deprecated)。

    ⚠ 每次都要重新拷 l0/l1 与那几个空列表：桌面端 `{...DEFAULT_STORE}` 是浅拷，
      源码自己又逐个补了一遍（:40-47）。Python 若不拷，两份 store 会共用同一个 list/dict，
      改 A 里再改 B 就串了——这是同一类坑，用 dict()/list() 显式深一层。
    """
    return {
        "schemaVersion": CURRENT_MEMORY_SCHEMA_VERSION,
        "l0": create_default_l0(),
        "l1": dict(DEFAULT_L1),
        "l2": [],
        "evidence": [],
        "reflectionLogs": [],
        "conflictLogs": [],
        "l2DmaeStates": [],
        "version": 1,
    }


# 来源：memory-store-defaults.ts:52-69
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff]")
_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")


def extract_memory_keywords(input: str, max_n: int = 12) -> List[str]:
    """
    DMAE 命中词抽取（memory-store-defaults.ts:52-69）。

    规则（**不是分词**，就是逐字 + 词频）：
      1) 每个 CJK 字符各算一个 token（中/日/韩：\\u4e00-\\u9fa5、\\u3040-\\u309f、\\u30a0-\\u30ff）；
      2) 再按 `[a-z0-9]+` 抽 ASCII/数字整词（先 lower）；
      3) 按出现次数降序取前 max（默认 12）。

    ⚠ 同一批中文字符的 token 顺序 = 「逐字扫描」在前、「整词」在后，同频时靠**稳定排序**
      保插入序（Python 的 sort 稳定，JS 的 Array.sort 自 ES2019 起也稳定）。改排序方式会变序，
      而序会进曝光位次——别动。
    """
    if not input:
        return []
    tokens: List[str] = []
    for ch in _CJK_CHAR_RE.findall(input):
        tokens.append(ch)
    for word in _ASCII_WORD_RE.findall(input.lower()):
        tokens.append(word)

    freq: Dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    ordered = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, _ in ordered[:max_n]]


def bound_memory_snippet(text: Optional[str], max_length: int) -> Optional[str]:
    """
    截断（memory-store-defaults.ts:71-74）：空值返回 None，超长切前 max_length。

    ⚠ 空串也返回 None（源码 `if (!text) return undefined`，"" 是 falsy）；
      调用方要的是「有没有」，别把 None 和 "" 混着用。
    """
    if not text:
        return None
    return text[:max_length] if len(text) > max_length else text


def memory_file_exists(file_path: Any) -> bool:
    """memory-store-io.ts:15-17。"""
    return os.path.exists(file_path)


def read_memory_file(file_path: Any) -> Dict[str, Any]:
    """
    读 memory.json（memory-store-io.ts:27-29）。

    ⚠ 源码返回 `Partial<MemoryStore>`——**字段可能是残缺的**，直接当完整 store 用会踩
      KeyError。本模块照抄这个语义（原样返回 dict），补齐交给 repair_migrations。
    """
    with open(file_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_memory_file(file_path: Any, store: Dict[str, Any]) -> None:
    """
    写 memory.json：`indent=2, ensure_ascii=False` + 原子替换（memory-store-io.ts:31-35）。

    ⚠ `ensure_ascii=False` 必须开：源码 `JSON.stringify` 不转义非 ASCII，中文要原样落盘
      （否则整个文件都是 \\uXXXX，人肉备份和 Obsidian 同步都不好使）。
    ⚠ 目录不存在就地建（源码 `mkdirSync(recursive: true)`）——首次写盘时 data/ 通常还没建。
    """
    text = json.dumps(store, indent=2, ensure_ascii=False)
    _atomic_write_text(file_path, text)


def backup_memory_file(file_path: Any) -> Optional[str]:
    """
    时间戳备份（memory-store-io.ts:19-25）：`memory.backup.<iso置换成横线>.json`，同目录。

    返回备份文件路径；源文件不存在时返回 None（源码直接 return，什么都不做）。

    ⚠ 桌面端用 `new Date().toISOString()`（UTC，毫秒 3 位），再把 `:` `.` 换成 `-`；
      这里用 time.gmtime 拼同形字符串，不引 datetime。
    ⚠ 同一毫秒内连调两次会**覆盖**前一个备份（源码 copyFileSync 同款行为）——见模块头的偏差说明。
    """
    p = Path(file_path)
    if not p.exists():
        return None
    gm = time.gmtime()
    ms = int((time.time() % 1) * 1000)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", gm) + "-%03dZ" % ms
    backup_path = p.parent / ("memory.backup.%s.json" % stamp)
    # ⚠ 按**字节**原样拷（源码 copyFileSync）：用文本模式读会把 CRLF 归一成 LF，
    #   备份就不再是「原件」了，真出事时对不上。
    with open(p, "rb") as src:
        data = src.read()
    _atomic_write_bytes(backup_path, data)
    return str(backup_path)


def repair_migrations(store: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    schema 迁移 / 补齐（memory-store-migrations.ts:9-53）。

    做四件事：
      1) 顶层字段逐个兜底（l0/l1 与默认值合并、各数组非 list 就置空、version 非数字就 1）；
      2) l2 每条补 syncStatus / evidenceIds / keywords（keywords 非空则**保留**，不重算）；
      3) conflictLogs 每条补 resolverStatus / resolverAttemptCount；
      4) 为没有 l2DmaeState 的 L2 补一条初始状态。

    ⚠ 第 4 步的 `state` 在源码里**硬编码成 "archived"**（:47 三元表达式两臂写的一模一样，
      大概率是笔误）。**照抄，不修**——跨查报告第 3 节第 7 条专门点了这个坑：顺手「修正」成
      按 status 映射，就和桌面端文件对不上了。
    ⚠ l0/l1 是「默认值 + 旧值」的**浅合并**：旧文件里多出来的未知字段会被保留。
      源码 `{...createDefaultL0(), ...store.l0}` 就是这个语义，别改成白名单过滤。
    ⚠ 入参可以是残缺 dict（源码收 Partial<MemoryStore>）；传 None / 非 dict 也能兜住，
      但不存在的字段一律按空处理。
    """
    src: Dict[str, Any] = store if isinstance(store, dict) else {}

    repaired: Dict[str, Any] = {
        "schemaVersion": CURRENT_MEMORY_SCHEMA_VERSION,
        "l0": {**create_default_l0(), **(_as_dict(src.get("l0")))},
        "l1": {**DEFAULT_L1, **(_as_dict(src.get("l1")))},
        "l2": [],
        "evidence": src.get("evidence") if isinstance(src.get("evidence"), list) else [],
        "reflectionLogs": src.get("reflectionLogs") if isinstance(src.get("reflectionLogs"), list) else [],
        "conflictLogs": [],
        "l2DmaeStates": src.get("l2DmaeStates") if isinstance(src.get("l2DmaeStates"), list) else [],
        "version": src.get("version") if isinstance(src.get("version"), int) and not isinstance(src.get("version"), bool) else 1,
    }

    raw_l2 = src.get("l2")
    if isinstance(raw_l2, list):
        for memory in raw_l2:
            if not isinstance(memory, dict):
                continue
            m = dict(memory)
            kws = m.get("keywords")
            if isinstance(kws, list) and len(kws) > 0:
                keywords = kws
            else:
                keywords = extract_memory_keywords("%s %s" % (m.get("content") or "", m.get("triggerText") or ""))
            m["syncStatus"] = _nn(m.get("syncStatus"), "synced" if m.get("ragId") else "pending_sync")
            m["evidenceIds"] = m.get("evidenceIds") if isinstance(m.get("evidenceIds"), list) else []
            m["keywords"] = keywords
            repaired["l2"].append(m)

    raw_logs = src.get("conflictLogs")
    if isinstance(raw_logs, list):
        for log in raw_logs:
            if not isinstance(log, dict):
                continue
            entry = dict(log)
            prio = entry.get("resolverPriority")
            entry["resolverStatus"] = _nn(
                entry.get("resolverStatus"),
                "queued" if (prio and prio != "none") else "not_queued",
            )
            attempt = entry.get("resolverAttemptCount")
            entry["resolverAttemptCount"] = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0
            repaired["conflictLogs"].append(entry)

    # V5 DMAE：缺状态的 L2 补初始化
    state_ids = {s.get("l2Id") for s in repaired["l2DmaeStates"] if isinstance(s, dict)}
    for memory in repaired["l2"]:
        if memory.get("id") not in state_ids:
            repaired["l2DmaeStates"].append(_default_l2_dmae_state(memory.get("id")))

    return repaired


def _as_dict(value: Any) -> Dict[str, Any]:
    """非 dict 的旧值一律当空（源码 `...store.l0` 遇到 undefined 展开为空）。"""
    return value if isinstance(value, dict) else {}


def _default_l2_dmae_state(l2_id: Any) -> Dict[str, Any]:
    """
    L2DmaeState 初始值（memory-store.ts:168-176 / :735-743）。

    ⚠ 默认 state 是 "archived"——即新增 L2 **不进热层**，要等召回（或关键词命中）把它唤醒。
      这不是 bug：DMAE 的热层靠命中抬 activation 进，冷启动就该是 archived。
    """
    return {
        "l2Id": l2_id,
        "activation": 0,
        "intrinsicValue": 0,
        "userSilence": 0,
        "modelSilence": 0,
        "recentUserHits": [],
        "state": "archived",
    }


# ========== P1-2 L2 条目操作（照 memory-store.ts） ==========
def _find_l2(store: Dict[str, Any], l2_id: str) -> Optional[Dict[str, Any]]:
    for mem in store.get("l2") or []:
        if isinstance(mem, dict) and mem.get("id") == l2_id:
            return mem
    return None


def _create_evidence(memory: Dict[str, Any], input: Dict[str, Any]) -> Dict[str, Any]:
    """
    构造 evidence（memory-store.ts:196-206）。

    quoteSnippet 取 `triggerText || content`——**triggerText 优先**（原文比浓缩结论更值钱）。
    ⚠ `conversationId: input.sourceConversationId || undefined`：空串会被吃成「没有」这个键，
      本模块用 None，写盘后是 null，与桌面端 JSON 的 `undefined → 键消失` **有细微差别**
      （见 mark/score 处的同类注释）。未统一，标为已知偏差。
    """
    return {
        "id": "ev_%d_%s" % (_now_ms(), _rand6()),
        "memoryId": memory.get("id"),
        "quoteSnippet": bound_memory_snippet(
            _nn(input.get("triggerText"), None) or input.get("content") or "", QUOTE_SNIPPET_MAX
        ) or "",
        "conversationId": (input.get("sourceConversationId") or None),
        "messageIds": input.get("sourceMessageIds"),
        "createdAt": _now_ms(),
        "sourceStatus": "active",
    }


def _build_l2(input: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    造一条 L2 及其 evidence（memory-store.ts:150-166 的合并版，add_l2 / add_l2_batch 共用）。

    默认值：accessCount=0 / weight=0 / status="active" / evidenceIds=[] /
    keywords=extractMemoryKeywords(content + " " + triggerText)。
    ⚠ 源码里 createdAt 与 lastAccessedAt 是**两次** Date.now() 调用（理论上能差 1ms）；
      这里只取一次 now 给两者，避免「后访问时间早于创建时间」这种脏数据。无实质差异。
    """
    now = _now_ms()
    memory = dict(input)
    memory["id"] = "l2_%d_%s" % (now, _rand6())
    memory["createdAt"] = now
    memory["lastAccessedAt"] = now
    memory["accessCount"] = 0
    memory["weight"] = 0
    memory["status"] = "active"
    memory["syncStatus"] = _nn(input.get("syncStatus"), "synced" if input.get("ragId") else "pending_sync")
    memory["evidenceIds"] = input.get("evidenceIds") if isinstance(input.get("evidenceIds"), list) else []
    memory["keywords"] = extract_memory_keywords(
        "%s %s" % (input.get("content") or "", input.get("triggerText") or "")
    )
    evidence = _create_evidence(memory, input)
    memory["evidenceIds"] = list(memory.get("evidenceIds") or []) + [evidence["id"]]
    return memory, evidence


def add_l2(store: Dict[str, Any], input: Dict[str, Any]) -> Dict[str, Any]:
    """
    新增一条 L2，并同步追加 evidence 与 l2DmaeState（memory-store.ts:148-194 addL2Memory）。

    ⚠ 参数名 `input` 沿用源码（buildin 遮蔽是有意的，作用域只在函数内）。
    ⚠ 三样东西**必须同时写**：l2 / evidence / l2DmaeStates。少一个，DMAE 热层或召回取证据
      就找不到这条记忆——源码在这里是硬绑的，移植时拆开写很容易漏。
    返回新建的记忆 dict（就地改动 store）。
    """
    memory, evidence = _build_l2(input)
    store.setdefault("l2", []).append(memory)
    if not isinstance(store.get("evidence"), list):
        store["evidence"] = []
    store["evidence"].append(evidence)
    if not isinstance(store.get("l2DmaeStates"), list):
        store["l2DmaeStates"] = []
    store["l2DmaeStates"].append(_default_l2_dmae_state(memory["id"]))
    return memory


def add_l2_batch(store: Dict[str, Any], inputs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量新增（压缩总结用，memory-store.ts:654-706）。与 add_l2 同款，只是循环。"""
    if not isinstance(store.get("l2DmaeStates"), list):
        store["l2DmaeStates"] = []
    results: List[Dict[str, Any]] = []
    for item in inputs:
        memory, evidence = _build_l2(item)
        store.setdefault("l2", []).append(memory)
        if not isinstance(store.get("evidence"), list):
            store["evidence"] = []
        store["evidence"].append(evidence)
        store["l2DmaeStates"].append(_default_l2_dmae_state(memory["id"]))
        results.append(memory)
    return results


def update_l2_recall_stats(store: Dict[str, Any], l2_id: str, delta: int = 1) -> Optional[Dict[str, Any]]:
    """
    召回命中后加权重（memory-store.ts:212-247）。

    weight 夹取 [0,100]；status 判定：
      - `isPinned` 或**原本就是 active** → 保持 active
      - 否则 `weight >= 30` → active
      - 否则 → aging

    ⚠ 只处理 active / aging 两种状态：archived / superseded / merged 一律**直接跳过**
      （源码 :216-226 早退），连 accessCount 都不加。想让归档条目复活得走召回唤醒那条链。
    ⚠ `previousStatus === "active"` 这一支：只有「本来就是 active」才无条件留 active，
      不是「加完够 30 就 active」——aging 条目加到 25 仍回 aging。
    """
    mem = _find_l2(store, l2_id)
    if mem is None:
        return None
    if mem.get("status") not in ("active", "aging"):
        return mem  # not_recallable：源码只写 trace，不改数据
    previous_status = mem.get("status")
    mem["weight"] = max(0, min(100, (mem.get("weight") or 0) + delta))
    mem["lastAccessedAt"] = _now_ms()
    mem["accessCount"] = (mem.get("accessCount") or 0) + 1
    if mem.get("isPinned") or previous_status == "active":
        mem["status"] = "active"
    elif mem.get("weight") >= 30:
        mem["status"] = "active"
    else:
        mem["status"] = "aging"
    return mem


def update_l2_weight(store: Dict[str, Any], l2_id: str, delta: int) -> Optional[Dict[str, Any]]:
    """别名（memory-store.ts:289-291，源码就是直接转发）。"""
    return update_l2_recall_stats(store, l2_id, delta)


def pin_l2(store: Dict[str, Any], l2_id: str, pinned: bool) -> Optional[Dict[str, Any]]:
    """
    锁定 / 解锁（memory-store.ts:249-274）。

    pinned → 强制 active；unpin 时按 weight 重判：
      `> 60` → active（:256 这一支和下面的 `>= 30` 结果相同，源码**两支并存**，照抄）
      `>= 30` → active；`>= 10` → aging；否则 → archived

    ⚠ `> 60` 那支是冗余的（结果与 `>= 30` 一样）。**不合并**：源码如此，删掉它就少了一处
      与桌面端逐行对账的锚点；日后若要给高权重 pin 记录日志，这一支是预留位。
    """
    mem = _find_l2(store, l2_id)
    if mem is None:
        return None
    mem["isPinned"] = bool(pinned)
    if pinned:
        mem["status"] = "active"
    elif (mem.get("weight") or 0) > 60:
        mem["status"] = "active"
    elif (mem.get("weight") or 0) >= 30:
        mem["status"] = "active"
    elif (mem.get("weight") or 0) >= 10:
        mem["status"] = "aging"
    else:
        mem["status"] = "archived"
    return mem


def delete_l2(store: Dict[str, Any], l2_id: str) -> bool:
    """
    删除一条 L2 及其 evidence（memory-store.ts:276-287）。

    ⚠ 源码**不清理 l2DmaeStates**（只 filter l2 与 evidence），残留的孤儿状态行会留在文件里。
      照抄，不顺手清理——清了就和桌面端文件不一致；孤儿行由 repair/加载侧容忍。
      返回是否真的删掉了 L2 本身。
    """
    mem = _find_l2(store, l2_id)
    store["l2"] = [m for m in (store.get("l2") or []) if not (isinstance(m, dict) and m.get("id") == l2_id)]
    ev = store.get("evidence")
    store["evidence"] = [
        e for e in (ev if isinstance(ev, list) else [])
        if not (isinstance(e, dict) and e.get("memoryId") == l2_id)
    ]
    return mem is not None


def mark_l2_sync_status(
    store: Dict[str, Any], l2_id: str, sync_status: str, rag_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    标记同步状态（memory-store.ts:293-310）。`ragId` 非空才覆盖（源码 `if (ragId)`）。
    syncStatus ∈ pending_sync / synced / sync_failed（此处不校验，照抄源码的宽松）。
    """
    mem = _find_l2(store, l2_id)
    if mem is None:
        return None
    mem["syncStatus"] = sync_status
    if rag_id:
        mem["ragId"] = rag_id
    return mem


def mark_l2_conflict(store: Dict[str, Any], l2_id: str, conflict_rag_id: str) -> Optional[Dict[str, Any]]:
    """
    记一条冲突关系（memory-store.ts:312-334）。

    ⚠ 传入的 `conflict_rag_id` 按源码语义是 **ragId**（不是 L2 主键）——别把 l2_xxx 塞进来。
    ⚠ 已记过同一个 id 时返回 **None**（源码 :317 早退），调用方要能区分「标记成功」与
      「本来就记过」；这不是错误。
    ⚠ 副作用：active 且未 pinned 的条目会被**降级成 aging**（把有疑问的记忆先降温，
      等 resolver 判完再定），pinned 的不动。
    """
    mem = _find_l2(store, l2_id)
    if mem is None:
        return None
    conflicts = mem.get("conflictWith") or []
    if conflict_rag_id in conflicts:
        return None
    mem["conflictWith"] = list(conflicts) + [conflict_rag_id]
    if not mem.get("isPinned") and mem.get("status") == "active":
        mem["status"] = "aging"
    return mem


def update_l2_content(store: Dict[str, Any], l2_id: str, content: str) -> Optional[Dict[str, Any]]:
    """
    只改正文（Obsidian 回流用，memory-store.ts:343-361）。

    ⚠ 正文变了要**重算 keywords**（DMAE 命中检测靠它），并置 `pending_sync`：
      向量重建完成前该记忆不可被语义召回，防止检索命中旧向量里的旧文本。
    ⚠ 正文没变就**原样返回、不动 syncStatus**（源码 :347 早退）——别顺手刷成 pending_sync，
      否则每次回流都会把已同步的条目打回未同步。
    """
    mem = _find_l2(store, l2_id)
    if mem is None:
        return None
    if mem.get("content") == content:
        return mem
    mem["content"] = content
    mem["keywords"] = extract_memory_keywords("%s %s" % (content, mem.get("triggerText") or ""))
    mem["syncStatus"] = "pending_sync"
    return mem


def get_all_l2(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回 l2 列表本身（memory-store.ts:363-366，源码返回的是同一引用）。"""
    return store.get("l2") if isinstance(store.get("l2"), list) else []


def get_evidence_by_memory_id(store: Dict[str, Any], memory_id: str) -> List[Dict[str, Any]]:
    """memory-store.ts:368-371。"""
    ev = store.get("evidence")
    return [e for e in (ev if isinstance(ev, list) else []) if isinstance(e, dict) and e.get("memoryId") == memory_id]


def update_l2_status(store: Dict[str, Any], ids: Sequence[str], status: str) -> int:
    """批量改 status（memory-store.ts:604-618）。返回改了几条。"""
    changed = 0
    idset = set(ids)
    for mem in store.get("l2") or []:
        if isinstance(mem, dict) and mem.get("id") in idset:
            mem["status"] = status
            changed += 1
    return changed


def archive_l2_batch(store: Dict[str, Any], ids: Sequence[str]) -> int:
    """memory-store.ts:620-622。"""
    return update_l2_status(store, ids, "archived")


def upsert_l0_field(store: Dict[str, Any], field: str, value: Any) -> Dict[str, Any]:
    """
    写一个 L0 字段并刷新 updatedAt（memory-store.ts:106-116）。

    ⚠ `updatedAt` 由本函数统一接管（源码里它被 L0WritableField 排除在外），调用方不要自己传。
    """
    if not isinstance(store.get("l0"), dict):
        store["l0"] = create_default_l0()
    store["l0"][field] = value
    store["l0"]["updatedAt"] = _now_ms()
    return store["l0"]


def update_l0(store: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    批量写 L0（memory-store.ts:118-123）。

    ⚠ 源码逐字段调用 upsert，等于**每个字段各刷一次 updatedAt**；这里合并成一次刷。
      结果等价（时间戳只可能差几毫秒），但少 N 次写盘时机——照抄语义、不照抄调用次数。
    """
    for field, value in (patch or {}).items():
        if field == "updatedAt":
            continue
        upsert_l0_field(store, field, value)
    return store.get("l0")


def replace_l1_field(store: Dict[str, Any], field: str, value: Any) -> Dict[str, Any]:
    """写一个 L1 字段（memory-store.ts:130-140）。L1 没有 updatedAt，不刷新。"""
    if not isinstance(store.get("l1"), dict):
        store["l1"] = dict(DEFAULT_L1)
    store["l1"][field] = value
    return store["l1"]


def update_l1(store: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """memory-store.ts:142-146。"""
    for field, value in (patch or {}).items():
        replace_l1_field(store, field, value)
    return store.get("l1")


def append_reflection_log(store: Dict[str, Any], log: Dict[str, Any]) -> Dict[str, Any]:
    """
    追加反思日志（memory-store.ts:373-393）。上限 50，超出 `slice(-50)` 丢最旧。

    ⚠ type 只有 compression / l0_update / l1_update 三种；**没有 l2_update**
      （L2 写入不写反思日志，见 l2-prompts.md §6）。本函数不校验，但别发明第四种。
    """
    entry = dict(log)
    entry["id"] = "ref_%d_%s" % (_now_ms(), _rand6())
    entry["createdAt"] = _now_ms()
    if not isinstance(store.get("reflectionLogs"), list):
        store["reflectionLogs"] = []
    store["reflectionLogs"].append(entry)
    if len(store["reflectionLogs"]) > REFLECTION_LOG_MAX:
        store["reflectionLogs"] = store["reflectionLogs"][-REFLECTION_LOG_MAX:]
    return entry


def get_reflection_logs(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """memory-store.ts:399-402。"""
    logs = store.get("reflectionLogs")
    return logs if isinstance(logs, list) else []


def append_conflict_log(store: Dict[str, Any], log: Dict[str, Any]) -> Dict[str, Any]:
    """追加冲突日志（memory-store.ts:404-431）。上限 100，超出 slice(-100)。"""
    entry = dict(log)
    entry["id"] = "conf_%d_%s" % (_now_ms(), _rand6())
    entry["createdAt"] = _now_ms()
    if not isinstance(store.get("conflictLogs"), list):
        store["conflictLogs"] = []
    store["conflictLogs"].append(entry)
    if len(store["conflictLogs"]) > CONFLICT_LOG_MAX:
        store["conflictLogs"] = store["conflictLogs"][-CONFLICT_LOG_MAX:]
    return entry


def get_conflict_logs(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """memory-store.ts:433-436。"""
    logs = store.get("conflictLogs")
    return logs if isinstance(logs, list) else []


def score_conflict_log(store: Dict[str, Any], log_id: str, score: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    把打分结果写回冲突日志，并决定要不要进 resolver 队列（memory-store.ts:438-492）。

    `score` 含 conflictScore / resolverPriority / scoringSignals 三键。
    入队条件：`status == "candidate"` 且 `resolverPriority != "none"`。

    ⚠ else 分支里源码写的是 `log.resolverQueuedAt = undefined`——JSON.stringify 后**这个键会消失**。
      Python 里必须 `pop` 掉而不是置 None，否则文件里多一个 `"resolverQueuedAt": null`，
      和桌面端逐字节比对就会挂。这里已按 pop 处理。
    ⚠ `resolverAttemptCount: log.resolverAttemptCount ?? 0` 在**两个分支都没被清零**：
      已有计数会保留。别顺手改成 =0。
    """
    log = None
    for entry in store.get("conflictLogs") or []:
        if isinstance(entry, dict) and entry.get("id") == log_id:
            log = entry
            break
    if log is None:
        return None

    log["conflictScore"] = score.get("conflictScore")
    log["resolverPriority"] = score.get("resolverPriority")
    log["scoringSignals"] = score.get("scoringSignals")

    should_queue = log.get("status") == "candidate" and score.get("resolverPriority") != "none"
    if should_queue:
        log["resolverStatus"] = "queued"
        log["resolverQueuedAt"] = _nn(log.get("resolverQueuedAt"), _now_ms())
        log["resolverAttemptCount"] = _nn(log.get("resolverAttemptCount"), 0)
    else:
        log["resolverStatus"] = "not_queued"
        log.pop("resolverQueuedAt", None)
        log["resolverAttemptCount"] = _nn(log.get("resolverAttemptCount"), 0)
    return log


def get_resolver_queue(store: Dict[str, Any], limit: int = 20) -> List[Dict[str, Any]]:
    """
    取待处理的 resolver 队列（memory-store.ts:494-509）：候选 + 已入队 + 有非 none 优先级，
    按优先级降序、入队时间升序，截 limit。

    ⚠ 筛选条件照抄源码，包括一个反直觉的细节：源码判的是 `resolverPriority !== undefined`。
      JSON 里的 `null` **不等于** undefined，所以 `"resolverPriority": null` 会被**算作已设置**
      并进入队列。本模块按「键存在且不等于 none」实现，忠实于该行为，不当 bug 修。
    ⚠ 排序键遇到未知优先级时，源码会算出 NaN（顺序由引擎实现决定）；这里用 `.get(k, 0)` 兜 0。
    """
    queue = []
    for log in store.get("conflictLogs") or []:
        if not isinstance(log, dict):
            continue
        if log.get("status") != "candidate" or log.get("resolverStatus") != "queued":
            continue
        if "resolverPriority" not in log:
            continue
        if log.get("resolverPriority") == "none":
            continue
        queue.append(log)
    queue.sort(key=lambda log: (
        -RESOLVER_PRIORITY_RANK.get(log.get("resolverPriority") or "none", 0),
        _nn(log.get("resolverQueuedAt"), log.get("createdAt") or 0),
    ))
    return queue[:limit]


def apply_resolver_resolution(
    store: Dict[str, Any], conflict_log_id: str, resolution: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    应用 resolver 结论（memory-store.ts:511-601）。

    做四件事：必要时新建「和解记忆」；改旧/新记忆的 status（并在 superseded/merged 时回填
    supersededBy / mergedInto）；把结论写回日志；按 resolutionType 定日志终态
    （unrelated → dismissed；要问用户 → clarification_needed；否则 resolved）。

    ⚠ 新版「和解记忆」的 `triggerText` 用的是 `resolution.reason`（不是用户原话）——
      源码如此，别改成从旧记忆继承。
    ⚠ 找不到 source/target 记忆时返回 None 且**什么都不改**（源码 :517 早退）：resolver 的
      结论可能比记忆过期，这种竞态靠这条早退挡住。
    """
    log = None
    for entry in store.get("conflictLogs") or []:
        if isinstance(entry, dict) and entry.get("id") == conflict_log_id:
            log = entry
            break
    if log is None:
        return None
    new_memory = _find_l2(store, log.get("sourceL2Id"))
    old_memory = _find_l2(store, log.get("targetL2Id"))
    if new_memory is None or old_memory is None:
        return None

    actions = resolution.get("actions") or {}
    resolution_memory_id: Optional[str] = None
    resolved_summary = (resolution.get("resolvedSummary") or "").strip()
    if actions.get("createResolvedMemory") and resolved_summary:
        reason = resolution.get("reason")
        resolved, evidence = _build_l2({
            "content": resolved_summary,
            "triggerText": reason,
            "sourceConversationId": new_memory.get("sourceConversationId") or old_memory.get("sourceConversationId"),
            "sourceMessageIds": list(old_memory.get("sourceMessageIds") or []) + list(new_memory.get("sourceMessageIds") or []),
            "isPinned": False,
            "syncStatus": "pending_sync",
            "evidenceIds": list(old_memory.get("evidenceIds") or []) + list(new_memory.get("evidenceIds") or []),
        })
        store.setdefault("l2", []).append(resolved)
        if isinstance(store.get("evidence"), list):
            store["evidence"].append(evidence)
        resolution_memory_id = resolved["id"]

    old_status = actions.get("oldMemoryStatus")
    if old_status:
        old_memory["status"] = old_status
        if old_status == "superseded" and resolution_memory_id:
            old_memory["supersededBy"] = resolution_memory_id
        if old_status == "merged" and resolution_memory_id:
            old_memory["mergedInto"] = resolution_memory_id
    new_status = actions.get("newMemoryStatus")
    if new_status:
        new_memory["status"] = new_status
        if new_status == "superseded" and resolution_memory_id:
            new_memory["supersededBy"] = resolution_memory_id
        if new_status == "merged" and resolution_memory_id:
            new_memory["mergedInto"] = resolution_memory_id

    log["resolverStatus"] = "resolved"
    log["resolverFinishedAt"] = _now_ms()
    log["resolutionType"] = resolution.get("resolutionType")
    if resolution_memory_id:
        log["resolutionMemoryId"] = resolution_memory_id
    log["resolutionReason"] = resolution.get("reason")
    log["resolutionConfidence"] = resolution.get("confidence")
    log["shouldAskUser"] = actions.get("shouldAskUser") is True
    log["clarificationNeeded"] = actions.get("clarificationNeeded") is True

    if resolution.get("resolutionType") == "unrelated":
        log["status"] = "dismissed"
    elif actions.get("clarificationNeeded") or actions.get("shouldAskUser"):
        log["status"] = "clarification_needed"
    else:
        log["status"] = "resolved"
    return log


# ── V5 L2 DMAE 状态读写（memory-store.ts:708-747）──
def get_l2_dmae_state(store: Dict[str, Any], l2_id: str) -> Optional[Dict[str, Any]]:
    """memory-store.ts:709-712。"""
    for st in store.get("l2DmaeStates") or []:
        if isinstance(st, dict) and st.get("l2Id") == l2_id:
            return st
    return None


def get_all_l2_dmae_states(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """memory-store.ts:714-717。"""
    states = store.get("l2DmaeStates")
    return states if isinstance(states, list) else []


def update_l2_dmae_state(store: Dict[str, Any], l2_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    局部更新状态（memory-store.ts:719-728）。`l2Id` 永远以入参为准（源码 `{...state, ...patch, l2Id}`）。

    ⚠ 状态行**不存在**时返回 None 且不新建（源码 :723 早退）；要建就走
      init_l2_dmae_state_if_missing。两者别混。
    """
    if not isinstance(store.get("l2DmaeStates"), list):
        store["l2DmaeStates"] = []
    for idx, st in enumerate(store["l2DmaeStates"]):
        if isinstance(st, dict) and st.get("l2Id") == l2_id:
            merged = {**st, **patch, "l2Id": l2_id}
            store["l2DmaeStates"][idx] = merged
            return merged
    return None


def init_l2_dmae_state_if_missing(store: Dict[str, Any], l2_id: str) -> Dict[str, Any]:
    """memory-store.ts:730-747：有就返回，没有就补一条 archived 初始状态。"""
    existing = get_l2_dmae_state(store, l2_id)
    if existing is not None:
        return existing
    if not isinstance(store.get("l2DmaeStates"), list):
        store["l2DmaeStates"] = []
    created = _default_l2_dmae_state(l2_id)
    store["l2DmaeStates"].append(created)
    return created


# ========== P1-3 权重三态与衰减 ==========
def decay_l2_weights(store: Dict[str, Any], delta: int = 1) -> int:
    """
    全局衰减一轮（memory-store.ts:624-652），返回被改动的条数。

    跳过三类：`isPinned` / 已 `archived` / `weight <= 0`。
    其余：`weight = max(0, weight - delta)`，再按 `>=30 active` / `>=10 aging` / 否则 `archived` 重判。

    ⚠ 跳过 `weight <= 0` 是为了让「已经归零的」不再反复计数（源码注释：防止 changed 虚高）。
    ⚠ 判定用 `>=`：weight 正好 10 是 aging、正好 30 是 active。边界值必须按闭区间理解。
    ⚠ `archived` 一旦落下就**不再被衰减翻转**（跳过了），只能靠召回唤醒——即衰减是单向的。
    """
    changed = 0
    for mem in store.get("l2") or []:
        if not isinstance(mem, dict):
            continue
        if mem.get("isPinned") or mem.get("status") == "archived" or (mem.get("weight") or 0) <= 0:
            continue
        mem["weight"] = max(0, (mem.get("weight") or 0) - delta)
        if mem["weight"] >= 30:
            mem["status"] = "active"
        elif mem["weight"] >= 10:
            mem["status"] = "aging"
        else:
            mem["status"] = "archived"
        changed += 1
    return changed


# ========== 其他照抄常量与纯函数（memory-manager.ts） ==========
def has_correction_intent(text: str) -> bool:
    """
    用户是否在纠正记忆（memory-manager.ts:30-32）。

    ⚠ 「记错了」是「你记错了」的**子串**，所以关键词表里有冗余项；列表本身逐字照抄，
      不做去冗余（顺序即匹配优先级，改了就与源码不一致）。
    """
    if not text:
        return False
    return any(kw in text for kw in CORRECTION_INTENT_KEYWORDS)


def guess_l1_field(content: str) -> str:
    """
    L1 字段兜底推断（memory-manager.ts:19-23）：目标/想要/计划/打算 → recentGoals；
    项目/在做/开发/写 → currentProject；否则 recentPreferences。

    ⚠ 顺序即优先级：先判 goals 再判 project。一句话同时含「想」与「项目」时归 recentGoals。
    """
    text = content or ""
    if _RE_L1_GOALS.search(text):
        return "recentGoals"
    if _RE_L1_PROJECT.search(text):
        return "currentProject"
    return "recentPreferences"


def resolve_l1_field(field: Optional[str], content: str) -> str:
    """白名单内用原值，否则兜底推断（memory-manager.ts:25-28）。"""
    if field in L1_FIELD_WHITELIST:
        return field  # type: ignore[return-value]
    return guess_l1_field(content)


# ========== P1-4 词法冲突（memory-conflict.ts） ==========
# 语义矛盾关键词对：前者正面/肯定，后者负面/否定。**9 条，逐字照抄，勿扩写**
# （memory-conflict.ts:8-18；跨查报告明确写了「不是开放式规则」）。
CONTRADICTION_PAIRS: List[Tuple[str, List[str]]] = [
    ("喜欢", ["不喜欢", "讨厌", "反感", "厌恶", "不再喜欢"]),
    ("爱", ["不爱", "讨厌", "恨"]),
    ("想", ["不想", "别想", "不愿"]),
    ("要", ["不要", "别要"]),
    ("是", ["不是", "并非"]),
    ("可以", ["不可以", "不行", "不能"]),
    ("会", ["不会"]),
    ("有", ["没有", "没了", "无"]),
    ("忙", ["不忙", "闲"]),
]

# 来源：memory-conflict.ts:20-48。⚠ 源码字面量里「不是 / 不会 / 没有」各出现了两次，
# 集合去重后是 **24 个**（不是 27）。本模块用 frozenset，与源码 Set 语义一致。
STOP_TERMS: frozenset = frozenset({
    "用户", "一个", "一种", "这个", "那个", "自己", "因为", "所以", "但是",
    "没有", "不是", "不会", "不能", "不喜", "喜欢", "讨厌", "反感", "厌恶",
    "不爱", "不想", "不要", "不行", "没了", "不忙",
})

# ⚠ 注意 CJK 区间是 \u4e00-\u9fff（到 9fff），**不是** memory-store-defaults 里的 \u9fa5。
#   两个文件的口径本来就不同，照各自源码写，别「统一」。
_TOPIC_TERM_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{3,}")
_CJK_ONLY_RE = re.compile(r"^[\u4e00-\u9fff]+$")


def extract_topic_terms(text: str) -> Set[str]:
    """
    抽主题词（memory-conflict.ts:54-69）。

    规则：CJK 连续串 ≥2 字、ASCII/数字串 ≥3 字；命中 STOP_TERMS 的整串丢弃；
    CJK 串长度 > 2 时额外补**所有 bigram**（滑动 2 字窗），bigram 命中 STOP_TERMS 也丢。

    ⚠ 整串与 bigram 是**并存**的：`"喜欢香菇"` 会同时产出整串 "喜欢香菇"（不在停用表里，留着）
      和 bigram "欢香"/"香菇"（"喜欢" 被停用表吃掉）。别以为是「只要 bigram」。
    ⚠ 传 None 时源码会抛（`text.match` 无此方法）；这里按空串处理（[降级] 容错，不改判定结果）。
    """
    terms: Set[str] = set()
    if not text:
        return terms
    for raw in _TOPIC_TERM_RE.findall(text):
        term = raw.lower()
        if term in STOP_TERMS:
            continue
        terms.add(term)
        if _CJK_ONLY_RE.fullmatch(term) and len(term) > 2:
            for i in range(len(term) - 1):
                gram = term[i:i + 2]
                if gram not in STOP_TERMS:
                    terms.add(gram)
    return terms


def has_shared_topic(text_a: str, text_b: str) -> bool:
    """两段文本是否有共同主题词（memory-conflict.ts:71-78）。"""
    a_terms = extract_topic_terms(text_a)
    b_terms = extract_topic_terms(text_b)
    return any(t in b_terms for t in a_terms)


def find_possible_conflict_candidate(new_content: str, existing_content: str) -> Dict[str, Any]:
    """
    词法级「可能冲突」初筛（memory-conflict.ts:80-102）。

    返回 dict：`{"isCandidate": bool, "reason"?: str, "confidence": float}`，命中时置信度恒为
    **0.35**（源码硬编码，不是算出来的），reason 里带命中的那个肯定词。

    ⚠ 返回值恒为 dict，**没有 None 分支**——源码两条 return 各自返回对象（:82 / :101）。
      任务书写的是 `dict|None`，与源码不符；此处按源码返回 dict，由调用方读 isCandidate。
    ⚠ 先判主题共享再判正反义：不共享主题一律 False（宁漏不误的取向）。
    ⚠ **已知误报源（源码行为，未修）**：判定用 `substring`，而否定词大多**包含**肯定词
      （「不喜欢」含「喜欢」、「不爱」含「爱」）。于是「我不喜欢吃辣」与「我也不喜欢吃辣」
      这种**双否定同义句**会互相命中，被判为候选冲突。这是 substring 口径的必然代价，
      上了 resolver 由模型兜底；移植时**不要**顺手加「否定词出现即视为不含肯定词」的修补，
      那会同时改掉 9 条正反义对的命中面，把假阴性引进来。
    """
    if not has_shared_topic(new_content, existing_content):
        return {"isCandidate": False, "confidence": 0}
    a = (new_content or "").lower()
    b = (existing_content or "").lower()
    for positive, negatives in CONTRADICTION_PAIRS:
        a_has_pos = positive in a
        b_has_pos = positive in b
        a_has_neg = any(n in a for n in negatives)
        b_has_neg = any(n in b for n in negatives)
        if (a_has_pos and b_has_neg) or (b_has_pos and a_has_neg):
            return {
                "isCandidate": True,
                "reason": "possible shared-topic lexical contradiction: %s" % positive,
                "confidence": 0.35,
            }
    return {"isCandidate": False, "confidence": 0}


# ========== P1-5 冲突打分（memory-conflict-score.ts） ==========
def clamp_score(score: float) -> int:
    """
    `Math.max(0, Math.min(100, Math.round(score)))`（memory-conflict-score.ts:24-26）。

    ⚠ 用 _js_round 不用 round()：Python 是银行家舍入，74.5 会变 74 → 优先级从 high 掉到
      normal。这一位之差跨过 75 门槛，是真实可复现的分歧。
    """
    return max(0, min(100, _js_round(score)))


def priority_for(score: float) -> str:
    """四档：>=75 high / >=55 normal / >=35 idle / 否则 none（memory-conflict-score.ts:28-33）。"""
    if score >= 75:
        return "high"
    if score >= 55:
        return "normal"
    if score >= 35:
        return "idle"
    return "none"


def rag_points(score: Optional[float]) -> int:
    """
    RAG 相似度得分（memory-conflict-score.ts:35-40）：None → 0；>=0.75 → 25；>=0.45 → 18；否则 10。

    ⚠ 只要传了数字就给分：0.0 甚至负数也拿 10 分（源码没有下界判断）。别改成「低分给 0」。
    """
    if score is None:
        return 0
    if score >= 0.75:
        return 25
    if score >= 0.45:
        return 18
    return 10


def evidence_points(evidence: str) -> int:
    """both → 15；one_side → 8；其余（含 none）→ 0（memory-conflict-score.ts:42-46）。"""
    if evidence == "both":
        return 15
    if evidence == "one_side":
        return 8
    return 0


def impact_points(scope: Optional[str]) -> int:
    """high → 10；medium → 6；low → 3；其余/None → 0（memory-conflict-score.ts:48-53）。"""
    if scope == "high":
        return 10
    if scope == "medium":
        return 6
    if scope == "low":
        return 3
    return 0


def score_memory_conflict(input: Dict[str, Any]) -> Dict[str, Any]:
    """
    冲突打分（memory-conflict-score.ts:55-104）。

    入参（camelCase，字段名照抄源码 ConflictScoreInput）：
      candidateSource: "local" | "rag" | "recent_injection"（必填）
      evidence: "none" | "one_side" | "both"（必填）
      activeTarget: bool（必填）
      ragScore / correctionIntent / recentInjection / localContradiction /
      impactScope / recentlyResolvedSamePair（可选）

    加分：correctionIntent +20；ragCandidate +rag_points；recentInjection +20；
          evidence_points；localContradiction +10；impact_points。
    扣分：非 activeTarget -25；evidence == "none" -20；recentlyResolvedSamePair -25。
    最后：clamp → priority_for → **降级闸门**：`candidateSource == "local"` 或
          非 activeTarget 或 evidence == "none" 时，优先级一律压成 "none"。

    ⚠ 降级闸门只改 resolverPriority，**不改 conflictScore**——所以会出现「分数 80 但优先级
      none」。这是源码的刻意设计（本地词法命中与无证据的都不值得调模型），别当 bug 抹平。
    ⚠ `ragCandidate` 的判定是「来源是 rag **或** 传了 ragScore」；`recentInjection` 同理。
      因此 candidateSource="local" 但带了 ragScore 时，分数里含 RAG 分，优先级仍被压成 none。
    ⚠ **impactScope 有个口径分裂，照抄别抹平**：计分走 `impactPoints(input.impactScope)`
      （源码 :67，**没有 `?? "low"` 兜底**），而 signals 里写的是 `input.impactScope ?? "low"`
      （:95）。所以不传 impactScope 时会出现「signals 显示 low，但一分没加（按 0 分算）」。
      想拿满 low 的 3 分必须**显式**传 "low"。
    """
    penalties: List[str] = []
    score = 0

    corpus_source = input.get("candidateSource")
    rag_candidate = corpus_source == "rag" or input.get("ragScore") is not None
    recent_injection = corpus_source == "recent_injection" or input.get("recentInjection") is True

    if input.get("correctionIntent"):
        score += 20
    if rag_candidate:
        score += rag_points(input.get("ragScore"))
    if recent_injection:
        score += 20
    score += evidence_points(input.get("evidence"))
    if input.get("localContradiction"):
        score += 10
    score += impact_points(input.get("impactScope"))

    active_target = input.get("activeTarget")
    evidence = input.get("evidence")
    if not active_target:
        score -= 25
        penalties.append("archived_only_target")
    if evidence == "none":
        score -= 20
        penalties.append("missing_evidence")
    if input.get("recentlyResolvedSamePair"):
        score -= 25
        penalties.append("recently_resolved_same_pair")

    conflict_score = clamp_score(score)
    resolver_priority = priority_for(conflict_score)
    if corpus_source == "local" or not active_target or evidence == "none":
        resolver_priority = "none"

    scoring_signals = {
        "correctionIntent": input.get("correctionIntent") is True,
        "ragCandidate": rag_candidate,
        "recentInjection": recent_injection,
        "evidenceAvailable": evidence != "none",
        "localContradiction": input.get("localContradiction") is True,
        "impactScope": _nn(input.get("impactScope"), "low"),
        "penalties": penalties,
    }
    return {
        "conflictScore": conflict_score,
        "resolverPriority": resolver_priority,
        "scoringSignals": scoring_signals,
    }


# ========== P1-6 关系画像（relationship-log.ts） ==========
# 来源：relationship-log.ts:42-43
MAX_ENTRIES = 500
MAX_DAILY_SUMMARIES = 90

# 来源：relationship-log.ts:62-71。顺序即优先级（先判疲惫，再判边界，再焦虑/低落/开心）。
_MOOD_RULES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"累|疲惫|困|没精神|撑不住|倦"), "疲惫"),
    # ⚠ 边界表达刻意只认「明确指向交互方式」的写法，避免「不想/别/先不」这类常用词误伤
    #   （源码 :64-65 有注释：'我今天不想xx'、'想吃点别的' 不该触发低打扰偏好）。
    (re.compile(r"影响观感|太影响|不要.{0,4}(确认|弹|问|卡片)|别.{0,4}(问|弹|确认|卡片)|少问|别问了"), "明确边界"),
    (re.compile(r"焦虑|压力|烦|崩|紧张|担心|慌"), "焦虑"),
    (re.compile(r"难过|伤心|委屈|失落|想哭"), "低落"),
    (re.compile(r"开心|高兴|舒服|喜欢|好耶|太好了"), "开心"),
)
MOOD_UNKNOWN = "未知"


def local_date(ts: float) -> str:
    """
    毫秒时间戳 → **本地**日期 `YYYY-MM-DD`（relationship-log.ts:49-55）。

    ⚠ 源码用 getFullYear/getMonth/getDate（本地时区）；这里用 time.localtime 对齐。
      不要换成 UTC（换一天就差一天，日记摘要会错位）。
    """
    y, mo, d = time.localtime(ts / 1000.0)[:3]
    return "%04d-%02d-%02d" % (y, mo, d)


def compact(text: str, max_n: int = 120) -> str:
    """
    压空白 + 截断（relationship-log.ts:57-60）：连续空白折成一个空格、首尾去空白，
    超长则切前 max_n 再补 `...`（补的省略号**不计入** max_n）。
    """
    s = re.sub(r"\s+", " ", text or "").strip()
    return s[:max_n] + "..." if len(s) > max_n else s


def detect_user_mood(text: str) -> str:
    """情绪正则（relationship-log.ts:62-71）。五条规则按序命中，全不中返回 "未知"。"""
    t = text or ""
    for pattern, mood in _MOOD_RULES:
        if pattern.search(t):
            return mood
    return MOOD_UNKNOWN


def derive_signal(user_text: str, user_mood: str) -> Dict[str, Any]:
    """
    由情绪推固定文案（relationship-log.ts:73-118）。六条分支，文案逐字照抄。

    返回 `{"relationshipSignal": str, "nextCareCue": str, "importantMoment"?: str}`。

    ⚠ `importantMoment` **只有「明确边界」这一支有**（源码其它分支的对象字面量里没有这个键）。
      别为了「结构统一」给所有分支都塞一个空串——那会改变 build_context 里
      `find(e => e.importantMoment)` 的判定（空串是 falsy 还好，但键存在会让下游 dump 出多余字段）。
    ⚠ 默认分支的 nextCareCue 内嵌 `compact(user_text, 40)` 的**用户原话片段**。
    """
    if user_mood == "明确边界":
        return {
            "relationshipSignal": "用户表达了低打扰偏好或体验边界，需要优先尊重，不要把关心做成打断。",
            "importantMoment": "用户明确表示不喜欢影响观感的确认卡片或过度询问。",
            "nextCareCue": "不要弹确认或反复追问；先按用户偏好安静执行，必要时用一句话确认。",
        }
    if user_mood == "疲惫":
        return {
            "relationshipSignal": "用户显露疲惫状态，更需要低压力陪伴和短回应。",
            "nextCareCue": "下次回应提示：少安排、少追问，语气放慢，先接住状态。",
        }
    if user_mood == "焦虑":
        return {
            "relationshipSignal": "用户可能处在压力或焦虑里，需要稳定感和清晰的小步建议。",
            "nextCareCue": "下次回应提示：先安抚，再给一两个可执行小步，不要铺太大。",
        }
    if user_mood == "低落":
        return {
            "relationshipSignal": "用户情绪偏低，需要被理解和陪着，而不是立刻被纠正。",
            "nextCareCue": "下次回应提示：先承认感受，再轻轻陪伴，不要急着总结道理。",
        }
    if user_mood == "开心":
        return {
            "relationshipSignal": "用户反馈偏积极，可以保持轻快互动并记住触发愉快的点。",
            "nextCareCue": "下次回应提示：可以更轻松一点，延续用户的好状态。",
        }
    return {
        "relationshipSignal": "本轮互动没有明显情绪峰值，保持自然陪伴即可。",
        "nextCareCue": "下次回应提示：延续最近话题「%s」，不要过度解读。" % compact(user_text or "", 40),
    }


def summarize_date(date: str, entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    当日摘要（relationship-log.ts:138-155）。

    dominantMood 取**最后一条非「未知」**的情绪（不是众数）；important 取**最后一条**带
    importantMoment 的；cue/signal 取**最后一条**的。三条都用 `.at(-1)` 口径，别改成统计。

    ⚠ 兜底文案只在「数组为空 / 字段缺失」时生效（源码 `??`），**空字符串不会**触发兜底：
      nextCareCue="" 会原样进摘要。这里用 _nn 保住这个语义。
    """
    moods = [e.get("userMood") for e in entries if e.get("userMood") != MOOD_UNKNOWN]
    dominant_mood = moods[-1] if moods else "平稳"
    important = None
    for e in reversed(list(entries)):
        if e.get("importantMoment"):
            important = e.get("importantMoment")
            break
    last = entries[-1] if entries else None
    cue = _nn(last.get("nextCareCue") if last else None, "保持自然陪伴。")
    signal = _nn(last.get("relationshipSignal") if last else None, "今天互动平稳。")
    parts = [
        "%s：用户最近状态偏「%s」。" % (date, dominant_mood),
        ("重要偏好：%s" % important) if important else signal,
        cue,
    ]
    return {
        "date": date,
        "updatedAt": _now_ms(),
        "summary": " ".join(parts),
        "nextCareCue": cue,
    }


class RelationshipLogStore:
    """
    关系画像存储（relationship-log.ts:157-215）。

    存储结构：`{"entries": [...], "dailySummaries": [...]}` 两键 JSON。
    路径可注入；默认走模块常量 RELATIONSHIP_LOG_FILE（= cyrene_mobile/data/relationship-log.json）。

    ⚠ 落盘用原子写（源码是直接 writeFileSync）——语义不变，只是断电时不会剩半个文件。
    ⚠ 读失败（文件损坏 / 不存在）一律**当空数据**返回，不像 memory.json 那样抛错走备份恢复。
      源码就是这么宽容（:128-130 catch 全吞）：关系画像是软数据，读不出来也不该挡住主流程。
    """

    def __init__(self, file_path: Optional[Any] = None) -> None:
        self.file_path = str(file_path) if file_path is not None else str(RELATIONSHIP_LOG_FILE)

    # ---- 读写 ----
    def read_data(self) -> Dict[str, Any]:
        try:
            if not os.path.exists(self.file_path):
                return {"entries": [], "dailySummaries": []}
            with open(self.file_path, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
            if not isinstance(parsed, dict):
                return {"entries": [], "dailySummaries": []}
            return {
                "entries": parsed.get("entries") if isinstance(parsed.get("entries"), list) else [],
                "dailySummaries": parsed.get("dailySummaries") if isinstance(parsed.get("dailySummaries"), list) else [],
            }
        except (ValueError, OSError):
            return {"entries": [], "dailySummaries": []}

    def write_data(self, data: Dict[str, Any]) -> None:
        _atomic_write_text(self.file_path, json.dumps(data, indent=2, ensure_ascii=False))

    # ---- 记录一轮 ----
    def record_turn(self, inp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        记一轮互动（relationship-log.ts:160-194）。两侧文本都为空 → 返回 None（不写盘）。

        副作用：entries 追加并截到 MAX_ENTRIES；当日摘要**替换**进 dailySummaries 后再截
        MAX_DAILY_SUMMARIES（同日只留一条，且总是排在末尾）。

        ⚠ userText / assistantText 会先 trim 再 compact(500)（源码 :161-171）；原样存超过 500 的
          长文会把文件撑爆，所以这里也截。
        """
        user_text = (inp.get("userText") or "").strip()
        assistant_text = (inp.get("assistantText") or "").strip()
        if not user_text and not assistant_text:
            return None

        now = _now_ms()
        user_mood = detect_user_mood(user_text)
        cue = derive_signal(user_text, user_mood)
        entry = dict(inp)
        entry["userText"] = compact(user_text, 500)
        entry["assistantText"] = compact(assistant_text, 500)
        entry["id"] = "rel-%d-%s" % (now, _rand6())
        entry["date"] = local_date(now)
        entry["createdAt"] = now
        entry["userMood"] = user_mood
        entry["relationshipSignal"] = cue["relationshipSignal"]
        # ⚠ 只有「明确边界」会带来 importantMoment；其它情况源码里该键是 undefined，
        #   写盘后键消失。这里同样不落空键。
        if "importantMoment" in cue:
            entry["importantMoment"] = cue["importantMoment"]
        entry["nextCareCue"] = cue["nextCareCue"]

        data = self.read_data()
        data["entries"].append(entry)
        data["entries"] = data["entries"][-MAX_ENTRIES:]

        same_date = [item for item in data["entries"] if item.get("date") == entry["date"]]
        summary = summarize_date(entry["date"], same_date)
        data["dailySummaries"] = [
            item for item in data["dailySummaries"] if item.get("date") != entry["date"]
        ] + [summary]
        data["dailySummaries"] = data["dailySummaries"][-MAX_DAILY_SUMMARIES:]

        self.write_data(data)
        return entry

    # ---- 拼上下文 ----
    def build_context(self) -> str:
        """
        拼「近期关系线索」注入块（relationship-log.ts:196-214）。无记录时返回空串。

        ⚠ recent 取最近 **8** 条（源码硬编码 slice(-8)，不在常量表里，别当 MAX_ENTRIES）。
        ⚠ cues 先按出现序去重、再 `.slice(-3)`：**保留的是最后 3 种**，不是最常出现的 3 种。
        """
        data = self.read_data()
        recent = data["entries"][-8:]
        if not recent:
            return ""

        last_mood = "平稳"
        for e in reversed(recent):
            if e.get("userMood") != MOOD_UNKNOWN:
                last_mood = e.get("userMood")
                break
        latest_summary = data["dailySummaries"][-1].get("summary") if data["dailySummaries"] else None
        preference = None
        for e in reversed(recent):
            if e.get("importantMoment"):
                preference = e.get("importantMoment")
                break
        cues: List[str] = []
        for e in recent:
            c = e.get("nextCareCue")
            if c and c not in cues:
                cues.append(c)
        cues = cues[-3:]

        lines = ["【近期关系线索】", "- 用户最近状态：%s" % last_mood]
        if latest_summary:
            lines.append("- 最近日记摘要：%s" % latest_summary)
        if preference:
            lines.append("- 重要互动偏好：%s" % preference)
        if cues:
            lines.append("- 下次回应提示：%s" % "；".join(cues))
        return "\n".join(lines)


# 默认 store 惰性创建（与 relationship-log.ts:217-222 的 defaultStore 同款）
_default_store: Optional[RelationshipLogStore] = None


def get_default_store() -> RelationshipLogStore:
    global _default_store
    if _default_store is None:
        _default_store = RelationshipLogStore()
    return _default_store


def record_turn(inp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """模块级入口（relationship-log.ts:224-226 的 recordRelationshipTurn）。"""
    return get_default_store().record_turn(inp)


def build_context() -> str:
    """模块级入口（relationship-log.ts:228-230）。"""
    return get_default_store().build_context()


# 跨查报告 4.8 用的命名，一并导出，避免接线时两套名字对不上
record_relationship_turn = record_turn
build_relationship_context = build_context


# ========== P1-7 L2 的 DMAE 封装（l2-dmae-manager.ts） ==========
# V5 L2 专用参数（l2-dmae-manager.ts:6-19，"与用户确认值一致"）。
# ⚠ 与一期 worldbook 那套 **差 4 键**（其余 8 键相同）：
#     userRewardBase 20 → 10、modelRewardBase 8 → 4、decayAlpha 1.5 → 1.0、decayBeta 0.3 → 0.2
#   语义：L2 条目**涨得更慢、掉得更慢**（奖励腰斩、衰减也压到 2/3 与 2/3）。
#   抄成 worldbook 那套会让手机端 L2 热层比桌面端活跃得多——这是本批次最容易抄错的地方。
L2_DMAE_PARAMS: Dict[str, float] = {
    "maxScore": 100,
    "promptThreshold": 30,
    "userRewardBase": 10,
    "wakeGamma": 0.5,
    "modelRewardBase": 4,
    "wakeLambda": 0.3,
    "decayAlpha": 1.0,
    "decayBeta": 0.2,
    "repeatRho": 0.5,
    "satPower": 2,
    "repeatWindow": 6,
    "wakeBonus": 5,
}

# L2 位次 I 梯度（l2-dmae-manager.ts:21-23）：
#   向量召回 top-4 依次给 I=[36,8,8,1]。源码注释：位次 1 期望活跃 4 轮，位次 2/3 期望 3 轮，
#   位次 4 期望 2 轮。I 只进衰减分母（D ∝ 1/√I），不进奖励——见一期 user_reward 的注释。
L2_INTRINSIC_BY_RANK: List[int] = [36, 8, 8, 1]


def intrinsic_for_rank(rank: int) -> int:
    """
    召回位次 → I（0-based）。超出表长**钳到末位**（源码 `Math.min(i, len-1)`）。

    ⚠ 第 5 名及以后全拿 1（= 忘得最快）。不要改成「越靠后越小」或返回 0：
      I=0 会让衰减 D 里出现 √0，一期引擎的 MIN_INTRINSIC_VALUE 兜底是给「缺省」用的，
      正常路径不该踩到它。
    """
    if rank < 0:
        rank = 0
    return L2_INTRINSIC_BY_RANK[min(rank, len(L2_INTRINSIC_BY_RANK) - 1)]


def l2_to_entry(l2: Dict[str, Any], intrinsic_value: float = 0.0):
    """
    把一条 L2 适配成一期引擎认的 Entry（l2-dmae-agent.ts:29-38 adaptL2）。

    映射：id←l2.id、keywords←l2.keywords、intrinsic_value←传入的 I、
          permanent←l2.isPinned、enabled←(status != "archived")。

    ⚠ `permanent` 在一期引擎里意味着「**完全旁路 DMAE**，连状态都不分配」
      （worldbook 的常驻条目永远注入）。映射 isPinned 到此是源码原意，但也因此
      **pinned 的 L2 不参与本轮 updateActivation**（会被批量循环跳过），由 get_active 里
      「pinned 优先直接拼在最前」补上。两处合起来才是完整语义，只看一半会以为 pinned 漏算了。
    """
    if M is None:  # pragma: no cover
        raise RuntimeError("cyrene_memory 未导入，无法构造 Entry：%s" % (_CYRENE_MEMORY_IMPORT_ERROR,))
    return M.Entry(
        id=l2.get("id"),
        title=l2.get("slug") or l2.get("id") or "",
        keywords=list(l2.get("keywords") or []),
        content=l2.get("content") or "",
        permanent=bool(l2.get("isPinned")),
        enabled=l2.get("status") != "archived",
        intrinsic_value=float(intrinsic_value),
    )


class L2DmaeManager:
    """
    L2 Working Memory 的 DMAE 管理器（l2-dmae-manager.ts:60-200）。

    **复用一期的 `cyrene_memory.DmaeManager`**，只补三件 L2 特有的事：
      1) 用 L2_DMAE_PARAMS 而不是 worldbook 默认参数；
      2) 按召回位次给 I（一期里 I 是条目静态字段，L2 这边是**每轮召回序号**动态给的）；
      3) 状态持久化到 store 的 `l2DmaeStates`（一期是独立的 state 文件）。

    ⚠ 一期的 `DmaeManager.update_activation` 是**单条目**签名，而桌面端 L2 走的是批量主循环
      `updateActivation(entries, userText, modelText, turn)`。本类在调用侧补齐批量的三条语义，
      这三条**必须齐**，少一条数值就会跑偏：
        a) 跳过 `permanent`（isPinned）、`!enabled`（archived）、`keywords` 为空的条目；
        b) 跳过「状态为 Archived 且本轮 user/model 都没命中」的条目——一期引擎不做这个短路，
           docstring 说「跳过与否不改 activation」，**但那是对 activation 而言**：
           不跳过会让该条的 user/model silence 各 +1，状态被悄悄推进。所以这里显式跳过；
        c) 同一轮所有条目**共用一个 turn 号**（repeatWindow 靠它判重）。一期引擎把 turn 藏成
           自增计数器（每调一次 +1），因此这里每调一条前把它压回 `turn - 1`。
           ⚠ 这是本模块唯一一处**动了一期引擎内部属性**（`dmae.turn`）的地方：不改它，
             一次批量更新里每条看到的 turn 都不同，去重窗口会错位。

    ⚠ `get_active_l2_for_prompt` 与 update 的取舍不同：update 里 pinned 被跳过，取用时 pinned
      **无条件置顶**并优先占名额。这是刻意分工（常驻不靠分数，靠身份）。
    """

    def __init__(self, store: Dict[str, Any]) -> None:
        if M is None:  # pragma: no cover
            raise RuntimeError("cyrene_memory 未导入，L2DmaeManager 不可用：%s" % (_CYRENE_MEMORY_IMPORT_ERROR,))
        self.store = store
        self.dmae = M.DmaeManager(params=dict(L2_DMAE_PARAMS))
        # 运行时缓存每个 L2 的 I：源码注释「由召回器按位次设置，不存 DmaeManager EntryState」
        self._intrinsic: Dict[str, float] = {}
        self.turn = 0
        self.loaded = False

    # ---- 状态装载 / 回写 ----
    def load_states(self) -> None:
        """从 store 的 l2DmaeStates 装载（l2-dmae-manager.ts:76-91）。"""
        states = get_all_l2_dmae_states(self.store)
        self.dmae.clear()
        self._intrinsic.clear()
        for s in states:
            if not isinstance(s, dict):
                continue
            self.dmae.set_state(s.get("l2Id"), M.EntryState(
                activation=s.get("activation") or 0,
                user_silence=s.get("userSilence") or 0,
                model_silence=s.get("modelSilence") or 0,
                recent_user_hits=s.get("recentUserHits") or [],
            ))
            self._intrinsic[s.get("l2Id")] = s.get("intrinsicValue") or 0
        self.loaded = True

    def sync_to_store(self) -> None:
        """
        把引擎状态写回 store（l2-dmae-manager.ts:184-199）。

        ⚠ intrinsicValue 回写时**优先用本轮的 I**，没有才保留旧值（源码 :192 注释
          「保持召回器设置的 I」）。写成引擎的默认 0 会让上一轮的位次信息被抹掉。
        ⚠ **state 的大小写：源码此处自身不一致（初始化写小写 `"archived"`、回写用 worldbook
          标签 `"Active"/"Dormant"/"Archived"`），我们取小写以求自洽**。源码 `defaultL2DmaeState` /
          `initL2DmaeStateIfMissing` / `repairMigrations` 写的都是小写（L2Memory.status 口径），
          而本来这里回写的 `deriveState(...)` 返回的是 worldbook 标签（首字母大写），
          源码靠 `state as L2DmaeState["state"]` 一句断言把类型冲突盖住（l2-dmae-manager.ts:196）。
          现统一成小写三态（active / dormant / archived），与初始化口径一致，
          下游按 state 字符串判断不必再兼容两种写法。
          ⚠ 该字段在本模块内**只写不读**：三态判定一律现算（derive_state_at），不受它影响。
        """
        params = self.dmae.get_params()
        for s in get_all_l2_dmae_states(self.store):
            if not isinstance(s, dict):
                continue
            l2_id = s.get("l2Id")
            engine_state = self.dmae.get_state(l2_id)
            if engine_state is None:
                continue
            s["activation"] = engine_state.activation
            s["intrinsicValue"] = _nn(self._intrinsic.get(l2_id), s.get("intrinsicValue"))
            s["userSilence"] = engine_state.user_silence
            s["modelSilence"] = engine_state.model_silence
            s["recentUserHits"] = list(engine_state.recent_user_hits)
            # 统一小写三态：源码此处返回 worldbook 标签（首字母大写），我们取小写求自洽
            s["state"] = M.derive_state_at(engine_state.activation, params["promptThreshold"]).lower()

    def _ensure_state(self, l2_id: str) -> None:
        """缺失状态就补（l2-dmae-manager.ts:131-141，源码会顺带落盘）。"""
        if self.dmae.get_state(l2_id) is not None:
            return
        created = init_l2_dmae_state_if_missing(self.store, l2_id)
        self.dmae.set_state(l2_id, M.EntryState(
            activation=created.get("activation") or 0,
            user_silence=created.get("userSilence") or 0,
            model_silence=created.get("modelSilence") or 0,
            recent_user_hits=created.get("recentUserHits") or [],
        ))

    # ---- 召回位次 ----
    def set_recalled_intrinsic_values(self, recalled_ids: Sequence[str]) -> None:
        """
        按召回位次写 I，并把「本轮被召回但还冻着」的条目唤醒到阈值之上
        （l2-dmae-manager.ts:93-111）。

        ⚠ 唤醒是**置值**：`activation = promptThreshold + wakeBonus = 35`（不是叠加）。
          源码注释说「命中 Archived 时先 Wake-Up 到 promptThreshold + wakeBonus；否则保持原
          activation」——注意判据是**未唤醒前的** activation 派生出的状态。
        ⚠ 唤醒后的 35 已经 ≥ 阈值 30，所以紧接着的批量主循环里 Floor 分支**不会**再触发
          （它只在 oldState == Archived 时才置值）。两处都写 Floor 不会叠加，但会让 35 被
          覆盖成同样 35——看不出问题，改错成「累加」就立刻爆分。
        """
        params = self.dmae.get_params()
        for i, l2_id in enumerate(recalled_ids):
            st = self.dmae.get_state(l2_id)
            if st is None:
                st = M.EntryState()
            self._intrinsic[l2_id] = intrinsic_for_rank(i)
            activation = st.activation
            if self.dmae.derive_state(activation) == M.STATES["ARCHIVED"]:
                activation = min(params["maxScore"], params["promptThreshold"] + params["wakeBonus"])
            self.dmae.set_state(l2_id, M.EntryState(
                activation=activation,
                user_silence=st.user_silence,
                model_silence=st.model_silence,
                recent_user_hits=list(st.recent_user_hits),
            ))

    # ---- 主循环 ----
    def update_activation(
        self,
        l2_list: Sequence[Dict[str, Any]],
        user_text: str,
        model_text: str,
        recalled_ids: Sequence[str],
        turn: Optional[int] = None,
    ) -> None:
        """
        跑一轮 L2 DMAE（l2-dmae-manager.ts:113-159 + worldbook.ts:232-330 的批量语义）。

        `recalled_ids` 必须是**按相似度排好序**的 top-K：位次就是 I 的来源。
        `turn` 不传则自增（与源码 `turn ?? ++this.turnCounter` 一致）。

        ⚠ 命中判定用一期的 `match_keywords`（**大小写不敏感的子串**），而桌面端批量主循环用的是
          `user.includes(kw)`（**大小写敏感**）。差异只在「关键词表里有大写字母」时出现：
          extract_memory_keywords 产出的词已全部 lower（中文无大小写、英文走 `input.lower()`），
          所以实际不可达。留此注以免日后有人拿手写 keywords 喂进来。
        ⚠ 本轮既没召回到、关键词也没命中、且冷的条目会被跳过（见类 docstring 的 b 条）。
        """
        if not self.loaded:
            self.load_states()
        t = turn if turn is not None else self.turn + 1
        self.turn = t

        for l2 in l2_list:
            self._ensure_state(l2.get("id"))
        self.set_recalled_intrinsic_values(recalled_ids)

        for l2 in l2_list:
            if l2.get("isPinned") or l2.get("status") == "archived":
                continue
            if not (l2.get("keywords") or []):
                continue
            st = self.dmae.get_state(l2.get("id"))
            if st is None:
                continue
            entry = l2_to_entry(l2, self._intrinsic.get(l2.get("id"), 0))
            user_hit = M.match_keywords(user_text or "", entry)
            model_hit = M.match_keywords(model_text or "", entry)
            if self.dmae.derive_state(st.activation) == M.STATES["ARCHIVED"] and not user_hit and not model_hit:
                continue
            # 同一轮所有条目共用一个 turn（见类 docstring 的 c 条）
            self.dmae.turn = t - 1
            self.dmae.update_activation(st, user_hit, model_hit, self._intrinsic.get(l2.get("id"), 0))

        self.sync_to_store()

    # ---- 取可注入的 L2 ----
    def get_active_l2_for_prompt(self, l2_list: Sequence[Dict[str, Any]], max_count: int = 4) -> List[Dict[str, Any]]:
        """
        取可进 prompt 的 L2（l2-dmae-manager.ts:161-176）：pinned 无条件在前，其余按 activation
        降序取 Active 态，合计截 max_count。

        ⚠ 默认 **4** 条（源码 maxCount=4），与一期 worldbook 的 MAX_ACTIVE=8 不是一回事。
        ⚠ 非 pinned 的候选要求 `status != "archived"`；排序只按 activation，没有 priority tiebreak。
        """
        if not self.loaded:
            self.load_states()
        params = self.dmae.get_params()
        pinned = [l2 for l2 in l2_list if l2.get("isPinned")]
        active_pairs: List[Tuple[float, Dict[str, Any]]] = []
        for l2 in l2_list:
            if l2.get("isPinned") or l2.get("status") == "archived":
                continue
            st = self.dmae.get_state(l2.get("id"))
            if st is None:
                continue
            if M.derive_state_at(st.activation, params["promptThreshold"]) == M.STATES["ACTIVE"]:
                active_pairs.append((st.activation, l2))
        active_pairs.sort(key=lambda pair: pair[0], reverse=True)
        return (list(pinned) + [l2 for _, l2 in active_pairs])[:max_count]


# ========== P3-1 词法召回（向量召回的降级替身） ==========
# 桌面端用向量召回（embedding top-K）；手机端不加载模型、不装依赖，改用一期已验收的
# BM25 词法相似度。**降级的事实要如实记着**：换一种说法但意思一样的旧事捞不回来，
# 这是已知代价，不是 bug。将来接云端 embedding 时只换这一层，别的都不用动。
def l2_recallable(l2: Dict[str, Any]) -> bool:
    """能不能参与召回：只要不是 archived 就可以。

    ⚠ 这里**不要求 keywords 非空**：桌面端向量召回看的是正文向量，没有关键词的条目
      照样进 top-K；keywords 只在 DMAE 的命中判定里用（见 update_activation 的跳过条件）。
      两处判据不同是源码原意，别为了「看着统一」合并。
    """
    return isinstance(l2, dict) and l2.get("status") != "archived"


def recall_l2_ids(query: str, l2_list: Sequence[Dict[str, Any]], top_k: int = 8) -> List[str]:
    """词法召回：对 L2 打分取 top-K，返回**按相似度降序**的 id 列表。

    ⚠ 返回的顺序就是 DMAE 的位次（I = L2_INTRINSIC_BY_RANK[i]），调用方拿到后
      **不要再排序**，否则 I 的梯度会错位。
    ⚠ 复用一期的 lexical_recall —— 它只用 title / keywords / 正文前 600 字打分
      （那三条口径在一期已验收，这里不重复实现）。
    """
    if M is None:                                            # pragma: no cover
        return []
    cands = [x for x in (l2_list or []) if l2_recallable(x)]
    if not cands:
        return []
    entries = [l2_to_entry(x) for x in cands]
    hits = M.lexical_recall(str(query or ""), entries, top_k=max(1, int(top_k or 1)))
    return [e.id for _score, e in hits if e.id]
