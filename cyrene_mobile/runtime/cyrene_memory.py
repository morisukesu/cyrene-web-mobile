#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
昔涟 · 手机端「世界书」引擎（DMAE）—— 桌面端 Cyrene-Agent 的纯算法移植。

对应桌面端源码（src/main/rag/）：
  worldbook.ts            DMAE 引擎本体 + .md 解析 + 注入拼装
  worldbook-constants.ts  阈值 / 状态 / 注入文案常量

移植边界（为什么只搬这些）：
  只搬纯算法与纯数据结构。桌面端 WorldbookManager 外层那些 Electron / fs.watch /
  logger 的壳全部剥掉：条目列表由调用方（cyrene_web.py）灌入，本模块不 import 任何
  第三方库、不读写模型文件、不发网络请求。这样本机就能直接跑单测，不必起服务。

⚠ 桌面端源码 worldbook.ts:29-31 明确记过一个坑：DMAE 状态**不能挂在 entry 对象上**。
  parse_worldbook_md / load_worldbook_dir 会整表替换 entries（重新读 .md），挂在条目
  上的 activation 会在重载时一起丢。所以本模块的状态一律放在 DmaeManager.states 这张
  以 entry.id 为键的**独立表**里，并由 load_state / save_state 单独落盘；条目对象只承载
  静态定义，永远不带运行时状态。

⚠ 三态阈值的语义说明（未确认项，已按桌面端源码对齐）：
  derive_state 以 `promptThreshold`（默认 30）为 Active 线：activation>=30 → Active，
  0<x<30 → Dormant，<=0 → Archived。源码 worldbook.ts:163-167 即此闭区间语义
  （`<=0` 走 Archived、`>= threshold` 走 Active），任务书里的「阈值 30 为 Active 线」
  与之一致，故不存在二义；此处标注仅为留痕。
"""

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_DMAE_PARAMS",
    "WORLDBOOK_CONSTANTS",
    "INJECTION_HEADER",
    "INJECTION_PREAMBLE",
    "STATES",
    "Entry",
    "EntryState",
    "DmaeManager",
    "parse_worldbook_md",
    "load_worldbook_dir",
    "load_state",
    "save_state",
    "build_injection",
    "match_keywords",
    "gate_saturation",
    "gate_repeat",
    "derive_state_at",
]


# ========== 常量（数值一律来自源码，勿凭「看着合理」改动） ==========
# 来源：桌面端 src/main/rag/worldbook.ts:75-88 DEFAULT_DMAE_PARAMS
# 12 键顺序与源码一致，便于逐项对账。
DEFAULT_DMAE_PARAMS: Dict[str, float] = {
    "maxScore": 100,          # 激活度物理上界
    "promptThreshold": 30,    # >= 此值进 Prompt（业务层用）
    "userRewardBase": 20,     # Bu：每次 userHit 的基础奖励
    "wakeGamma": 0.5,         # γ：久别重逢增益（ln(1+U_old) 的系数）
    "modelRewardBase": 8,     # Bm：modelHit + Active 的上限
    "wakeLambda": 0.3,        # λ：模型奖励随 U_old 的衰减率
    "decayAlpha": 1.5,        # α：用户沉默权重（须 > β）
    "decayBeta": 0.3,         # β：模型沉默权重
    "repeatRho": 0.5,         # ρ：短窗口内重复命中的抑制强度
    "satPower": 2,            # p：接近 A_max 时的压缩幂次
    "repeatWindow": 6,        # w：重复统计窗口（轮）
    "wakeBonus": 5,           # B_w：Archived 复活的初始补偿
}

# 来源：桌面端 src/main/rag/worldbook-constants.ts:16-34
WORLDBOOK_CONSTANTS: Dict[str, Any] = {
    "MAX_ACTIVE": 8,                 # 终态注入条数硬上界
    "DEFAULT_INTRINSIC_VALUE": 60,   # .md 未写「内在价值」时的回落值
    "MIN_INTRINSIC_VALUE": 1,        # QuadraticResistanceDecay 除零保护：I 最小取 1
    "EPSILON": 0.01,                 # Rm < D 不变量保护：Rm = min(Rm, D - ε)
    "FLOOR_TRIGGER_STATE": "Archived",  # 只有 Archived 复活才触发 Floor
    "STATES": {
        "ACTIVE": "Active",
        "DORMANT": "Dormant",
        "ARCHIVED": "Archived",
    },
}

# 三态标签单独导出（与 WORLDBOOK_CONSTANTS["STATES"] 同一对象语义）
STATES: Dict[str, str] = WORLDBOOK_CONSTANTS["STATES"]

# 来源：桌面端 src/main/rag/worldbook-constants.ts:36-39（逐字复制，勿意译）
INJECTION_HEADER = "【已激活的世界知识】"
INJECTION_PREAMBLE = (
    "以下内容已由当前用户消息触发，视为真实且已知。"
    "回复时请自然使用这些信息，不要说「不知道」、「第一次听说」或要求用户介绍，"
    "除非内容本身存在矛盾。"
)

# 注入被 max_chars 截断时追加的提示（任务书给定文案）
INJECTION_OMIT_NOTE = "（以上为已激活知识的一部分，其余因篇幅省略）"

# 状态文件 schema 版本
STATE_SCHEMA_VERSION = 1


# ========== 数据结构 ==========
class Entry:
    """
    一条世界书条目（纯静态定义，⚠ 绝不承载运行时状态）。

    字段对齐桌面端 WorldbookEntry（worldbook.ts:8-17）：
      id / title / keywords / content / priority / permanent / enabled /
      intrinsic_value / link_triggers，另加 source_file 便于排错。

    - priority:    v3.4 起仅作排序 tiebreaker，不参与 DMAE 打分
    - permanent:   常驻：始终注入，旁路 DMAE（不给它分配状态）
    - intrinsic_value: ★ 长期价值基准（固定），参与 Floor 与 Resistance，不参与 Reward
    - link_triggers: 连带触发词（One-Shot，一轮有效）；[] 表示无
    """

    __slots__ = (
        "id", "title", "keywords", "content", "priority",
        "permanent", "enabled", "intrinsic_value", "link_triggers", "source_file",
    )

    def __init__(
        self,
        id: str,
        title: str,
        keywords: Optional[List[str]] = None,
        content: str = "",
        priority: int = 0,
        permanent: bool = False,
        enabled: bool = True,
        intrinsic_value: float = float(WORLDBOOK_CONSTANTS["DEFAULT_INTRINSIC_VALUE"]),
        link_triggers: Optional[List[str]] = None,
        source_file: str = "",
    ) -> None:
        self.id = id
        self.title = title
        self.keywords = list(keywords or [])
        self.content = content
        self.priority = priority
        self.permanent = permanent
        self.enabled = enabled
        self.intrinsic_value = intrinsic_value
        self.link_triggers = list(link_triggers or [])
        self.source_file = source_file

    def __repr__(self) -> str:  # 便于排错时看清条目身份
        return "Entry(id=%r, keywords=%r, permanent=%r, enabled=%r, iv=%r)" % (
            self.id, self.keywords, self.permanent, self.enabled, self.intrinsic_value,
        )


class EntryState:
    """
    一条条目的 DMAE 运行时状态（对齐 worldbook.ts:32-38）。

    ⚠ 特意不做成 Entry 的字段：见模块头关于「状态表不挂 entry」的坑。
    这里也没有 state 字段——状态由 (activation, threshold) 经 derive_state 派生。
    """

    __slots__ = ("activation", "user_silence", "model_silence", "recent_user_hits")

    def __init__(
        self,
        activation: float = 0.0,
        user_silence: int = 0,
        model_silence: int = 0,
        recent_user_hits: Optional[List[int]] = None,
    ) -> None:
        self.activation = activation          # 0..maxScore
        self.user_silence = user_silence      # 距上次用户命中的轮数
        self.model_silence = model_silence    # 距上次模型命中的轮数
        self.recent_user_hits = list(recent_user_hits or [])  # 窗口内的用户命中轮次编号

    def __repr__(self) -> str:
        return "EntryState(activation=%.3f, us=%d, ms=%d, hits=%r)" % (
            self.activation, self.user_silence, self.model_silence, self.recent_user_hits,
        )


# ========== 门控函数（worldbook.ts:119-125） ==========
def gate_saturation(activation: float, p: float, a_max: float) -> float:
    """G_sat = (1 − A_old/A_max)^p；A_old >= A_max 时取 0（源码同款早退）。"""
    if activation >= a_max:
        return 0.0
    return (1.0 - activation / a_max) ** p


def gate_repeat(hit_count: int, rho: float) -> float:
    """G_repeat = 1 / (1 + ρ·n_w)；n_w 为当前 repeatWindow 内的命中次数（含本轮）。"""
    return 1.0 / (1.0 + rho * hit_count)


def derive_state_at(activation: float, threshold: float) -> str:
    """纯函数版三态判定（worldbook.ts:163-167），供模块级函数复用。"""
    if activation <= 0:
        return STATES["ARCHIVED"]
    if activation >= threshold:
        return STATES["ACTIVE"]
    return STATES["DORMANT"]


# ========== .md 解析 ==========
_LIST_SPLIT_RE = re.compile(r"[,，、]")  # 桌面端按 [,，、] 切；任务书只要求 , 与 ，，多认顿号是超集
_KV_RE = re.compile(r"^[-*•]\s*(.+?)\s*[:：]\s*(.*)$")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

# 键名容错表（桌面端 worldbook.ts:464-494 的别名集合）
_K_TRIGGER = ("触发词",)
_K_PERMANENT = ("常驻",)
_K_INTRINSIC = ("内在价值", "初始分", "intrinsic_value", "initial_score")
_K_PRIORITY = ("优先级",)
_K_LINK = ("连带触发词", "连带触发", "link_triggers")

_TRUTHY = ("是", "true", "yes", "1")
_NONE_TOKENS = ("无", "無", "none", "-", "—", "/")


def _split_kv(line: str) -> Tuple[Optional[str], Optional[str]]:
    """把 `- 键: 值` 拆成 (键, 值)；不是键值行返回 (None, None)。"""
    m = _KV_RE.match(line.strip())
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


def _split_list(val: str, allow_none_token: bool = False) -> List[str]:
    """按 , / ， / 、 切词并 strip；allow_none_token 时把「无/none/-」视为空表。"""
    v = (val or "").strip()
    if allow_none_token and (v == "" or v.lower() in _NONE_TOKENS):
        return []
    return [p.strip() for p in _LIST_SPLIT_RE.split(v) if p.strip()]


def _parse_number(val: str, default: float) -> float:
    """先整体 float；失败再取前导数字（对齐 JS parseFloat 的宽松）；再失败回落默认。"""
    v = (val or "").strip()
    try:
        return float(v)
    except (ValueError, TypeError):
        pass
    m = _NUM_RE.match(v)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            pass
    return float(default)


def _parse_int(val: str, default: int = 0) -> int:
    v = (val or "").strip()
    try:
        return int(float(v))
    except (ValueError, TypeError):
        pass
    m = _NUM_RE.match(v)
    if m:
        try:
            return int(float(m.group(0)))
        except ValueError:
            pass
    return int(default)


def parse_worldbook_md(text: str, source_file: str = "") -> List[Entry]:
    """
    解析一份世界书 .md 文本为 Entry 列表。

    结构（worldbook.ts:425-433 的格式头注释）：
        ## 条目名
        - 触发词: 词1, 词2
        - 常驻: 是
        - 优先级: 200
        - 内在价值: 60
        正文段落...
        ---

    规则要点（对齐 worldbook.ts:434-540）：
      · 只在**行首** `## ` 处切条；H1 标题与开头 blockquote 属文件头，不进条目。
      · 元数据区从标题下一行开始，遇到空行 / H1 / `---` / 首个非 `- 键值` 行即结束。
      · 元数据行整段由调用方解析；未知 `- 键:` 静默忽略（源码同款）。
      · 正文取「元数据区之后 → 下一个 `## ` 或 `---` 之前」的全部行，首尾 strip；
        与源码一致**保留段间空行**（注入时可读性依赖它）。
      · 正文为空的条目不产出（源码 :523 同款）。
      · `触发词` 为空 → enabled=False（无触发入口，等价停用）。
      · id 用标题文本（跨重载稳定）；空标题回落「未命名」，重复标题追加 `#2`/`#3`。
    """
    lines = (text or "").split("\n")
    entries: List[Entry] = []
    id_counter: Dict[str, int] = {}  # 标题基名 -> 已出现次数
    n = len(lines)
    i = 0

    while i < n:
        # 找下一条 `## `（行首）
        if not lines[i].startswith("## "):
            i += 1
            continue

        title = lines[i][3:].strip()
        i += 1

        # ---- 默认值 ----
        keywords: List[str] = []
        link_triggers: List[str] = []
        priority = 5  # 桌面端 worldbook.ts:455 `let priority = 5`（缺省 5，非 0）
        permanent = False
        intrinsic_value = float(WORLDBOOK_CONSTANTS["DEFAULT_INTRINSIC_VALUE"])

        # ---- 元数据区 ----
        while i < n:
            s = lines[i].strip()
            if s == "" or s.startswith("# "):
                break  # 空行 / 顶层标题 → 元数据结束（空行本身留给正文区，strip 后无影响）
            if s.startswith("---"):
                i += 1
                break  # 分隔线本身不是正文，消费掉
            key, val = _split_kv(s)
            if key is None:
                break  # 出现非键值行 → 正文开始
            if key in _K_TRIGGER:
                keywords = _split_list(val)
            elif key in _K_PERMANENT:
                permanent = val.strip().lower() in _TRUTHY
            elif key in _K_INTRINSIC:
                intrinsic_value = _parse_number(val, WORLDBOOK_CONSTANTS["DEFAULT_INTRINSIC_VALUE"])
            elif key in _K_PRIORITY:
                # 与桌面端一致：worldbook.ts:474 `priority = parseInt(val) || 5`
                # 解析失败 / 得 0（falsy）都回落 5。
                priority = _parse_int(val, 5) or 5
            elif key in _K_LINK:
                link_triggers = _split_list(val, allow_none_token=True)
            # 其余未知键：忽略（源码 :502-504 同款）
            i += 1

        # ---- 正文区：直到下一个 `## ` 或 `---` ----
        body_lines: List[str] = []
        while i < n:
            s = lines[i].strip()
            if s.startswith("## ") or s == "---":
                break
            body_lines.append(lines[i])
            i += 1
        content = "\n".join(body_lines).strip()

        if content:
            base = title if title else "未命名"
            if base in id_counter:
                id_counter[base] += 1
                eid = "%s#%d" % (base, id_counter[base])
            else:
                id_counter[base] = 1
                eid = base
            entries.append(Entry(
                id=eid,
                title=title,
                keywords=keywords,
                content=content,
                priority=priority,
                permanent=permanent,
                enabled=bool(keywords),  # 触发词为空 → 停用
                intrinsic_value=intrinsic_value,
                link_triggers=link_triggers,
                source_file=source_file,
            ))
        # 空正文条目直接跳过（若因空行循环已停在标题行，外层 while 会重新识别）

    return entries


class LoadResult(list):
    """
    list[Entry] 的子类，额外挂 `errors`。

    这样 load_worldbook_dir 的字面返回类型仍是 list[Entry]（符合任务书签名），
    同时把「哪些文件读/解析失败」带出来，不必让调用方去猜，也不用抛异常。
    """

    def __init__(self, items: Optional[List[Entry]] = None, errors: Optional[List[str]] = None):
        super().__init__(items or [])
        self.errors: List[str] = list(errors or [])


def load_worldbook_dir(dirpath) -> "LoadResult":
    """
    读取目录下所有 `*.md`，跳过以 `_` 开头的文件（如 _glossary.md）。

    返回 LoadResult（list[Entry]），`result.errors` 里收集逐文件错误。
      · 目录不存在：返回空列表 + 一条 error，不抛异常。
      · 单文件读取/解析失败：记 error、继续下一个文件，绝不阻断整体加载。
    文件排序后再读，保证同一目录多次加载的条目顺序稳定。
    """
    errors: List[str] = []
    entries: List[Entry] = []
    d = Path(dirpath)

    if not d.is_dir():
        return LoadResult(entries, ["目录不存在或不是目录: %s" % dirpath])

    try:
        files = sorted(
            p for p in d.iterdir()
            if p.is_file() and p.name.endswith(".md") and not p.name.startswith("_")
        )
    except OSError as e:
        return LoadResult(entries, ["目录不可读: %s (%s)" % (dirpath, e)])

    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - 单文件失败必须降级为 error，不冒泡
            errors.append("%s: 读取失败 %s: %s" % (fp.name, type(e).__name__, e))
            continue
        try:
            entries.extend(parse_worldbook_md(text, source_file=fp.name))
        except Exception as e:  # noqa: BLE001
            errors.append("%s: 解析失败 %s: %s" % (fp.name, type(e).__name__, e))
    return LoadResult(entries, errors)


# ========== DMAE 主循环 ==========
class DmaeManager:
    """
    DMAE 激活度引擎（worldbook.ts:178-346 的 DmaeManager<T> 去掉泛型后的等价物）。

    ⚠ states 是独立状态表（id -> EntryState）。绝不能把它塞进 Entry：条目表会随
      .md 重载整表替换，状态必须活过重载（见模块头）。turn 是单调递增的轮次
      计数器，只用于 recentUserHits 的窗口判定——跨轮次编号（不是墙钟时间）。
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None) -> None:
        # 用 dict 合并，绝不原地改 DEFAULT_DMAE_PARAMS（源码 :186 同款展开语义）
        merged: Dict[str, Any] = dict(DEFAULT_DMAE_PARAMS)
        if params:
            for k, v in params.items():
                if k in DEFAULT_DMAE_PARAMS:
                    merged[k] = v
        self.params: Dict[str, Any] = merged
        self.states: Dict[str, EntryState] = {}
        self.turn = 0

    # ---- 参数 / 状态存取 ----
    def get_params(self) -> Dict[str, Any]:
        return self.params

    def init_entry(self, entry_id: str) -> EntryState:
        st = EntryState()
        self.states[entry_id] = st
        return st

    def init_entries(self, entries: List[Entry]) -> None:
        """重新初始化状态表：只为 enabled 且 !permanent 的条目建状态（源码 :214-221）。"""
        self.states = {}
        for e in entries:
            if e.enabled and not e.permanent:
                self.states[e.id] = EntryState()

    def get_state(self, entry_id: str) -> Optional[EntryState]:
        return self.states.get(entry_id)

    def set_state(self, entry_id: str, st: EntryState) -> None:
        self.states[entry_id] = st

    def clear(self) -> None:
        self.states = {}

    # ---- 三态派生 ----
    def derive_state(self, activation: float) -> str:
        return derive_state_at(activation, self.params["promptThreshold"])

    # ---- 奖励 / 衰减 ----
    def user_reward(self, state: EntryState, I: float) -> float:  # noqa: E741 - 沿用源码符号 I
        """
        Ru* = Bu × (1 + γ·ln(1+U_old)) × G_sat × G_repeat   [仅 userHit 调用]

        state.activation 传入的是本轮起始值 aInit（Floor 复活后的值）；
        state.user_silence 是更新前的 U_old；state.recent_user_hits 已含本轮。

        ⚠ I（intrinsic_value）刻意不参与本式：源码 worldbook.ts:145 有明确注释——
          避免「高价值条目既涨得快又忘得慢」而天然霸榜。参数位保留只是为了对齐
          接口形状，方便第二期迁 L2 时替换策略。
        """
        p = self.params
        n_w = len(state.recent_user_hits)
        base = p["userRewardBase"] * (1.0 + p["wakeGamma"] * math.log(1.0 + state.user_silence))
        g_sat = gate_saturation(state.activation, p["satPower"], p["maxScore"])
        g_rep = gate_repeat(n_w, p["repeatRho"])
        return base * g_sat * g_rep

    def model_reward(self, state: EntryState) -> float:
        """Rm = Bm × e^(−λ·U_old)   [仅 modelHit 且原状态 Active 时调用]。"""
        return self.params["modelRewardBase"] * math.exp(-self.params["wakeLambda"] * state.user_silence)

    def compute_decay(self, state: EntryState, I: float) -> float:  # noqa: E741
        """
        D = (α·U_new² + β·M_new²) / √I

        ⚠ state 传入的必须是**更新后**的 silence（U_new/M_new），与源码 :290-294 一致。
        ⚠ I 取 max(MIN_INTRINSIC_VALUE, I)：sqrt(0) 会除零爆炸（worldbook-constants.ts:19）。
        """
        i_eff = max(float(WORLDBOOK_CONSTANTS["MIN_INTRINSIC_VALUE"]), float(I))
        us = state.user_silence
        ms = state.model_silence
        raw = self.params["decayAlpha"] * us * us + self.params["decayBeta"] * ms * ms
        return raw / math.sqrt(i_eff)

    # ---- 主循环 ----
    def update_activation(self, state: EntryState, user_hit: bool, model_hit: bool, I: float) -> EntryState:
        """
        推进一条条目一轮：奖励 − 衰减，clamp 到 [0, maxScore]，并同步 silence / 命中窗口。

        顺序严格照源码 worldbook.ts:252-310：
          1. 取 aOld / U_old / M_old，派生 oldState
          2. Floor 复活：仅当 oldState==Archived 且 userHit 时，aInit = min(max, T + B_w)
             —— 是**置值**不是叠加，故先算 aInit 再算奖励
          3. 更新 silence：userHit→us=0，否则 us+1；命中(modelHit 也算)→ms=0，否则 ms+1
          4. 维护 recentUserHits：本轮 userHit 追加当前 turn，并裁掉 turn-w 及更早的
          5. Ru*（仅 userHit，n_w 含本轮）、Rm（仅 modelHit 且 oldState==Active，
             被 EPSILON 压住保证 < D）
          6. aNew = aInit + Ru* + Rm − D，clamp 到 [0, maxScore]

        ⚠ 与桌面端 DmaeManager 的一处差异：桌面端会跳过「Archived 且本轮未命中」的条目
          （worldbook.ts:257-260，纯粹为了省算力）。本移植由调用方决定要不要对每条调用，
          这里不做跳过，以保持签名简短、行为可预测；接线批次若需同款短路，在调用侧加即可。
          对本条做跳过与否不影响结果：Archived 且未命中时 Ru*=0、Rm=0，D 因 us/ms 各 +1
          而 >0，只会让 activation 停在 0，仍 clamp 为 0。
        """
        p = self.params
        max_score = p["maxScore"]
        threshold = p["promptThreshold"]

        a_old = state.activation
        us_old = state.user_silence
        ms_old = state.model_silence
        old_state = derive_state_at(a_old, threshold)

        # 2) Floor 复活（先记原状态，再决定 aInit）
        a_init = a_old
        if user_hit and old_state == WORLDBOOK_CONSTANTS["FLOOR_TRIGGER_STATE"]:
            a_init = min(max_score, threshold + p["wakeBonus"])

        # 3) silence
        us_new = 0 if user_hit else us_old + 1
        ms_new = 0 if (user_hit or model_hit) else ms_old + 1

        # 4) 命中窗口（跨轮次编号，只用单调 turn，不做墙钟时间）
        self.turn += 1
        turn = self.turn
        window = p["repeatWindow"]
        recent = [t for t in state.recent_user_hits if t > turn - window]
        if user_hit:
            recent.append(turn)
        n_w = len(recent)

        # 5a) 用户奖励：snap 用 aInit / U_old / 已含本轮的窗口
        snap_for_reward = EntryState(
            activation=a_init,
            user_silence=us_old,
            model_silence=ms_old,
            recent_user_hits=recent,
        )
        reward_u = self.user_reward(snap_for_reward, I) if user_hit else 0.0

        # 5b) 衰减：用更新后的 silence
        snap_for_decay = EntryState(user_silence=us_new, model_silence=ms_new)
        decay = self.compute_decay(snap_for_decay, I)

        # 5c) 模型奖励：仅 modelHit 且原状态 Active；EPSILON 不变量 Rm < D
        reward_m = 0.0
        if model_hit and old_state == STATES["ACTIVE"]:
            snap_for_model = EntryState(activation=a_old, user_silence=us_old, model_silence=ms_old)
            raw_rm = self.model_reward(snap_for_model)
            reward_m = max(0.0, min(raw_rm, decay - WORLDBOOK_CONSTANTS["EPSILON"]))

        # 6) 落值
        a_new = a_init + reward_u + reward_m - decay
        a_new = max(0.0, min(max_score, a_new))

        state.activation = a_new
        state.user_silence = us_new
        state.model_silence = ms_new
        state.recent_user_hits = recent
        return state

    # ---- 取活跃条目 ----
    def top_active(self, states: Dict[str, EntryState], entries: List[Entry], limit: int = None) -> List[Entry]:
        """
        取 Active 条目（activation >= promptThreshold），按 activation 降序，默认截 MAX_ACTIVE。

        对齐 worldbook.ts:588-609 的业务层：只收 enabled 且 !permanent 的条目，
        priority 仅作 tiebreaker（这里保持与通用层一致：只按 activation 排，
        priority tiebreak 由 worldbook 业务层在接线批次追加）。
        """
        if limit is None:
            limit = WORLDBOOK_CONSTANTS["MAX_ACTIVE"]
        threshold = self.params["promptThreshold"]

        picked: List[Tuple[float, Entry]] = []
        for e in entries:
            if not e.enabled or e.permanent:
                continue
            st = states.get(e.id)
            if st is None:
                continue
            if derive_state_at(st.activation, threshold) == STATES["ACTIVE"]:
                picked.append((st.activation, e))
        picked.sort(key=lambda pair: pair[0], reverse=True)
        return [e for _, e in picked[:limit]]


# ========== 状态持久化（手机端新增；桌面端为 no-op seam）==========
# ⚠ 桌面端 worldbook.ts:643-652 的 loadState/saveState 是 v1 no-op seam——只留
#   `// TODO v1.1: ...` 的空实现；worldbook.ts:353 注释亦写「v1 持久化 seam：传了
#   也暂时只 load/save 空实现，重启回 0」。即桌面端**并未真正持久化**。
# ⚠ 本模块的 load_state/save_state 是手机端新增的**真实实现**（atomic write 见下），
#   桌面端无对应函数可照搬，语义以本模块为准。
# ⚠ 接线批次必须显式调用 save_state(path, states, turn) 才能落盘；否则进程重启后
#   所有 activation 回冷态（等同桌面端现状），DMAE 记忆丢失。
def load_state(path) -> Tuple[int, Dict[str, EntryState]]:
    """
    读状态文件，返回 (turn, {id: EntryState})。

    ⚠ 文件不存在 / 解析失败 / 结构不符：一律返回 (0, {})，不抛异常——状态文件坏了
      只意味着「从冷态重来」，不该阻断启动。兼容 snake_case 与 camelCase 两种键名。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return 0, {}
    except (OSError, ValueError):
        return 0, {}

    if not isinstance(data, dict):
        return 0, {}

    try:
        turn = int(data.get("turn", 0) or 0)
    except (TypeError, ValueError):
        turn = 0

    states: Dict[str, EntryState] = {}
    raw = data.get("entries", {})
    if isinstance(raw, dict):
        for eid, sd in raw.items():
            if not isinstance(sd, dict):
                continue
            try:
                hits_raw = sd.get("recent_user_hits", sd.get("recentUserHits", [])) or []
                states[str(eid)] = EntryState(
                    activation=float(sd.get("activation", 0.0) or 0.0),
                    user_silence=int(sd.get("user_silence", sd.get("userSilence", 0)) or 0),
                    model_silence=int(sd.get("model_silence", sd.get("modelSilence", 0)) or 0),
                    recent_user_hits=[int(x) for x in hits_raw],
                )
            except (TypeError, ValueError):
                continue  # 单条坏了就丢这一条，不拖垮整份状态
    return turn, states


def save_state(path, states: Dict[str, EntryState], turn: int = 0) -> None:
    """
    原子写状态文件：同目录临时文件 → os.replace。

    ⚠ 临时文件必须与目标**同目录**：跨卷 os.replace 会退化成非原子拷贝，甚至直接失败。
    JSON 结构：{"version":1, "turn":N, "entries":{id: {...}}}；只存状态不存正文。
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    payload = {
        "version": STATE_SCHEMA_VERSION,
        "turn": int(turn),
        "entries": {},
    }
    for eid, st in states.items():
        payload["entries"][eid] = {
            "activation": st.activation,
            "user_silence": st.user_silence,
            "model_silence": st.model_silence,
            "recent_user_hits": list(st.recent_user_hits),
        }

    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ========== 注入拼装 ==========
def match_keywords(text: str, entry: Entry) -> bool:
    """
    命中判定：大小写不敏感的子串匹配。

    ⚠ 中文没有词边界，只能用子串（worldbook.ts:245 用 `text.includes(kw)` 同款语义）。
      这也意味着短词可能被更长的词包含而误命中，属于已知代价。
    """
    if not text:
        return False
    low = text.lower()
    for kw in entry.keywords:
        if kw and kw.lower() in low:
            return True
    return False


def build_injection(
    user_text: str,
    entries: List[Entry],
    states: Dict[str, EntryState],
    max_chars: int = 2000,
    threshold: Optional[float] = None,
) -> str:
    """
    拼注入文本：INJECTION_HEADER + INJECTION_PREAMBLE + 选中的条目正文。

    选条规则（与桌面端源码核准一致，核心：与「本轮是否命中」无关）：
      注入集合 = 「enabled 且 permanent」∪「enabled 且非 permanent 且 activation >= threshold」
      1) 常驻：enabled 且 permanent → 无条件注入，不论用户说了什么、不论 activation
         （_ref/src/main/rag/worldbook.ts:612-617 `getPermanentEntries`）
      2) 活跃：enabled 且非 permanent，且 activation >= threshold（Active 态）
         （worldbook.ts:332-344 `DmaeManager.getActiveEntries`；业务层 588-598）
      3) 排序：permanent 在前按 priority 降序；活跃按 activation 降序、priority 降序 tiebreak，
         截 MAX_ACTIVE（worldbook.ts:593-598）

    ⚠ 「本轮命中（match_keywords）」不参与注入选取——它只在 DMAE 里抬 activation
      （worldbook.ts:232-343 `updateActivation` 用 userText 抬分）。是否注入完全由状态阈值
      决定：调用链 index.ts:893 `updateWorldbookActivation(userInput, "")` → 895
      `getActiveWorldbookEntries()`（worldbook 内部只做阈值门控，不看本轮命中）。
      user_text 形参仅为签名兼容保留，本实现不再读取它。

    三态边界（worldbook.ts:161-167 deriveState 核准）：
      activation <= 0 → Archived；activation >= threshold → Active；其余 → Dormant。
      （threshold = promptThreshold，默认 30；"<=0" 是 Archived 而非 ">=Archived 线"）

    ⚠ Dormant / Archived 不入选，哪怕这轮被命中。
    ⚠ 一条都没选中时返回空字符串（不是只带 header）——调用方据此决定「不注入」。
    ⚠ max_chars 封顶：按顺序整条保留，绝不截半句；被截断时末尾追加省略提示。
      连第一条都放不下（或无省略提示的容身之处）时，宁可退化为更少的内容，也绝不超长。
    """
    _ = user_text  # 形参仅为签名兼容；桌面端注入不依赖本轮命中（见 docstring）

    # 有效阈值：默认取 DEFAULT_DMAE_PARAMS（本函数无 manager 上下文），
    # 调用方若改过 promptThreshold，应显式传入 threshold 对齐（接线批次的注意点）。
    if threshold is None:
        threshold = DEFAULT_DMAE_PARAMS["promptThreshold"]

    # ---- 1) 常驻（enabled 且 permanent，按 priority 降序；worldbook.ts:612-617 同款） ----
    permanent = [e for e in entries if e.enabled and e.permanent]
    permanent.sort(key=lambda e: e.priority, reverse=True)

    # ---- 2) 活跃（enabled 且非 permanent，activation >= threshold；与命中无关） ----
    active: List[Tuple[float, int, Entry]] = []
    for e in entries:
        if not e.enabled or e.permanent:
            continue
        st = states.get(e.id)
        if st is None or st.activation < threshold:
            continue
        active.append((st.activation, e.priority, e))
    # activation 降序；同分时 priority 降序 tiebreak（worldbook.ts:593-597）
    active.sort(key=lambda t: (t[0], t[1]), reverse=True)
    active = active[:WORLDBOOK_CONSTANTS["MAX_ACTIVE"]]

    # ---- 3) 合并成有序正文块（permanent 在前，已被 permanent 收录的 Active 不重复） ----
    seen_ids = set()
    bodies: List[str] = []
    for e in permanent:
        if e.id in seen_ids:
            continue
        seen_ids.add(e.id)
        bodies.append(_format_entry(e))
    for _, _, e in active:
        if e.id in seen_ids:
            continue
        seen_ids.add(e.id)
        bodies.append(_format_entry(e))

    if not bodies:
        return ""

    # ---- 4) 封顶拼装 ----
    sep = "\n\n"
    base = INJECTION_HEADER + "\n" + INJECTION_PREAMBLE
    parts = [base]
    used = len(base)
    selected = 0
    truncated = False

    for body in bodies:
        need = len(sep) + len(body)
        if used + need <= max_chars:
            parts.append(body)
            used += need
            selected += 1
        else:
            truncated = True
            break

    if selected == 0:
        return ""  # 一条都塞不下 → 视为没命中，宁可不注入也不超长

    text = sep.join(parts)
    if truncated and used + len(sep) + len(INJECTION_OMIT_NOTE) <= max_chars:
        text = text + sep + INJECTION_OMIT_NOTE
    return text


def _format_entry(entry: Entry) -> str:
    """
    单条正文包装。桌面端 worldbook.ts:604-608 是 `【title】\\n content`，
    这里直接沿用（title 取原样、不再从 id 反解，避免把 id 去重后缀 `#2` 也带进来）。
    """
    title = entry.title.strip() if entry.title else entry.id
    return "【%s】\n%s" % (title, entry.content)


# ========== 薄配置读取层（为「人设声明文件」等第二配置源预留） ==========
# 语义与 cyrene_web.py 的 vision_cfg 同构：只读一个全局 dict 的一个键，
# 未灌注 / 键缺失 / 类型不对一律回落 fallback，绝不抛。刻意做成独立函数而不是
# 到处直接读全局 SETTINGS，是为了下一步把「人设声明文件」接成第二个配置源时
# 只改这一处、引擎其余部分（DmaeManager / build_injection）完全无需感知。
MEMORY_CFG: Optional[Dict[str, Any]] = None


def set_memory_cfg(cfg: Optional[Dict[str, Any]]) -> None:
    """由接线方在 load_settings 之后灌注 memory 段（传 None 即清空回落）。"""
    global MEMORY_CFG
    MEMORY_CFG = cfg if isinstance(cfg, dict) else None


# ========== 词法召回（语义召回的降级实现） ==========
# 桌面端靠 embedding + BM25 + Cross-Encoder 做语义召回；手机端不装依赖、不加载
# 模型，这一层退化成纯词法：
#   - 分词：CJK 逐字 + 相邻 2-gram（中文没有词边界，2-gram 能抓住「翁法罗斯」
#     这类跨字搭配），英文数字按词切并小写
#   - 打分：BM25（k1=1.2 / b=0.75），语料统计随条目集走
#   - 精排：粗筛结果交给现有对话模型挑（在接线侧实现），失败回退词法序
# 同义改写、跨语言召回确实抓不到 —— 这是没有 embedding 的必然代价。将来接上
# 云端 embedding 时只需替换本段，精排与注入都不用动。
_BM25_K1 = 1.2
_BM25_B = 0.75
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD_RE = re.compile(r"[a-z0-9_]+")
# 中文高频虚词。这些字几乎每条语料都出现，BM25 的 IDF 能压低它们的权重、
# 却压不到 0 —— 留着会让「随便查一句都命中一堆条目」。只在**单字**层面过滤，
# 2-gram 不动，否则会误伤「存在的」这类正常搭配。
_CJK_STOP_CHARS = frozenset(
    "的了是在和与及或不就都而其为以于这那有无我你他她它们个把被让给对从到"
    "会能要很太还又再只更等后前里外时候地得着过吗呢吧啊呀哦嗯么什怎样些"
)


def tokenize(text: str) -> List[str]:
    """切检索用 token：CJK 逐字 + 相邻 2-gram；英文数字按词。"""
    s = (text or "").lower()
    tokens: List[str] = []
    for seg in _CJK_RUN_RE.findall(s):
        # 逐字（滤掉高频虚词）
        tokens.extend(ch for ch in seg if ch not in _CJK_STOP_CHARS)
        for i in range(len(seg) - 1):           # 相邻 2-gram
            tokens.append(seg[i:i + 2])
    tokens.extend(_WORD_RE.findall(s))
    return tokens


def bm25_scores(query_tokens: List[str], docs_tokens: List[List[str]]) -> List[float]:
    """一批已分词文档的 BM25 分。k1=1.2 / b=0.75（与桌面端 text-ranking 同值）。"""
    n = len(docs_tokens)
    if n == 0 or not query_tokens:
        return [0.0] * n
    lens = [len(d) for d in docs_tokens]
    avgdl = (sum(lens) / n) or 1.0
    q_uniq = set(query_tokens)
    # 文档频次：只为 query 里出现过的词统计，省一轮全表扫描
    df: Dict[str, int] = {t: 0 for t in q_uniq}
    for d in docs_tokens:
        for t in set(d) & q_uniq:
            df[t] += 1
    out: List[float] = []
    for d in docs_tokens:
        tf: Dict[str, int] = {}
        for t in d:
            if t in q_uniq:
                tf[t] = tf.get(t, 0) + 1
        dl = len(d) or 1
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (f * (_BM25_K1 + 1)) / (
                f + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl))
        out.append(s)
    return out


def lexical_recall(query: str, entries: List[Entry], top_k: int = 20
                   ) -> List[Tuple[float, Entry]]:
    """词法粗筛：返回 [(score, entry), ...] 降序，只保留分 > 0 的。

    纯本地计算，不碰网络、不碰 LLM —— 精排交给调用方。
    """
    if not entries:
        return []
    q = tokenize(query)
    if not q:
        return []
    docs: List[List[str]] = []
    for e in entries:
        # 检索面 = 标题 + 触发词 + 正文前 600 字。正文整段进 token 会让长条目
        # 压过短条目（BM25 的长度归一化只能缓解，不能抵掉），而条目开头
        # 通常就是结论句。
        blob = " ".join([
            getattr(e, "title", "") or "",
            " ".join(getattr(e, "keywords", None) or []),
            (getattr(e, "content", "") or "")[:600],
        ])
        docs.append(tokenize(blob))
    scores = bm25_scores(q, docs)
    ranked = sorted(zip(scores, entries), key=lambda x: x[0], reverse=True)
    return [(s, e) for s, e in ranked if s > 0][:max(1, int(top_k))]


# ========== 记忆策略（人设声明） ==========
# 人设文件 prompts/memory_policy.md 里可以声明三件事：
#   在意的事 / 不该记的 / 时间感
# 这一层让「人设」能左右记忆的行为 —— 她自己说该记什么、忘得慢还是快。
#
# ⚠ 第一期只落地两件：时间感（换算成 DMAE 参数的倍率）与不该记的（条目不进注入）。
#   「在意的事」先解析出来存着 —— 它真正的用在二期的抽取层（决定什么值得写下来）。
_POLICY_SECTIONS = ("在意的事", "不该记的", "时间感")
_POLICY_WAKE_SCALE = {"加重": 1.6, "保持": 1.0, "减弱": 0.6}
_POLICY_DECAY_SCALE = {"慢": 0.6, "正常": 1.0, "快": 1.6}
_POLICY_WAKE_KEYS = ("久别重逢", "重逢")
_POLICY_DECAY_KEYS = ("消散", "衰减")


def parse_memory_policy(text: str) -> Dict[str, Any]:
    """解析人设里的记忆策略声明。

    格式与世界书同源（`## 小节` + `- 键: 值`），但不带触发词那套元数据：
      ## 在意的事   → 一条条列出来（二期抽取层用）
      ## 不该记的   → 一条条列出来（硬过滤：命中的条目不进注入）
      ## 时间感     → `- 久别重逢: 加重` / `- 消散: 慢`

    认不出的键值一律忽略 —— 策略是软声明，写错一个字不该让记忆整块罢工。
    """
    out: Dict[str, Any] = {
        "loaded": False, "care": [], "avoid": [],
        "wakeScale": 1.0, "decayScale": 1.0, "errors": [],
    }
    if not text:
        return out
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">") or line.startswith("---"):
            continue
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            section = title if title in _POLICY_SECTIONS else None
            if section:
                out["loaded"] = True
            continue
        if section is None:
            continue
        item = line.lstrip("-*•").strip()
        if not item:
            continue
        if section == "时间感":
            key, val = _split_kv(line)
            if not key or val is None:
                continue
            key, val = key.strip(), val.strip()
            if key in _POLICY_WAKE_KEYS:
                out["wakeScale"] = _POLICY_WAKE_SCALE.get(val, out["wakeScale"])
            elif key in _POLICY_DECAY_KEYS:
                out["decayScale"] = _POLICY_DECAY_SCALE.get(val, out["decayScale"])
            else:
                out["errors"].append("时间感里认不出的键: %s" % key)
            continue
        if section == "在意的事":
            out["care"].append(item)
        elif section == "不该记的":
            # 一行里可以用逗号分隔多个词，与世界书那套一致
            out["avoid"].extend(_split_list(item))
    out["avoid"] = [w for w in out["avoid"] if w]
    return out


def filter_entries(entries: List[Entry], avoid: Optional[List[str]]) -> List[Entry]:
    """按「不该记的」清单筛掉条目：标题 / 触发词 / 正文任一命中即排除。

    清单为空时原样返回，走零开销路径。
    """
    if not avoid:
        return list(entries)
    keep: List[Entry] = []
    for e in entries:
        blob = " ".join([
            getattr(e, "title", "") or "",
            " ".join(getattr(e, "keywords", None) or []),
            getattr(e, "content", "") or "",
        ])
        if any(w in blob for w in avoid):
            continue
        keep.append(e)
    return keep


def get_memory_cfg(key: str, fallback: Any = None) -> Any:
    """读单个记忆配置项。MEMORY_CFG 未灌注时回落 fallback（与 vision_cfg 同构）。"""
    try:
        return MEMORY_CFG.get(key, fallback) if isinstance(MEMORY_CFG, dict) else fallback
    except AttributeError:
        return fallback
