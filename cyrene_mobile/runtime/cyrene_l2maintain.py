#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
昔涟 · 手机端记忆系统二期「维护链路」（P4）—— 冲突裁决 / 相似压缩 + 画像反思 / 衰减 / 体积护栏。

对应桌面端源码（src/main/memory/，报告 `_reports/l2-prompts.md` 与 `_ref/` 副本）：
  memory-resolver.ts    提示词逐字原文（:81-99 / :102）、判定流程 resolveNextConflict
  memory-compressor.ts  两段提示词（阶段 A :80-99 / 阶段 B :180-209）、聚类阈值 :23-24、
                        confidence 阈值 :229、反思日志 type :134 / :234 / :243
  memory-store.ts       applyResolverResolution :511-601、decayL2Weights :624-652
  memory-schemas.ts     解析侧校验 parseMemoryResolveResult :372-396、
                        parseMemoryReflectionResult（layer 仅 L0/L1）

移植边界（为什么只搬这些）：
  只搬纯算法与提示词拼装，**不碰网络、不加载模型文件、零第三方依赖**。桌面端调用 LLM 走
  `invokeMemoryStructuredOutput` 那一整条（provider 配置、超时、token 上限、重试）全部剥掉：
  本模块只认一个调用方注入的 `llm_fn(messages) -> str` 回调（messages 形如
  `[{"role":"system","content":...},{"role":"user","content":...}]`）。冒烟测试塞一个「假端点」
  返回写死的字符串，全程离线；真接线时由主 Agent 注入真实回调。

⚠ 三条贯穿全模块的约定，别踩：
 1) **提示词逐字照抄** `_reports/l2-prompts.md` §2.2（压缩 A/B）与 §2.3（resolver）。
    report 里 ``` 围栏代码块就是运行期拼出来的字符串。**不要顺手重写、翻译或精简** ——
    提示词一改，模型行为就不一样了。
 2) **store 键名一律保持 camelCase**（sourceL2Id / resolutionType / oldMemoryStatus …）。
    这是桌面端 JSON 与 `cyrene_l2store` 的共同口径，改成 snake_case 会两边都对不上。
 3) 反思日志 type 只有 **三个**：compression / l0_update / l1_update（`memory-types.ts:88`，
    报告 §6）。**没有 l2_update** —— L2 写入不写反思日志。本模块不发明第四种。

⚠ 与源码的有意/必要偏差（都在函数注释里再标一次，不藏）：
 1) resolver 逐条处理**整个队列**（源码 `resolveNextConflict` 每轮只取一条 + 60s 限流）。
    手机端由 `MemoryScheduler` 每 5 轮触发一次，故这里一次把队列跑完；`resolve_once` 返回
    统计三元组而非单条 Result。
 2) 相似聚类用**词法相似度**（CJK bigram + 英文/数字词，Jaccard 型）代替源码的 RAG 向量余弦
    —— 手机端无 embedding 索引。默认阈值 `SIMILARITY_THRESHOLD = 0.85`，但词法口径下近似
    重复的条目也常低于 0.85，故源码阈值在手机端会「几乎不触发」，实际由 `cluster_similar_l2`
    的 `threshold` 参数兜底（percent 风格入参会被换算回 0-1）。这是降级，不是等价实现。
 3) 压缩落库**新建一条总结条目**（`isSummary=True` + `subEntryIds`），簇内其余标 `merged`。
    依据见 `_archive_merged` 的注释；源码走 `commitMemoryCompression` 事务（该文件不在 `_ref/`
    拷贝里），但 compressor 调用点给 deps 的名字是 `createSummary`（`memory-compressor.ts:115`）
    + `archiveSources`（:118），顾名思义就是「新建总结 + 归档来源」。
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# 运行时目录补进搜索路径：本机 python 是嵌入式发行版（python/python312._pth），
# 脚本目录不进 sys.path，直接 import 同目录模块会静默失败。与 cyrene_l2write.py 同款处理。
_RUNTIME_DIR = str(Path(__file__).resolve().parent)
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

import cyrene_l2store as L          # noqa: E402  （P1 产物：只复用，不改）

__all__ = [
    # 常量
    "RESOLVER_SYSTEM_PROMPT", "COMPRESS_SYSTEM_PROMPT", "REFLECT_SYSTEM_PROMPT",
    "RESOLUTION_TYPES", "VALID_MEMORY_STATUS", "REFLECTION_TYPES",
    "SIMILARITY_THRESHOLD", "MIN_GROUP_SIZE", "REFLECT_CONFIDENCE_MIN",
    "L2_MAX_ENTRIES", "ERRORS",
    # resolver
    "build_resolver_user_prompt", "parse_resolution", "MemoryResolver",
    # compressor
    "build_compress_user_prompt", "build_reflect_user_prompt", "parse_reflect_updates",
    "tokenize_similarity", "cluster_similar_l2", "MemoryCompressor",
    # 衰减 / 体积护栏
    "run_decay", "enforce_l2_limit",
]


# ==========================================================================
# 常量（数值一律标注来源，勿凭「看着合理」改动）
# ==========================================================================

# resolver 系统提示词：`memory-resolver.ts:102`，一行，逐字（报告 §2.3）。
RESOLVER_SYSTEM_PROMPT = "你是谨慎的用户记忆冲突 Resolver。你只根据 summary 和 evidence 判断，不要编造事实，只输出 JSON。"

# resolver resolutionType 取值集合：`memory-schemas.ts:56-58`（VALID_RESOLUTION_TYPES），
# 报告 §3.2 :175。**顺序即源码枚举顺序**。
RESOLUTION_TYPES = (
    "unrelated",
    "context_difference",
    "preference_evolution",
    "direct_conflict",
    "uncertain",
)

# L2 status 合法值：`memory-schemas.ts:59`（VALID_MEMORY_STATUS），报告 §3.2 :185-186。
VALID_MEMORY_STATUS = ("active", "aging", "archived", "superseded", "merged")

# 反思日志 type：`memory-types.ts:88`，报告 §6 —— 只有这三个。
REFLECTION_TYPES = ("compression", "l0_update", "l1_update")

# 阶段 A 压缩系统/用户提示词：`memory-compressor.ts:95` 与 :80-90，逐字（报告 §2.2）。
COMPRESS_SYSTEM_PROMPT = "你是一个简洁的记忆总结助手。"
_COMPRESS_USER_HEADER = "\n".join([
    "你是一个记忆总结助手。以下是一组相似的用户记忆条目，请将它们合并成一条简洁的总结。",
    "要求：",
    "- 保留所有关键信息，去重",
    "- 用中文自然语言",
    "- 控制在 100 字以内",
    "- 直接输出总结文本，不要额外解释",
    "",
    "记忆条目：",
])

# 阶段 B 反思系统提示词：`memory-compressor.ts:180-187`，逐字（报告 §2.2）。
REFLECT_SYSTEM_PROMPT = "\n".join([
    "你是一个谨慎的用户画像反思助手。",
    "你只能输出 JSON，不要 Markdown 代码块、不要解释、不要注释。",
    "输出必须是顶层 JSON 对象，唯一的顶层字段为 updates。",
    "updates 是 JSON 数组，每个元素格式：",
    '{ "layer": "L0" 或 "L1", "field": "字段名（可选）", "content": "新的用户画像内容", "confidence": 0.0 到 1.0 }',
    '没有更新时输出 {"updates":[]}。',
])

# 聚类阈值：`memory-compressor.ts:23`（SIMILARITY_THRESHOLD = 0.85）。
SIMILARITY_THRESHOLD = 0.85
# 最小成组条数：`memory-compressor.ts:24`（MIN_GROUP_SIZE = 3）。
MIN_GROUP_SIZE = 3

# 反思落库 confidence 阈值：`memory-compressor.ts:229`（`item.confidence < 0.6` 跳过）。
REFLECT_CONFIDENCE_MIN = 0.6

# ⚠ 手机端自补约束，**源码里没有**：桌面端 L2 无条数上限，手机端存储与内存都紧张，
#    故在 `enforce_l2_limit` 里给一个软上限，超限按 weight 升序归档（见该函数）。
L2_MAX_ENTRIES = 300

# 模块级「最近一次失败原因」环形缓冲（照抄 `cyrene_l2write.JUDGE_ERRORS` + `_note_error`）。
ERRORS: List[str] = []
_ERRORS_MAX = 20


def _note_error(msg: str) -> None:
    """记一行失败原因（不抛异常）。只保留最近若干条，避免长跑无限增长。"""
    ERRORS.append(str(msg))
    if len(ERRORS) > _ERRORS_MAX:
        del ERRORS[:-_ERRORS_MAX]


def _finite_number(value: Any) -> Optional[float]:
    """有限数才认（bool 不算数）；否则 None。与 `cyrene_l2write._finite_number` 同款口径。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        fvalue = float(value)
        if fvalue == fvalue and fvalue not in (float("inf"), float("-inf")):
            return fvalue
    return None


def _loads_lenient(text: str) -> Any:
    """先按原样 `json.loads`；失败再尝试剥掉 ```json 围栏。仍失败则抛原异常。"""
    try:
        return json.loads(text)
    except ValueError as first_err:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if match:
            return json.loads(match.group(1).strip())
        raise first_err

# ==========================================================================
# A. resolver（冲突裁决）
# ==========================================================================

def build_resolver_user_prompt(
    old_memory: Dict[str, Any],
    old_evidence: Sequence[Dict[str, Any]],
    new_memory: Dict[str, Any],
    new_evidence: Sequence[Dict[str, Any]],
    conflict_score: Any,
    scoring_signals: Any,
) -> str:
    """
    resolver 的 user prompt（`memory-resolver.ts:81-99` 模板，报告 §2.3，逐字）。

    evidence 每条展开 3 行（`:78-80`）：`- quote: ...` / `  conversationId: ...` /
    `  sourceStatus: ...`；为空时整段输出 `- none`。
    `scoringSignals` 用 `json.dumps` 序列化（源码 `JSON.stringify(scoringSignals ?? {})`）。
    """
    def evidence_lines(items: Sequence[Dict[str, Any]]) -> str:
        lines = []
        for item in items or []:
            conv = item.get("conversationId")
            conv = "unknown" if conv in (None, "") else conv
            lines.append("\n".join([
                "- quote: %s" % (item.get("quoteSnippet") or ""),
                "  conversationId: %s" % conv,
                "  sourceStatus: %s" % (item.get("sourceStatus") or ""),
            ]))
        return "\n".join(lines)

    old_ev = evidence_lines(old_evidence) or "- none"
    new_ev = evidence_lines(new_evidence) or "- none"
    signals_json = json.dumps(scoring_signals if scoring_signals is not None else {}, ensure_ascii=False)
    return "\n".join([
        "请判断以下两条用户记忆的关系，并只输出 JSON。",
        "",
        "旧记忆：",
        "summary: %s" % (old_memory.get("content") or ""),
        "evidence:",
        old_ev,
        "",
        "新记忆：",
        "summary: %s" % (new_memory.get("content") or ""),
        "evidence:",
        new_ev,
        "",
        "conflictScore: %s" % ("" if conflict_score is None else conflict_score),
        "scoringSignals: %s" % signals_json,
        "",
        "JSON 格式：",
        '{"resolutionType":"unrelated|context_difference|preference_evolution|direct_conflict|uncertain","resolvedSummary":"可选","currentSummary":"可选","historicalSummary":"可选","reason":"原因","confidence":0.0,"actions":{"createResolvedMemory":false,"oldMemoryStatus":"active|aging|archived|superseded|merged","newMemoryStatus":"active|aging|archived|superseded|merged","shouldUpdateCoreMemory":false,"shouldAskUser":false,"clarificationNeeded":false}}',
    ])


def parse_resolution(raw: Any) -> Optional[Dict[str, Any]]:
    """
    容错解析 resolver 的原始输出（`parseMemoryResolveResult`，`memory-schemas.ts:372-396`）。

    校验 `resolutionType ∈ RESOLUTION_TYPES`、`reason` 非空、`confidence` 是有限数，
    不合法一律返回 **None**（不抛异常）。解析通过则返回干净 dict（字段同源码）：
    resolutionType / reason / confidence / actions（actions 只保留 createResolvedMemory 与
    四个可选键中**合法**的那些），可选 summary 三键非空才带上。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = _loads_lenient(raw.strip())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    resolution_type = data.get("resolutionType")
    if not isinstance(resolution_type, str) or resolution_type not in RESOLUTION_TYPES:
        return None
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    confidence = _finite_number(data.get("confidence"))
    if confidence is None:
        return None

    raw_actions = data.get("actions")
    if not isinstance(raw_actions, dict):
        return None
    actions: Dict[str, Any] = {"createResolvedMemory": raw_actions.get("createResolvedMemory") is True}
    if raw_actions.get("oldMemoryStatus") in VALID_MEMORY_STATUS:
        actions["oldMemoryStatus"] = raw_actions["oldMemoryStatus"]
    if raw_actions.get("newMemoryStatus") in VALID_MEMORY_STATUS:
        actions["newMemoryStatus"] = raw_actions["newMemoryStatus"]
    if raw_actions.get("shouldUpdateCoreMemory") is True:
        actions["shouldUpdateCoreMemory"] = True
    if raw_actions.get("shouldAskUser") is True:
        actions["shouldAskUser"] = True
    if raw_actions.get("clarificationNeeded") is True:
        actions["clarificationNeeded"] = True

    result: Dict[str, Any] = {
        "resolutionType": resolution_type,
        "reason": reason,
        "confidence": confidence,
        "actions": actions,
    }
    for key in ("resolvedSummary", "currentSummary", "historicalSummary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return result


class MemoryResolver:
    """
    冲突裁决器。**不联网**：LLM 走注入的 `llm_fn(messages) -> str` 回调。

    流程（逐条照抄 `resolveNextConflict` 的单条分支，`memory-resolver.ts:158-190`，报告 §3.2）：
      get_resolver_queue → 对每条日志取 source/target 两条 L2 与各自 evidence → 拼提示词 →
      调 llm_fn → parse_resolution 校验 → 成功则 apply_resolver_resolution 落库并按源码分类
      写一条 reflectionLog（type=l0_update）；解析失败/校验不过/调用异常 → **不改库**、记错误、继续。
    """

    def __init__(self, llm_fn: Callable[[List[Dict[str, str]]], str]) -> None:
        self._llm_fn = llm_fn

    def resolve_once(self, store: Dict[str, Any], limit: int = 20) -> Dict[str, int]:
        """
        处理 resolver 队列中至多 `limit` 条日志。返回 `{"handled", "resolved", "failed"}`。

        ⚠ 单条失败不影响整批：任何异常都吞掉并记入 `ERRORS`，继续下一条。
        """
        queue = L.get_resolver_queue(store, limit)
        handled = 0
        resolved = 0
        failed = 0
        for log in queue:
            handled += 1
            try:
                ok = self._resolve_one(store, log)
            except Exception as exc:                             # pragma: no cover - 容错网
                _note_error("resolver 单条异常（log=%s）: %s" % (log.get("id"), exc))
                ok = False
            if ok:
                resolved += 1
            else:
                failed += 1
        return {"handled": handled, "resolved": resolved, "failed": failed}

    def _resolve_one(self, store: Dict[str, Any], log: Dict[str, Any]) -> bool:
        source_id = log.get("sourceL2Id")
        target_id = log.get("targetL2Id")
        new_memory = _find_l2(store, source_id)
        old_memory = _find_l2(store, target_id)
        if new_memory is None or old_memory is None:
            # 源码里这是「payload 构造失败」，等同本条的失败（不改库）。
            _note_error("resolver 缺 source/target 记忆（log=%s source=%s target=%s）"
                        % (log.get("id"), source_id, target_id))
            return False

        new_evidence = L.get_evidence_by_memory_id(store, source_id)
        old_evidence = L.get_evidence_by_memory_id(store, target_id)
        messages = [
            {"role": "system", "content": RESOLVER_SYSTEM_PROMPT},
            {"role": "user", "content": build_resolver_user_prompt(
                old_memory, old_evidence, new_memory, new_evidence,
                log.get("conflictScore"), log.get("scoringSignals"),
            )},
        ]
        try:
            raw = self._llm_fn(messages)
        except Exception as exc:
            _note_error("resolver LLM 调用失败（log=%s）: %s" % (log.get("id"), exc))
            return False
        resolution = parse_resolution(raw)
        if resolution is None:
            _note_error("resolver 输出校验不过（log=%s）: %s" % (log.get("id"), _shorten(raw)))
            return False

        applied = L.apply_resolver_resolution(store, log.get("id"), resolution)
        if applied is None:
            _note_error("resolver 落库失败（log=%s，记忆可能已过期）" % log.get("id"))
            return False

        # 依源码分类写一条反思日志。⚠ 反思日志 type 只有 3 个，没有专用 resolver 类型，
        #   这里复用 l0_update 记录「核心/长期记忆可能变动」的裁决结论（详情写进 details）。
        L.append_reflection_log(store, {
            "type": "l0_update",
            "summary": "冲突裁决 %s（log=%s）" % (resolution["resolutionType"], log.get("id")),
            "details": "终态=%s；reason=%s；confidence=%s"
                       % (applied.get("status"), resolution.get("reason"),
                          resolution.get("confidence")),
        })
        return True

# ==========================================================================
# B. compressor（相似压缩 + 画像反思）
# ==========================================================================

# L0_FIELD_DESCRIPTIONS 的兜底副本（照抄 memory-types.ts:11-17；拿不到 cyrene_l2write 时用）。
_FALLBACK_L0_FIELDS: Dict[str, str] = {
    "preferredName": '用户希望被如何称呼、叫什么名字、昵称。例如："叫我P宝""我叫Playa""以后喊我宝宝"',
    "occupation": '用户的职业、身份、工作。例如："我是前端工程师""我在做设计"',
    "longTermInterests": '用户的长期兴趣爱好（稳定的，不是临时的）。例如："我一直喜欢画画""我从小学钢琴"',
    "language": '用户常用的语言或地区习惯。例如："我习惯说中文""我是广东人"',
    "permanentNote": '其他不属于以上四类的稳定个人信息。例如："我有一只猫""我住在上海"',
}
_WRITE_MODULE: Any = None


def _imp_write():
    """惰性 import cyrene_l2write（复用它的 L0_FIELD_DESCRIPTIONS，避免两处各写一份走样）。"""
    global _WRITE_MODULE
    if _WRITE_MODULE is None:
        try:
            import cyrene_l2write as write_mod          # type: ignore
            _WRITE_MODULE = write_mod
        except Exception:
            _WRITE_MODULE = False
    return _WRITE_MODULE


def build_compress_user_prompt(texts: Sequence[str]) -> str:
    """
    阶段 A 的 user prompt（`memory-compressor.ts:80-90`，报告 §2.2，逐字）。

    `texts` 传**原始记忆内容**，本函数补 `- ` 前缀逐条拼接（源码里是 `- ${g.l2.content}`）。
    """
    lines = [_COMPRESS_USER_HEADER]
    for text in texts or []:
        lines.append("- %s" % (text or ""))
    return "\n".join(lines)


def build_reflect_user_prompt(
    l0: Dict[str, Any],
    l1: Dict[str, Any],
    field_descriptions: Dict[str, str],
) -> str:
    """
    阶段 B 的 user prompt（`memory-compressor.ts:189-209`，报告 §2.2，逐字）。

    `currentProfile` 逐字照抄 `memory-compressor.ts:161-174`：空值行省略、「对话轮数」无条件保留、
    两段之间保留一个空行。`field_descriptions` 展开为 `  ${field}：${desc}`。
    """
    l0 = l0 or {}
    l1 = l1 or {}
    profile_lines: List[str] = ["当前用户画像："]
    for field, label in (
        ("preferredName", "称呼"),
        ("occupation", "职业"),
        ("longTermInterests", "长期兴趣"),
        ("language", "常用语言"),
        ("permanentNote", "备注"),
    ):
        value = l0.get(field)
        if value:
            profile_lines.append("  %s：%s" % (label, value))
    profile_lines.append("")
    profile_lines.append("当前近期状态：")
    for field, label in (
        ("recentGoals", "最近目标"),
        ("recentPreferences", "近期偏好"),
        ("currentProject", "当前项目"),
    ):
        value = l1.get(field)
        if value:
            profile_lines.append("  %s：%s" % (label, value))
    profile_lines.append("  对话轮数：%s" % (l1.get("roundCount") if l1.get("roundCount") is not None else 0))
    current_profile = "\n".join(profile_lines)

    field_lines = ["  %s：%s" % (field, desc) for field, desc in (field_descriptions or {}).items()]
    return "\n".join([
        "回顾与用户的长期互动，判断是否需要更新用户画像或近期状态。",
        "",
        current_profile,
        "",
        "请分析：",
        "1. 是否有信息可以更新画像字段（稳定身份信息）？",
        "   可用字段：\n%s" % "\n".join(field_lines),
        "2. 是否有信息可以更新近况字段（近期目标/偏好/项目）？",
        "",
        "输出格式：",
        "{",
        '  "updates": [',
        '    { "layer": "L1", "field": "recentGoals", "content": "想系统性学习 Transformer", "confidence": 0.85 }',
        "  ]",
        "}",
        "",
        "近况字段可以选择 recentGoals / recentPreferences / currentProject。",
        '如果没有需要更新的信息，输出 {"updates":[]}。',
        "只输出 JSON，不要额外解释。",
    ])


def parse_reflect_updates(raw: Any) -> List[Dict[str, Any]]:
    """
    解析阶段 B 的原始输出 → update 列表（`parseMemoryReflectionResult`，`memory-schemas.ts`）。

    兼容顶层数组或 `{"updates": [...]}`。**单条校验**：`layer ∈ {L0, L1}`、`content` 非空字符串、
    `confidence` 是有限数；不合法**丢那一条**（不整批失败）。`field` 保留原文（是否合法由调用方
    按层白名单判定）。返回干净 dict 列表。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = _loads_lenient(text)
        except Exception:
            return []
    else:
        data = raw
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("updates"), list):
        items = data["updates"]
    else:
        return []

    updates: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        layer = item.get("layer")
        if layer not in ("L0", "L1"):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        confidence = _finite_number(item.get("confidence"))
        if confidence is None:
            continue
        clean: Dict[str, Any] = {"layer": layer, "content": content.strip(), "confidence": confidence}
        field = item.get("field")
        if isinstance(field, str) and field.strip():
            clean["field"] = field.strip()
        updates.append(clean)
    return updates


# 词法相似度用的小工具：CJK 字符区间、拉丁/数字词、停用词。
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_NUM_RE = re.compile(r"[a-z0-9]+")
# 极小的停用表（与 cyrene_l2store.STOP_TERMS 同源，另补几个高频虚词）。只为把「相同主题」压出来。
_LEX_STOP = frozenset({
    "用户", "一个", "一种", "这个", "那个", "自己", "因为", "所以", "但是",
    "没有", "不是", "不会", "不能", "喜欢", "讨厌", "反感", "厌恶",
    "我们", "你们", "他们", "什么", "怎么", "可以", "可能", "然后", "现在",
})


def _tokenize(text: str) -> set:
    """把文本切成词元集合：CJK 连续串做 2-gram，英文/数字词保留长度 ≥ 2 的。"""
    tokens = set()
    text = text or ""
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                tokens.add(run[i:i + 2])
    for word in _LATIN_NUM_RE.findall(text.lower()):
        if len(word) >= 2:
            tokens.add(word)
    return {token for token in tokens if token not in _LEX_STOP}


def tokenize_similarity(a: str, b: str) -> float:
    """
    词法相似度：两条文本词元集合的 `|A∩B| / max(|A|, |B|)`（0~1 的 Jaccard 型）。

    ⚠ 这是**降级**口径（源码用 RAG 向量余弦）。仅供 `cluster_similar_l2` 使用；两条完全相同的
      文本得 1.0，任一为空得 0.0。
    """
    set_a = _tokenize(a)
    set_b = _tokenize(b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    return inter / float(max(len(set_a), len(set_b)))


def _threshold_ratio(threshold: Any) -> float:
    """
    归一化相似度阈值：入参落在 (0, 1) 时按其原值（与源码 0.85 口径一致）；
    若给了 0-100 的「百分制」值（如 50 表示 0.5），换算回 0-1。缺省 None → SIMILARITY_THRESHOLD。
    """
    if threshold is None:
        return SIMILARITY_THRESHOLD
    value = float(threshold)
    if 0.0 < value < 1.0:
        return value
    return max(0.0, min(1.0, value / 100.0))


def cluster_similar_l2(l2_list: Sequence[Dict[str, Any]], threshold: Any = None) -> List[List[Dict[str, Any]]]:
    """
    词法相似度聚类（`memory-compressor.ts:44-70` 的贪心分组，报告 §4 #7/#8）。

    只在**非 archived、非 pinned** 的条目上做（另跳过 isSummary / 空 content）。贪心：
    以首个未用条目为种子，把与**种子**相似度 ≥ 阈值且未用的条目并入一组；组内条数 ≥
    `MIN_GROUP_SIZE`（3）才保留，否则丢弃该组。返回若干组（每组是同一批 L2 dict 的列表）。
    """
    ratio = _threshold_ratio(threshold)
    candidates = [
        mem for mem in (l2_list or [])
        if isinstance(mem, dict)
        and mem.get("status") != "archived"
        and not mem.get("isPinned")
        and not mem.get("isSummary")
        and (mem.get("content") or "").strip()
    ]

    groups: List[List[Dict[str, Any]]] = []
    used: set = set()
    for mem in candidates:
        mem_id = mem.get("id")
        if mem_id in used:
            continue
        group = [mem]
        used.add(mem_id)
        seed = mem.get("content") or ""
        for other in candidates:
            other_id = other.get("id")
            if other_id in used:
                continue
            if tokenize_similarity(seed, other.get("content") or "") >= ratio:
                group.append(other)
                used.add(other_id)
        if len(group) >= MIN_GROUP_SIZE:
            groups.append(group)
    return groups

class MemoryCompressor:
    """
    相似压缩 + 画像反思。**不联网**：LLM 走注入的 `llm_fn(messages) -> str` 回调。

    `compress_once(store)` 跑两阶段（`memory-compressor.ts` 的 `compressMemories` +
    `runReflection`，报告 §2.2）：
      阶段 A：`cluster_similar_l2` 聚类 → 每个 ≥2 条的簇调 LLM 合并 → 新建一条总结条目
              （isSummary）并把簇内**其余**条目标 `merged`（理由见 `_archive_merged`）→
              写一条 type=compression 的反思日志。
      阶段 B：用当前 L0/L1 拼提示词 → 解析 updates → 只落 layer∈{L0,L1}、field 合法、
              confidence ≥ 0.6 的项 → 每落一条写一条 l0_update / l1_update 反思日志。
    任何单步失败只丢那一步、记 `ERRORS`，不整批炸。
    """

    def __init__(self, llm_fn: Callable[[List[Dict[str, str]]], str]) -> None:
        self._llm_fn = llm_fn

    def compress_once(self, store: Dict[str, Any], threshold: Any = None) -> Dict[str, int]:
        """
        返回统计：`{"groups", "compressed", "updates", "failed"}` ——
        groups=保留的簇数，compressed=被标 merged 的来源条数，updates=落库的 L0/L1 更新数，
        failed=失败步数。`threshold` 透传给 `cluster_similar_l2`（见该函数）。
        """
        groups, compressed, failed_a = self._compress_stage_a(store, threshold)
        updates, failed_b = self._reflect_stage_b(store)
        return {
            "groups": groups,
            "compressed": compressed,
            "updates": updates,
            "failed": failed_a + failed_b,
        }

    # ── 阶段 A ──
    def _compress_stage_a(self, store: Dict[str, Any], threshold: Any = None):
        clusters = cluster_similar_l2(L.get_all_l2(store), threshold)
        groups = 0
        compressed = 0
        failed = 0
        for cluster in clusters:
            groups += 1
            texts = [(mem.get("content") or "") for mem in cluster]
            messages = [
                {"role": "system", "content": COMPRESS_SYSTEM_PROMPT},
                {"role": "user", "content": build_compress_user_prompt(texts)},
            ]
            try:
                raw = self._llm_fn(messages)
            except Exception as exc:
                _note_error("compressor 阶段A LLM 调用失败: %s" % exc)
                failed += 1
                continue
            summary = _clean_summary(raw)
            if not summary:
                _note_error("compressor 阶段A 总结为空，跳过该簇")
                failed += 1
                continue

            head = cluster[0]
            sub_ids = [mem.get("id") for mem in cluster]
            try:
                # 新建总结条目：从簇内首条继承会话/触发文本等元信息（源码 createSummary 的入参）。
                L.add_l2(store, {
                    "content": summary,
                    "triggerText": head.get("triggerText") or summary,
                    "sourceConversationId": head.get("sourceConversationId") or "",
                    "isSummary": True,
                    "subEntryIds": sub_ids,
                    "syncStatus": "pending_sync",
                })
                compressed += self._archive_merged(store, sub_ids)
                L.append_reflection_log(store, {
                    "type": "compression",
                    "summary": "压缩 %d 条记忆为一条总结" % len(sub_ids),
                    "details": "原条目：%s\n总结：%s" % (" | ".join(texts), summary),
                })
            except Exception as exc:
                _note_error("compressor 阶段A 落库失败: %s" % exc)
                failed += 1
        return groups, compressed, failed

    @staticmethod
    def _archive_merged(store: Dict[str, Any], sub_ids: Sequence[str]) -> int:
        """
        把簇内来源标 `merged`（`memory-status = merged`，`memory-types.ts:74`）。

        依据：源码走 `commitMemoryCompression` 事务，该文件不在 `_ref/` 拷贝里；但 compressor
        调用点（`memory-compressor.ts:104-127`）提供的 deps 是 `createSummary`（:115，新建总结）
        + `archiveSources`（:118，归档来源）。故手机端等价做法是「新建一条 isSummary 总结 +
        把来源标 merged」——`merged` 比 `archived` 更贴切（来源被并入了新总结，而非单纯淘汰）；
        `L2MemoryStatus` 里也确有 `merged`（`memory-types.ts:74`）。返回改动条数。
        """
        return L.update_l2_status(store, list(sub_ids), "merged")

    # ── 阶段 B ──
    def _reflect_stage_b(self, store: Dict[str, Any]):
        l0 = store.get("l0") if isinstance(store.get("l0"), dict) else {}
        l1 = store.get("l1") if isinstance(store.get("l1"), dict) else {}
        write_mod = _imp_write()
        field_descriptions = getattr(write_mod, "L0_FIELD_DESCRIPTIONS", None) if write_mod else None
        if not isinstance(field_descriptions, dict):
            field_descriptions = _FALLBACK_L0_FIELDS
        messages = [
            {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
            {"role": "user", "content": build_reflect_user_prompt(l0, l1, field_descriptions)},
        ]
        try:
            raw = self._llm_fn(messages)
        except Exception as exc:
            _note_error("compressor 阶段B LLM 调用失败: %s" % exc)
            return 0, 1
        try:
            updates = parse_reflect_updates(raw)
        except Exception as exc:                                 # pragma: no cover - 解析已容错
            _note_error("compressor 阶段B 解析失败: %s" % exc)
            return 0, 1

        valid_l0_fields = set(field_descriptions.keys())
        valid_l1_fields = set(getattr(L, "L1_FIELD_WHITELIST",
                                      ("recentGoals", "recentPreferences", "currentProject")))
        applied = 0
        failed = 0
        for item in updates:
            # confidence 阈值 0.6（memory-compressor.ts:229）。
            if item["confidence"] < REFLECT_CONFIDENCE_MIN:
                continue
            try:
                if item["layer"] == "L0":
                    field = item.get("field")
                    if not field or field not in valid_l0_fields:
                        continue
                    if l0.get("isPinned"):
                        continue
                    L.upsert_l0_field(store, field, item["content"])
                    L.append_reflection_log(store, {
                        "type": "l0_update",
                        "summary": "L0.%s 更新为 \"%s\"（置信度 %.2f）"
                                   % (field, item["content"][:30], item["confidence"]),
                    })
                    applied += 1
                else:  # item["layer"] == "L1"（parse_reflect_updates 已保证）
                    resolved = L.resolve_l1_field(item.get("field"), item["content"])
                    if resolved not in valid_l1_fields:
                        continue
                    L.replace_l1_field(store, resolved, item["content"])
                    L.append_reflection_log(store, {
                        "type": "l1_update",
                        "summary": "L1.%s 更新为 \"%s\"（置信度 %.2f）"
                                   % (resolved, item["content"][:30], item["confidence"]),
                    })
                    applied += 1
            except Exception as exc:
                _note_error("compressor 阶段B 落库失败: %s" % exc)
                failed += 1
        return applied, failed


# ==========================================================================
# C. 衰减与体积护栏
# ==========================================================================

def run_decay(store: Dict[str, Any]) -> int:
    """
    全局衰减一轮，直接转发 `l2store.decay_l2_weights`（`memory-store.ts:624-652`），返回改动条数。

    三态判定（闭区间）：weight ≥ 30 → active，≥ 10 → aging，否则 archived；
    跳过 pinned / 已 archived / weight ≤ 0。
    """
    return L.decay_l2_weights(store)


def enforce_l2_limit(store: Dict[str, Any], max_n: int = L2_MAX_ENTRIES) -> int:
    """
    软上限护栏：L2 条数超过 `max_n` 时，把多出来的部分按 weight **升序**归档。

    ⚠ `L2_MAX_ENTRIES` 是**手机端自补约束，源码里没有**（桌面端不设条数上限）。pinned 与
      已 archived 的条目一律跳过，不参与归档。返回归档条数。

    ⚠ 只改 status（走 `update_l2_status`，等价 `archive_l2_batch`），不动 weight / 其它字段。
    """
    # ⚠ 「上限」只统计**未归档**条目：已 archived 的跳过（`跳过 pinned 与已 archived 的`）。
    #    若把 archived 也算进去，护栏每次运行都会继续归档活跃条目、最终把活跃集掏空——
    #    这不是体积护栏该有的行为。按「活跃集」计数可保证幂等（再跑一次不动）。
    live = [
        mem for mem in L.get_all_l2(store)
        if isinstance(mem, dict) and mem.get("status") != "archived"
    ]
    over = len(live) - int(max_n)
    if over <= 0:
        return 0
    archivable = [mem for mem in live if not mem.get("isPinned")]
    archivable.sort(key=lambda mem: mem.get("weight") or 0)
    victims = [mem.get("id") for mem in archivable[:over]]
    if not victims:
        return 0
    return L.update_l2_status(store, victims, "archived")


# ==========================================================================
# D. 小工具
# ==========================================================================

def _clean_summary(raw: Any) -> str:
    """清洗 LLM 的输出：去首尾引号/空白；不足 5 字视为无效（源码 `cleanSummary.length < 5`）。"""
    text = raw
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return ""
    text = re.sub(r'^["「『]+|["」』]+$', "", text.strip()).strip()
    return text if len(text) >= 5 else ""


def _find_l2(store: Dict[str, Any], l2_id: Any) -> Optional[Dict[str, Any]]:
    """按 id 找一条 L2（复用 cyrene_l2store 的私有查找器，语义完全一致）。"""
    return L._find_l2(store, l2_id) if l2_id else None


def _shorten(value: Any, max_n: int = 120) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\n", " ")
    return text if len(text) <= max_n else text[:max_n] + "…"