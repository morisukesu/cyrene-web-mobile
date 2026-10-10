#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
昔涟 · 手机端记忆系统二期「写入链路」—— 抽取器 / 结构化校验 / 写入门控 / 节流调度。

对应桌面端源码（src/main/memory/，报告 `_reports/l2-prompts.md` 与 `_ref/` 副本）：
  memory-judge.ts      系统提示词逐字原文、postFilterCandidates、hasUnsupportedAbsolute
  memory-schemas.ts    解析侧校验 parseMemoryCandidate / parseExtractedEntity、slug 规则
  memory-manager.ts    写入门控 shouldSkipCandidate / canWriteCoreProfile、L1 字段判定
  memory-scheduler.ts  节流调度（6 / 8 / 5 / 20 / 50）与「judge 只喂最近 8 轮」
  memory-types.ts:11-17 L0_FIELD_DESCRIPTIONS（5 行，逐字）

移植边界（为什么只搬这些）：
  只搬纯算法与提示词拼装，**不碰网络**。桌面端调用 LLM 走 `invokeMemoryStructuredOutput`
  那一整条（provider 配置、超时、token 上限、重试）全部剥掉：本模块的 judge 只认一个
  调用方注入的 `call_llm(messages) -> str` 回调。这样冒烟测试就能塞一个「假端点」返回
  写死的字符串，全程离线、零依赖；真接线时由主 Agent 注入真实回调。

⚠ 三条贯穿全模块的约定，别踩：
 1) **提示词逐字照抄** `_reports/l2-prompts.md` §2.1。报告里那段 ``` 围栏代码块就是
    `systemPrompt.join("\\n")` 的展开结果（含 `[L0 字段块]` 占位行）；本模块把它作为
    字面量常量保存，运行期只做两处替换（L0 字段块、可选「在意的事」小节）。**不要
    顺手重写、翻译或精简**——提示词一改，抽取行为就不一样了。
    ⚠ 一字之差说明：报告代码块与 `_ref/memory-judge.ts` 的 source array 差一处空行
      （TS 在 `buildL0FieldPrompt()` 之后多一个空字符串元素）。本模块以**报告**为准
      （任务书要求「提示词一律以报告为准」）。
 2) **store 键名一律保持 camelCase**（layer / sourceQuote / forbiddenOverclaims …）。
    这是桌面端 JSON 与 `cyrene_l2store` 的共同口径，改成 snake_case 会两边都对不上。
 3) 候选/实体全部用**普通 dict** 表示，不做 dataclass —— 为的是能和 store 里的 dict
    直接互转，也和 `cyrene_l2store` 的返回风格一致。

⚠ 与源码的有意偏差（都在函数注释里再标一次，不藏）：
 1) 单条校验失败只丢那一条（任务书硬要求）；`memory-schemas.ts` 原实现是抛错后整批失败。
 2) `evidenceQuotes` 非数组即丢弃整条候选 —— 依报告 §3.1「非数组即抛错」的口径；
    源码实际更宽松（`if (Array.isArray(...))`，不合法就忽略该字段）。
 3) L2 去重只按 `content` **精确字符串**比对 —— 源码走 RAG 向量相似度，手机端无索引，
    降级为字面去重，见 `write_candidates`。
 4) slug 长度按 Python code point 计（源码 `String.length` 数 UTF-16 code unit）；
    含非 BMP 字符（emoji 等）时会有 1~2 字差异 —— 与 `cyrene_l2store` 同款已知偏差。
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 运行时目录补进搜索路径：本机 python 是嵌入式发行版（python/python312._pth），
# 脚本目录不进 sys.path，直接 import 同目录模块会静默失败。与 cyrene_web.py 同款处理。
_RUNTIME_DIR = str(Path(__file__).resolve().parent)
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

import cyrene_l2store as L          # noqa: E402  （P1 产物：只复用，不改）
import cyrene_memory as _mem        # noqa: E402  （一期产物：只复用 parse_memory_policy）

__all__ = [
    # 常量
    "VALID_LAYERS", "VALID_ENTITY_TYPES", "SLUG_MAX_LENGTH", "ABSOLUTE_TERMS",
    "L0_FIELD_DESCRIPTIONS", "CORRECTION_INTENT_KEYWORDS", "L1_FIELD_WHITELIST",
    "JUDGE_ERRORS",
    # 提示词
    "build_l0_field_prompt", "build_system_prompt", "build_user_prompt",
    "load_policy_care",
    # 结构化校验 / 后处理
    "parse_candidate", "parse_entity", "parse_judge_result",
    "has_unsupported_absolute", "post_filter_candidates",
    # 写入门控
    "should_skip_candidate", "can_write_core_profile",
    "has_correction_intent", "guess_l1_field", "resolve_l1_field",
    "write_candidates",
    # 抽取器 / 调度
    "MemoryJudge", "MemoryScheduler",
]


# ==========================================================================
# 常量（数值一律来自报告 §4，勿凭「看着合理」改动）
# ==========================================================================

# 报告 §4 #1~#6：调度节流值（默认值）
JUDGE_INTERVAL = 6            # 每 6 轮 judge
JUDGE_CONTEXT_TURNS = 8       # judge 只看最近 8 轮
RESOLVE_INTERVAL = 5          # 每 5 轮 resolver
COMPRESS_INTERVAL = 20        # 每 20 轮压缩 + 回顾
DECAY_INTERVAL = 50           # 每 50 轮 L2 权重衰减

# 报告 §4 #22~#24：slug / sourceQuote 上限
SLUG_MAX_LENGTH = 20
SOURCE_QUOTE_MAX_LENGTH = 500

# memory-schemas.ts `SLUG_ALLOWED_PATTERN`：中文（含扩展 A 区 \u3400-\u9fa5）+ 英文字母 +
# 数字 + 下划线 + 连字符。⚠ 这个字符类天然把空格/标点/引号/emoji 全部挡在外面，所以源码里
# 那条 `\p{Extended_Pictographic}` 检查在 Python 侧**被字符类覆盖**（Python `re` 也不支持
# \p{...}），此处不再单独实现，行为等价。
_SLUG_ALLOWED_RE = re.compile(r"^[\u3400-\u9fa5A-Za-z0-9_-]+$")

VALID_LAYERS = frozenset(("L0", "L1", "L2"))
VALID_ENTITY_TYPES = frozenset(("person", "place", "concept", "preference", "organization"))
_VALID_IMPORTANCE = frozenset(("low", "medium", "high"))
_VALID_STABILITY = frozenset(("one_off", "situational", "stable"))
_VALID_CERTAINTY = frozenset(("explicit", "inferred", "uncertain"))
_VALID_ATTRIBUTION = frozenset(("user_explicit", "assistant_inferred", "mixed"))

# memory-judge.ts:6（绝对化词黑名单，逐字，8 个）
ABSOLUTE_TERMS: Tuple[str, ...] = ("只", "永远", "从不", "一定", "完全", "绝对", "以后都", "不再")

# 复用 P1 已搬好的常量，避免两处各写一份走样
CORRECTION_INTENT_KEYWORDS: Tuple[str, ...] = L.CORRECTION_INTENT_KEYWORDS
L1_FIELD_WHITELIST: Tuple[str, ...] = L.L1_FIELD_WHITELIST

# memory-types.ts:11-17 —— L0_FIELD_DESCRIPTIONS（5 项，顺序即 `Object.entries` 顺序）。
# ⚠ `nickname` 不在这个表里，所以它**不进** L0 字段块（尽管默认 l0 里有这个键）。
L0_FIELD_DESCRIPTIONS: Dict[str, str] = {
    "preferredName": '用户希望被如何称呼、叫什么名字、昵称。例如："叫我P宝""我叫Playa""以后喊我宝宝"',
    "occupation": '用户的职业、身份、工作。例如："我是前端工程师""我在做设计"',
    "longTermInterests": '用户的长期兴趣爱好（稳定的，不是临时的）。例如："我一直喜欢画画""我从小学钢琴"',
    "language": '用户常用的语言或地区习惯。例如："我习惯说中文""我是广东人"',
    "permanentNote": '其他不属于以上四类的稳定个人信息。例如："我有一只猫""我住在上海"',
}

# 模块级「最近一次失败原因」环形缓冲。判据失败时塞一行，供接线时排查；冒烟直接查它。
JUDGE_ERRORS: List[str] = []
_JUDGE_ERRORS_MAX = 20


def _note_error(msg: str) -> None:
    """记一行失败原因（不抛异常）。只保留最近若干条，避免长跑无限增长。"""
    JUDGE_ERRORS.append(str(msg))
    if len(JUDGE_ERRORS) > _JUDGE_ERRORS_MAX:
        del JUDGE_ERRORS[:-_JUDGE_ERRORS_MAX]


# ==========================================================================
# 提示词（逐字照抄 `_reports/l2-prompts.md` §2.1）
# ==========================================================================

def build_l0_field_prompt() -> str:
    """展开 `memory-judge.ts` 的 `buildL0FieldPrompt()`：5 行 `  · 字段：说明`。"""
    return "\n".join("  · %s：%s" % (field, desc) for field, desc in L0_FIELD_DESCRIPTIONS.items())


# 报告 §2.1 的代码块，去掉首尾各一个空行；`[L0 字段块]` 那行保留为占位，运行期替换。
_L0_PLACEHOLDER = "[L0 字段块] ← 由 buildL0FieldPrompt() 运行时生成"

_JUDGE_SYSTEM_PROMPT_LITERAL = """\
你是一个保守的记忆候选提取器，不是事实裁判，也不是用户画像改写器。
你的目标是少记错，不是多记住。

你只能提取用户明确表达、且未来确实有帮助的信息候选。
禁止把推断写成确定事实；禁止把一次性状态写成长期偏好；禁止为了输出而输出。
如果最近这些对话没有值得记的内容，必须返回 {"candidates":[]}。

PMRS 层级定义：
- 画像 (L0)：用户稳定身份信息或核心画像。只有 certainty=explicit 且 attribution=user_explicit 才允许进入画像。
  识别到画像信息时，必须同时在 field 字段里指定要写入哪个格子。
  可用的 field 值如下（只能用这些，不能自己发明）：
[L0 字段块] ← 由 buildL0FieldPrompt() 运行时生成
  重要：field 的值必须严格是上方列出的英文字段名，
  例如 preferredName、occupation，
  不能用 nickname、name、job 等其他词。
- 近况 (L1)：用户近期目标或阶段性偏好，只能写近期状态，不要写成长期偏好。
  识别到近况信息时，必须在 field 字段指定写入哪个格子，可用值：recentGoals / recentPreferences / currentProject。
- 片段 (L2)：具体事件、经历、局部偏好、情绪背景、待观察信息。

判断原则：
- 宁可漏记，不要误记
- 纯日常问候、闲聊、情绪发泄（无信息量）→ 返回 {"candidates":[]}
- 必须是用户主动表达的信息，不是 AI 说的
- summary 必须忠于用户原话和上下文，不要自行推广范围
- 如果只是 AI 的建议、安慰、总结、推断，不要写成用户事实
- 不要把「这次」「刚刚」「这个话题里」变成长期偏好
- 不要自动使用绝对化表达：只、永远、从不、一定、完全、绝对、以后都、不再，除非用户原话明确说过这些词
- 如果 summary 中存在可能过度概括的词，必须写入 forbiddenOverclaims；有 forbiddenOverclaims 时 shouldWrite 必须是 false

重要格式规则：
- summary 和 evidenceQuotes 字段的值里，禁止出现英文双引号 "
- 如果内容里有引号，统一用中文引号「」替代，例如：用户希望被称为「宝宝」
- 输出必须是顶层 JSON 对象，顶层字段为 candidates 和 entities
- candidates 的值必须是 JSON 数组

实体抽取（与候选一起输出，复用本次调用，不额外开销）：
- 只抽用户明确提到的、有指代价值的命名实体（人物名/地名/机构名/具体偏好对象/具体概念）
- 实体类型只能是：person（人物）/ place（地点）/ concept（概念）/ preference（偏好）/ organization（组织）
- 禁止抽取聊天碎片：标点、引号、emoji、语气词、单字、代词、感叹词、对话子串
- 如果只是 AI 提到的、或用户随口一带没有指代价值的，不要抽
- aliases 字段：该实体的其他叫法（可选，没有就省略）
- 没有值得记录的实体时，entities 返回空数组 []

L2 slug 抽取（与候选一起输出，复用本次调用，不额外开销）：
- L2 候选必须输出 slug 字段：精炼的记忆标题，将作为 Obsidian 文件名与双链锚点
- 规则：≤20 字；只能含中文/英文字母/数字/下划线/连字符；禁止标点、引号、空格、emoji
- slug 应高度概括本条记忆的主题，不要直接复用 summary 全文
- 示例：用户说喜欢吃香菇 → slug="喜欢香菇"；和小张约下周吃饭 → slug="和小张约饭"；React Chat 窗口迁移 → slug="ReactChat迁移"
- L0 / L1 候选不要输出 slug 字段

L2 sourceQuote 抽取（与候选一起输出，复用本次调用，不额外开销）：
- L2 候选必须输出 sourceQuote 字段：从最近对话里挑出最有信息量的一段原文片段（用户或对话原话）
- 目的：L2 是浓缩结论，会丢失字面信息（专有名词/数字/代码片段）；sourceQuote 保留「用户当时说的原话」，召回时让后续模型看到字面证据
- 规则：软上限 500 字；不要整段照抄对话；优先挑含专有名词、数字、代码、关键名词的句子；允许标点、空格、emoji（因为是原文）
- 不要把 summary 复制进 sourceQuote；sourceQuote 应是原话片段，summary 是你的浓缩结论
- 示例：用户说「我用 React 18.2 做的前端，部署在 vercel 上」→ sourceQuote="我用 React 18.2 做的前端，部署在 vercel 上"
- L0 / L1 候选不要输出 sourceQuote 字段

输出结构：
{
  "candidates": [
    {
      "layer": "L0",
      "field": "preferredName",
      "summary": "保守、可追溯的候选摘要",
      "slug": "L2精炼标题",
      "sourceQuote": "L2原文对话片段",
      "content": "与 summary 相同",
      "confidence": 0.9,
      "triggerText": "用户原话短引文",
      "importance": "low|medium|high",
      "stability": "one_off|situational|stable",
      "certainty": "explicit|inferred|uncertain",
      "attribution": "user_explicit|assistant_inferred|mixed",
      "evidenceQuotes": ["用户原话短引文，必须来自用户"],
      "contextSummary": "最近多轮上下文概括，不超过80字",
      "shouldWrite": true,
      "reason": "为什么值得记，或为什么不写",
      "forbiddenOverclaims": []
    }
  ],
  "entities": [
    {"name": "小张", "type": "person", "aliases": ["张三"]}
  ]
}

片段不需要 field。近况必须指定 field（recentGoals / recentPreferences / currentProject）。
L2 片段必须输出 slug 字段（精炼标题，≤20 字，仅中文/字母/数字/_/-），如 "slug": "喜欢香菇"。
L2 片段必须输出 sourceQuote 字段（原文对话片段，≤500 字，允许标点/空格/emoji），如 "sourceQuote": "我用 React 18.2 做的前端"。
inferred / uncertain 不允许进入画像；如果还值得保留，只能放片段，或者 shouldWrite=false。
没有值得记录的信息时，输出：{"candidates":[],"entities":[]}
summary 和 evidenceQuotes 里禁止出现英文双引号，用「」替代。
实体 name 也禁止包含英文双引号、标点、emoji。
slug 禁止包含标点、引号、空格、emoji；只能含中文/字母/数字/下划线/连字符。"""

# 「在意的事」小节的插入锚点：判断原则段最后一条 bullet（含行尾换行）。
_CARE_ANCHOR = (
    "- 如果 summary 中存在可能过度概括的词，必须写入 forbiddenOverclaims；"
    "有 forbiddenOverclaims 时 shouldWrite 必须是 false\n"
)


def build_system_prompt(policy_care: Optional[Sequence[str]] = None) -> str:
    """
    拼 judge 的系统提示词。

    位置（任务书 E）：在「判断原则」段之后追加一小节。策略为空/缺省时**不留空节**，
    返回逐字照抄的原提示词。
    """
    prompt = _JUDGE_SYSTEM_PROMPT_LITERAL.replace(_L0_PLACEHOLDER, build_l0_field_prompt())
    care = [str(item).strip() for item in (policy_care or []) if str(item).strip()]
    if care:
        block = "\n她本人额外在意的事（同等信息量时优先记下这些）：\n"
        block += "".join("- %s\n" % item for item in care)
        prompt = prompt.replace(_CARE_ANCHOR, _CARE_ANCHOR + block)
    return prompt


def build_user_prompt(turns: Sequence[Dict[str, Any]], conversation_id: str) -> str:
    """
    judge 的 user prompt（`memory-judge.ts:142-152` 的模板）：

        conversationId: ${conversationId}
        最近对话：
        第 1 轮：
        用户：${userInput}
        AI：${assistantReply}
        <空行>
        第 2 轮：
        …

    轮内 `\\n` 分隔、轮间 `\\n\\n` 分隔（`turns.map(...).join("\\n\\n")`）。
    """
    blocks = []
    for index, turn in enumerate(turns):
        blocks.append("\n".join([
            "第 %d 轮：" % (index + 1),
            "用户：%s" % (turn.get("userInput") or ""),
            "AI：%s" % (turn.get("assistantReply") or ""),
        ]))
    transcript = "\n\n".join(blocks)
    return "\n".join([
        "conversationId: %s" % (conversation_id if conversation_id is not None else ""),
        "最近对话：",
        transcript,
    ])


def _default_policy_path() -> Path:
    """默认人设策略文件：cyrene_mobile/prompts/memory_policy.md。"""
    return Path(__file__).resolve().parent.parent / "prompts" / "memory_policy.md"


def load_policy_care(path: Any = None) -> List[str]:
    """
    读策略文件的「在意的事」清单（复用一期 `cyrene_memory.parse_memory_policy`）。

    ⚠ 文件读不到 / 解析失败一律**安静退场**返回空表（等价于没有这一节），只记一行原因。
    策略是锦上添花，不该成为单点故障。
    """
    policy_path = Path(path) if path is not None else _default_policy_path()
    try:
        if not policy_path.exists():
            return []
        text = policy_path.read_text(encoding="utf-8")
        policy = _mem.parse_memory_policy(text)
        care = policy.get("care") or []
        return [str(item).strip() for item in care if str(item).strip()]
    except Exception as exc:                                     # pragma: no cover
        _note_error("策略读取失败（等同无此节）: %s" % exc)
        return []


# ==========================================================================
# 业务后处理（memory-judge.ts postFilterCandidates / hasUnsupportedAbsolute）
# ==========================================================================

def has_unsupported_absolute(summary: str, evidence_quotes: Optional[Sequence[str]]) -> bool:
    """
    命中绝对化词、但用户原话（evidenceQuotes）里也没出现过 → 判为「无据绝对化」。

    ⚠ 判据是 `summary.includes(term) and not any(quote.includes(term))`，逐字对齐源码。
    """
    quotes = evidence_quotes or []
    summary = summary or ""
    for term in ABSOLUTE_TERMS:
        if term in summary and not any(term in (quote or "") for quote in quotes):
            return True
    return False


def post_filter_candidates(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    业务级后处理（memory-judge.ts `postFilterCandidates`），顺序即源码顺序：

      1. `shouldWrite !== true` 一律剔除（注意：源码是 `.filter(shouldWrite === true)`，
         缺字段或 false 都出局）；
      2. L0 必须 `certainty == "explicit"` 且 `attribution == "user_explicit"`；
      3. `forbiddenOverclaims` 非空出局；
      4. summary/content 含无据绝对化词出局。

    ⚠ 第 1 条比写入门控更严：门控只挡 `shouldWrite === false`，这里连「没写 shouldWrite」
      也挡。两层口径不同是源码原样，别「统一」。
    """
    out: List[Dict[str, Any]] = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        if item.get("shouldWrite") is not True:
            continue
        if item.get("layer") == "L0" and not (item.get("certainty") == "explicit" and item.get("attribution") == "user_explicit"):
            continue
        overclaims = item.get("forbiddenOverclaims")
        if overclaims:
            continue
        if has_unsupported_absolute(item.get("summary") or item.get("content") or "", item.get("evidenceQuotes")):
            continue
        out.append(item)
    return out


# ==========================================================================
# 结构化校验（memory-schemas.ts parseMemoryCandidate / parseExtractedEntity）
# ⚠ 单条不过只丢那一条，绝不让整批失败（任务书硬约束）。
# ==========================================================================

def _nonempty_str(value: Any) -> Optional[str]:
    """等价 `requiredString`：非字符串或 trim 后为空 → None（调用方据此丢条目）。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _finite_number(value: Any) -> Optional[float]:
    """等价 `requiredNumber`：bool 不是数（JS 里 typeof true !== "number"），NaN/Inf 也拒。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):   # NaN / Inf
        return None
    return value


def _validate_string_list(value: Any) -> Optional[List[str]]:
    """等价 `stringArray`：非数组 → None；有非字符串成员 → None（整条丢）。"""
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, str):
            return None
    return list(value)


def is_valid_slug(value: Any) -> bool:
    """slug 合法：非空、≤20 字、仅中文/字母/数字/_/-。非法返回 False（字段丢弃，候选保留）。"""
    if not isinstance(value, str):
        return False
    trimmed = value.strip()
    if not trimmed or len(trimmed) > SLUG_MAX_LENGTH:
        return False
    return bool(_SLUG_ALLOWED_RE.match(trimmed))


def is_valid_source_quote(value: Any) -> bool:
    """sourceQuote 合法：非空、trim 后 ≤500 字（允许任意 Unicode，因为是原文）。"""
    if not isinstance(value, str):
        return False
    trimmed = value.strip()
    return 0 < len(trimmed) <= SOURCE_QUOTE_MAX_LENGTH


def parse_candidate(obj: Any) -> Optional[Dict[str, Any]]:
    """
    校验单条候选（`parseMemoryCandidate`）。通过返回干净 dict，否则返回 None。

    必填：layer ∈ {L0,L1,L2}、content、confidence、triggerText（后三者非空/有限数）。
    ⚠ slug / sourceQuote **只对 L2 保留**；L0/L1 即使给了也一律丢掉（字段级丢弃，候选保留）。
    ⚠ evidenceQuotes：非数组即丢弃整条候选（按报告 §3.1 口径；源码实际更宽松，见模块头注）。
    """
    if not isinstance(obj, dict):
        return None
    layer = _nonempty_str(obj.get("layer"))
    if layer not in VALID_LAYERS:
        return None
    content = _nonempty_str(obj.get("content"))
    if content is None:
        return None
    confidence = _finite_number(obj.get("confidence"))
    if confidence is None:
        return None
    trigger_text = _nonempty_str(obj.get("triggerText"))
    if trigger_text is None:
        return None

    result: Dict[str, Any] = {
        "layer": layer,
        "content": content,
        "confidence": confidence,
        "triggerText": trigger_text,
    }
    field = _nonempty_str(obj.get("field"))
    if field is not None:
        result["field"] = field
    summary = _nonempty_str(obj.get("summary"))
    if summary is not None:
        result["summary"] = summary
    if layer == "L2" and is_valid_slug(obj.get("slug")):
        result["slug"] = obj["slug"].strip()
    if layer == "L2" and is_valid_source_quote(obj.get("sourceQuote")):
        result["sourceQuote"] = obj["sourceQuote"].strip()
    if obj.get("importance") in _VALID_IMPORTANCE:
        result["importance"] = obj["importance"]
    if obj.get("stability") in _VALID_STABILITY:
        result["stability"] = obj["stability"]
    if obj.get("certainty") in _VALID_CERTAINTY:
        result["certainty"] = obj["certainty"]
    if obj.get("attribution") in _VALID_ATTRIBUTION:
        result["attribution"] = obj["attribution"]
    # evidenceQuotes：给了就必须是数组（口径见 docstring）；没给则跳过。
    if "evidenceQuotes" in obj:
        quotes = _validate_string_list(obj.get("evidenceQuotes"))
        if quotes is None:
            return None
        result["evidenceQuotes"] = quotes
    if isinstance(obj.get("contextSummary"), str):
        result["contextSummary"] = obj["contextSummary"]
    if isinstance(obj.get("shouldWrite"), bool):
        result["shouldWrite"] = obj["shouldWrite"]
    if isinstance(obj.get("reason"), str):
        result["reason"] = obj["reason"]
    if "forbiddenOverclaims" in obj:
        overclaims = _validate_string_list(obj.get("forbiddenOverclaims"))
        if overclaims is None:
            result["forbiddenOverclaims"] = []          # 空数组是合法值，等同「没有」
        else:
            result["forbiddenOverclaims"] = overclaims
    return result


def parse_entity(obj: Any) -> Optional[Dict[str, Any]]:
    """
    校验单个实体（`parseExtractedEntity`）。通过返回干净 dict，否则 None。

    必填：name、type ∈ {person, place, concept, preference, organization}。
    aliases：trim + 去空 + **去掉等于 name 自身的**（源码 `filter(a => a && a !== name)`）。
    ⚠ aliases 里彼此重复的**不去重**（源码也没去），保持插入序。
    """
    if not isinstance(obj, dict):
        return None
    name = _nonempty_str(obj.get("name"))
    if name is None:
        return None
    entity_type = _nonempty_str(obj.get("type"))
    if entity_type not in VALID_ENTITY_TYPES:
        return None
    result: Dict[str, Any] = {"name": name, "type": entity_type}
    if isinstance(obj.get("aliases"), list):
        raw = _validate_string_list(obj.get("aliases"))
        if raw is None:
            return None                                     # 成员里混了非字符串 → 丢整条
        aliases = [alias.strip() for alias in raw]
        aliases = [alias for alias in aliases if alias and alias != name]
        result["aliases"] = aliases
    return result


def _loads_lenient(text: str) -> Any:
    """先按原样 `json.loads`；失败再尝试剥掉 ```json 围栏。仍失败则抛原异常。"""
    try:
        return json.loads(text)
    except ValueError as first_err:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if match:
            return json.loads(match.group(1).strip())
        raise first_err


def parse_judge_result(raw: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    解析 judge 的原始输出 → (candidates, entities)。**只丢单条，不整批失败**。

    ⚠ 顶层坏掉（空串 / 坏 JSON / 顶层非对象也非数组 / candidates 不是数组）会抛异常，
    由 `MemoryJudge.judge` 兜住；单条坏掉只丢那一条。
    ⚠ 兼容旧形式：顶层直接是数组时视为纯 candidates（源码同款向后兼容）。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        raise ValueError("LLM 输出不是字符串")
    text = raw.strip()
    if not text:
        raise ValueError("LLM 输出为空")
    data = _loads_lenient(text)
    if isinstance(data, list):
        data = {"candidates": data, "entities": []}
    if not isinstance(data, dict):
        raise ValueError("judge 输出顶层必须是 JSON 对象或数组")
    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates 必须是数组")
    candidates: List[Dict[str, Any]] = []
    for item in raw_candidates:
        parsed = parse_candidate(item)
        if parsed is not None:
            candidates.append(parsed)
    entities: List[Dict[str, Any]] = []
    raw_entities = data.get("entities")
    if isinstance(raw_entities, list):
        for item in raw_entities:
            parsed = parse_entity(item)
            if parsed is not None:
                entities.append(parsed)
    return candidates, entities


# ==========================================================================
# A. MemoryJudge（抽取器）
# ==========================================================================

class MemoryJudge:
    """
    记忆候选抽取器。**不联网**：LLM 走注入的 `call_llm(messages) -> str` 回调。

    参数：
      call_llm       必填，`(messages: List[dict]) -> str`；messages 为
                     `[{"role":"system","content":...},{"role":"user","content":...}]`
      policy_care    可选，「在意的事」清单；拼进系统提示（见 build_system_prompt）
      system_prompt  可选，直接指定系统提示（测试用；给了就忽略 policy_care）
    """

    def __init__(
        self,
        call_llm: Callable[[List[Dict[str, str]]], str],
        policy_care: Optional[Sequence[str]] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self._call_llm = call_llm
        self._system_prompt = system_prompt if system_prompt is not None else build_system_prompt(policy_care)

    def build_system_prompt(self) -> str:
        return self._system_prompt

    def build_user_prompt(self, turns: Sequence[Dict[str, Any]], conversation_id: str) -> str:
        return build_user_prompt(turns, conversation_id)

    def judge(self, turns: Sequence[Dict[str, Any]], conversation_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        抽取一轮。**任何失败都不抛异常**：LLM 报错 / 输出坏 JSON / 输出空串，一律返回
        空候选 + 空实体，并在模块里记一行原因（`JUDGE_ERRORS`）。
        """
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": build_user_prompt(turns, conversation_id)},
        ]
        try:
            raw = self._call_llm(messages)
        except Exception as exc:
            _note_error("judge LLM 调用失败: %s" % exc)
            return {"candidates": [], "entities": []}
        try:
            candidates, entities = parse_judge_result(raw)
        except Exception as exc:
            _note_error("judge 输出解析失败: %s" % exc)
            return {"candidates": [], "entities": []}
        return {"candidates": candidates, "entities": entities}


# ==========================================================================
# C. 写入门控（memory-manager.ts）
# ==========================================================================

def should_skip_candidate(c: Dict[str, Any]) -> bool:
    """
    `shouldSkipCandidate`：`shouldWrite === false` 或 `forbiddenOverclaims` 非空 → 跳过。

    ⚠ 严格判 `is False`：缺字段、null、true 都不跳过（与源码 `=== false` 一致）。
    """
    if not isinstance(c, dict):
        return True
    if c.get("shouldWrite") is False:
        return True
    overclaims = c.get("forbiddenOverclaims")
    if isinstance(overclaims, list) and len(overclaims) > 0:
        return True
    return False


def can_write_core_profile(c: Dict[str, Any]) -> bool:
    """`canWriteCoreProfile`：L0 必须 certainty=explicit 且 attribution=user_explicit。"""
    if not isinstance(c, dict):
        return False
    return c.get("certainty") == "explicit" and c.get("attribution") == "user_explicit"


# L1 字段判定直接复用 P1 已搬好的实现（常量与正则都在 cyrene_l2store），避免两处分叉。
def has_correction_intent(text: str) -> bool:
    """是否在纠正记忆（纠正意图关键词表，memory-manager.ts:29）。"""
    return L.has_correction_intent(text)


def guess_l1_field(content: str) -> str:
    """L1 字段兜底推断：目标/想要/计划/打算 → recentGoals；项目/在做/开发/写 → currentProject；否则 recentPreferences。"""
    return L.guess_l1_field(content)


def resolve_l1_field(field: Optional[str], content: str) -> str:
    """白名单内用原值，否则兜底推断。"""
    return L.resolve_l1_field(field, content)


def write_candidates(store: Dict[str, Any], candidates: Sequence[Dict[str, Any]], conversation_id: str) -> Dict[str, Any]:
    """
    按层把候选落库（就地改 store）。**用 cyrene_l2store 的接口**，不自己造轮子：
      L0：先过 `can_write_core_profile`，再 `upsert_l0_field(field, content)`；
      L1：`resolve_l1_field` 定字段后 `replace_l1_field(field, content)`；
      L2：按 `content` 精确去重后 `add_l2(...)`，带上 slug / sourceQuote / triggerText。

    返回写入统计：
      {"L0": n, "L1": n, "L2": n, "written": 总写入, "gated": 门控挡下, "deduped": L2 去重跳过}
    ⚠ 落盘与否由调用方决定（与 l2store 约定一致）：本函数只改内存，接线时记得 write_memory_file。
    """
    stats = {"L0": 0, "L1": 0, "L2": 0, "written": 0, "gated": 0, "deduped": 0}

    # L2 去重表：现存 content 集合（降级为字面比对，见模块头注）。
    existing_contents = set()
    for mem in L.get_all_l2(store):
        if isinstance(mem, dict) and isinstance(mem.get("content"), str):
            existing_contents.add(mem["content"])

    for candidate in candidates or []:
        if should_skip_candidate(candidate):
            stats["gated"] += 1
            continue
        layer = candidate.get("layer")

        if layer == "L0":
            if not can_write_core_profile(candidate):
                stats["gated"] += 1
                continue
            field = candidate.get("field")
            if field not in L0_FIELD_DESCRIPTIONS:
                # 缺字段或幻觉字段名 —— 与源码一致，跳过 L0 自动写核心画像。
                stats["gated"] += 1
                continue
            L.upsert_l0_field(store, field, candidate.get("content"))
            stats["L0"] += 1

        elif layer == "L1":
            field = resolve_l1_field(candidate.get("field"), candidate.get("content") or "")
            L.replace_l1_field(store, field, candidate.get("content"))
            stats["L1"] += 1

        elif layer == "L2":
            content = candidate.get("content")
            if content in existing_contents:
                stats["deduped"] += 1
                continue
            trigger_text = candidate.get("triggerText") or content
            l2_input: Dict[str, Any] = {
                "content": content,
                "triggerText": trigger_text,
                "sourceConversationId": conversation_id or "",
                "embedding": [],
                "isPinned": False,
                "syncStatus": "pending_sync",
            }
            if candidate.get("slug"):
                l2_input["slug"] = candidate["slug"]
            if candidate.get("sourceQuote"):
                l2_input["sourceQuote"] = candidate["sourceQuote"]
            L.add_l2(store, l2_input)
            existing_contents.add(content)
            stats["L2"] += 1

        else:
            # 理论上 parse_candidate 已挡住；防御一层。
            stats["gated"] += 1

    stats["written"] = stats["L0"] + stats["L1"] + stats["L2"]
    return stats


# ==========================================================================
# D. MemoryScheduler（节流调度）
# ==========================================================================

class MemoryScheduler:
    """
    记忆维护的节流调度器。**自己不调 LLM、不碰网络**，只负责「到点了就调哪个注入进来的函数」。

    deps（全部依赖注入）：
      judge_fn(turns, conversation_id)  每 6 轮，喂最近 8 轮对话
      resolve_fn(round_count)           每 5 轮
      compress_fn(round_count)          每 20 轮
      decay_fn(round_count)             每 50 轮
      get_round_count() -> int          读当前轮数（如从 L1.roundCount）
      set_round_count(n)                写回轮数
    可选覆盖节流值：judge_interval / context_turns / resolve_interval / compress_interval / decay_interval。

    ⚠ 每个回调都包在 try/except 里：维护任务失败**不影响主流程**（源码同款容错）。
    """

    def __init__(self, deps: Dict[str, Any]) -> None:
        self.judge_fn = deps.get("judge_fn")
        self.resolve_fn = deps.get("resolve_fn")
        self.compress_fn = deps.get("compress_fn")
        self.decay_fn = deps.get("decay_fn")
        self.get_round_count = deps.get("get_round_count") or (lambda: 0)
        self.set_round_count = deps.get("set_round_count") or (lambda n: None)
        self.judge_interval = deps.get("judge_interval", JUDGE_INTERVAL)
        self.context_turns = deps.get("context_turns", JUDGE_CONTEXT_TURNS)
        self.resolve_interval = deps.get("resolve_interval", RESOLVE_INTERVAL)
        self.compress_interval = deps.get("compress_interval", COMPRESS_INTERVAL)
        self.decay_interval = deps.get("decay_interval", DECAY_INTERVAL)
        # 轮次缓存上限 = context_turns * 2（源码 `MEMORY_JUDGE_CONTEXT_TURNS * 2`）。
        self._recent_turns: List[Dict[str, Any]] = []
        self._next_seq = 0

    def after_turn(self, user_input: str, assistant_reply: str, conversation_id: Optional[str] = None) -> int:
        """
        登记一轮、轮数 +1，然后按周期触发。返回新的轮数。

        ⚠ judge 只喂**最近 context_turns 轮**（默认 8）；喂进去的是 dict 副本，不含内部 seq。
        """
        self._next_seq += 1
        self._recent_turns.append({
            "seq": self._next_seq,
            "userInput": user_input,
            "assistantReply": assistant_reply,
        })
        cap = self.context_turns * 2
        if len(self._recent_turns) > cap:
            self._recent_turns = self._recent_turns[-cap:]

        count = self.get_round_count() + 1
        self.set_round_count(count)

        if self.judge_interval and count % self.judge_interval == 0:
            turns = [
                {"userInput": t["userInput"], "assistantReply": t["assistantReply"]}
                for t in self._recent_turns[-self.context_turns:]
            ]
            self._safe_call(self.judge_fn, turns, conversation_id if conversation_id is not None else "default")

        if self.resolve_interval and count % self.resolve_interval == 0:
            self._safe_call(self.resolve_fn, count)

        if self.compress_interval and count % self.compress_interval == 0:
            self._safe_call(self.compress_fn, count)

        if self.decay_interval and count % self.decay_interval == 0:
            self._safe_call(self.decay_fn, count)

        return count

    @staticmethod
    def _safe_call(fn: Optional[Callable[..., Any]], *args: Any) -> None:
        """回调容错：没注入就跳过；抛异常只记一行，不影响主流程。"""
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as exc:                                 # pragma: no cover
            _note_error("调度回调失败: %s" % exc)
