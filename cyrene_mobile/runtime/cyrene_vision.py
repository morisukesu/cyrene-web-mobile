# -*- coding: utf-8 -*-
"""cyrene_vision —— 视觉子系统（`cyrene_web` 拆出来的第四块）。

它原来是什么
------------
让「昔涟」真的能看见图的那一套，从头到尾一条链：

  多模态表示层   判断一条消息里有没有图、把图剥出来省 token、把图拼进上下文
  图片路由       `resolve_image_route` 决定这张图走「直接塞上下文」还是「先描述」
                 还是「拒掉」，依据是模型支不支持图、配没配视觉端点
  描述缓存       `VISION_CAPTION_CACHE_MAX` 条内存 TTL 缓存（不落盘：手机磁盘
                  已用 98%，任何「顺手写一份到磁盘」都是给自己挖坑）
  协议层         `VisionClient` 直连视觉端点、`image_dimensions` 解宽高、
                  `sniff_image_type` 认格式、`caption_image` 是总入口
  两个工具       `read_image`（本地图）/ `read_image_url`（公网图）

在宿主里这些散成六段（244–296 / 866–948 / 950–1060 / 1062–1143 / 1146–1509 /
3240–3318），中间夹着附件上传、记忆函数、网络 handler。

它依赖宿主什么
--------------
由宿主经 `bind_deps()` 注入：

  · `settings()`        配置读取（取值函数，SETTINGS 换版也读得到新的）
  · `outcome_success` / `outcome_failure`   工具结果常量
  · `guard_path` / `path_guard_error`       路径守卫（来自 cyrene_fs）
  · `rel` / `fs_root`                      回显相对路径
  · `max_file_bytes`                       单张图的硬上限（派生自 FS 上限）

⚠ 这几个注入项**缺一不可，且缺失会静默走偏**：`multimodal_enabled` 与
  `resolve_caption_vision_config` 原本用 `except (NameError, AttributeError)`
  兜底（那段代码是防「SETTINGS 还没加载」的），搬出来之后若注入没做，
  异常会被同一个兜底吞掉 —— 不报错，但路由**恒判 direct**，行为全变。
  所以 `deps_ready()` 把它列为必需项，启动自检要盯。

红线同前：**绝不 import cyrene_web**，方向只有「宿主 → 内核 → 子系统」。
"""

import base64
import hashlib
import json
import re
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

__all__ = [
    "VISION_RANGES", "VISION_TOOL_IDS", "VISION_CAPTION_CACHE_MAX",
    "VISION_LAST", "VISION_ERR_CONFIG", "VISION_ERR_NETWORK",
    "VISION_ERR_TOO_BIG", "VISION_ERR_FORMAT", "VISION_DEFAULT_PROMPT",
    "IMAGE_ROUTE_DIRECT", "IMAGE_ROUTE_CAPTION", "IMAGE_ROUTE_REJECT",
    "IMAGE_REJECT_REASON", "DEPS.support_probe()",
    "normalize_vision", "vision_cfg", "vision_ready", "vision_tools_allowed",
    "multimodal_enabled", "resolve_image_route", "resolve_caption_vision_config",
    "vision_cache_get", "vision_cache_put", "vision_cache_clear",
    "vision_cache_count",
    "content_has_image", "messages_have_images", "media_to_text",
    "strip_images", "image_item", "read_injectable", "build_vision_content",
    "vision_endpoint_url", "sniff_image_type", "image_dimensions",
    "read_image_bounded", "vision_max_bytes", "caption_image",
    "VisionClient",
    "bind_deps", "deps_ready", "DEPS", "PLUGIN",
    "_h_read_image", "_h_read_image_url",
]


def _def_settings():
    """`settings` 的内置兜底：返回空 dict。

    `vision_cfg` 在宿主的 `build_system_prompt`（模块加载期）就会被调用，那时
    SETTINGS 还没装配好。原来那段代码靠 `except (NameError, AttributeError)`
    兜底，这里等价 —— 读不到就全部回落默认值。
    """
    return {}


def _def_as_bool(v, fb=False):
    """`_as_bool` 的内置兜底。

    为什么要兜底：`normalize_vision` 在宿主的 `load_settings()` 阶段就会被调用，
    那时内核还没启动、注入还没发生。这三个小纯函数不是「能力」，是稳定的小工具，
    给一份兜底比让整个配置加载炸掉划算；宿主注入进来会覆盖它们（行为一致）。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off"):
            return False
    return fb


def _def_as_str(v, fb=""):
    """`_as_str` 的内置兜底：非字符串一律回落默认值。"""
    return v if isinstance(v, str) else fb


def _def_clamp(v, lo, hi, fb):
    """`_clamp` 的内置兜底：取整并夹到 [lo, hi]，非法值回落 fb。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = int(fb)
    return max(lo, min(hi, n))


class _Deps:
    """宿主注入的资源袋。"""

    __slots__ = ("settings", "outcome_success", "outcome_failure",
                 "guard_path", "path_guard_error", "rel", "fs_root",
                 "max_file_bytes", "as_bool", "as_str", "clamp",
                 "support_probe", "human_size", "resolve_upload")

    def __init__(self):
        # settings 同样给兜底（见 _def_settings 的说明）；它**仍列在 missing() 里**，
        # 因为内核起来之后必须换成宿主的实时取值函数 —— 否则配置读不到，会静默按默认值跑
        self.settings = _def_settings
        self.outcome_success = "success"
        self.outcome_failure = "failure"
        self.guard_path = None
        self.path_guard_error = None
        self.rel = None
        self.fs_root = None
        self.max_file_bytes = None
        # 这三个给内置兜底而不是 None —— 它们在 `load_settings()` 阶段就会被用到，
        # 那时内核还没启动。宿主注入会覆盖。
        self.as_bool = _def_as_bool
        self.as_str = _def_as_str
        self.clamp = _def_clamp
        # 「端点收不收图」的进程级探测结果 —— 宿主那份才是唯一真相
        # （LLMClient 撞到 502 时会写它），所以这边读的是宿主的取值函数
        self.support_probe = None
        # `_human_size`（相对大小显示）随文件工具组搬去了 cyrene_fs；
        # `resolve_upload`（附件 id → 路径）属附件上传、留在宿主 —— 都从 fs 服务取
        self.human_size = None
        self.resolve_upload = None

    def missing(self):
        # max_file_bytes 是**取值函数**（宿主给 lambda: VISION_MAX_BYTES）：
        # 这样测试直接改写宿主那个常量就能生效，不必去捅子系统内部。
        # as_bool / as_str / clamp 不在这里：它们有内置兜底，缺了不会走偏。
        # support_probe 在 —— 缺了会让图片路由恒判 direct，属于静默走偏。
        need = ("settings", "guard_path", "path_guard_error", "rel", "fs_root",
                "max_file_bytes", "support_probe", "human_size",
                "resolve_upload")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()
_FRESH_DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入。未知的键直接报错，不静默吞掉。"""
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_vision.bind_deps 不认识这些参数：%s"
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
        guard_path=host.guard_path,
        path_guard_error=host.path_guard_error,
        rel=host.rel,
        fs_root=host.root,
        max_file_bytes=tools.max_file_bytes,
        as_bool=tools.as_bool,
        as_str=tools.as_str,
        clamp=tools.clamp,
        support_probe=tools.support_probe,
        human_size=host.human_size,
        resolve_upload=host.resolve_upload,
    )
    ctx.effect(_reset_deps, desc="vision:deps")
    ctx.log("已接到内核（依赖 config / tools / fs）")


PLUGIN = {
    "id": "vision",
    "requires": ["config", "tools", "fs"],
    "setup": _plugin_setup,
}


# ===========================================================================
# 以下正文由 _make_vision_module.py 从 cyrene_web.py 逐字抽出
# ===========================================================================


def _h_read_image(a):
    """read_image：把本地图片交给独立视觉模型，返回文字描述。

    与 read_file 的分工（对齐桌面端那套）：
      read_file  → 文本文件
      read_image → 本地图片，让「她」看见内容
      read_image_url → 公网图片，本机不下载

    这里只做参数校验 + 路径守卫 + 调用协议层；图片怎么编码、请求怎么拼，
    全在 caption_image 里，本函数不碰 image_url 格式。
    """
    ok, conf = resolve_caption_vision_config()
    if not ok:
        # 配置不齐是「可预期」的失败，不是异常：返回可读原因，
        # 她会照着如实告诉用户（对应工具描述里那句「未配置时会返回错误」）。
        return DEPS.outcome_failure, str(conf)

    try:
        p = DEPS.guard_path(a.get("path"), want="file")
    except DEPS.path_guard_error as e:
        return DEPS.outcome_failure, str(e)

    question = (a.get("question") or "").strip()[:500]
    text = caption_image({"path": p}, question, cfg=(ok, conf))
    if isinstance(text, str) and text.startswith("[错误"):
        return DEPS.outcome_failure, text

    head = f"图片: {DEPS.rel(p, DEPS.fs_root())}"
    return DEPS.outcome_success, head + "\n" + text


def _h_read_image_url(a):
    """read_image_url：把公网图片 URL 交给独立视觉模型，返回文字描述。

    URL 原样直传给厂商，本机不下载、不转 base64 —— 省流量省内存，
    也让「看一眼网上的图」不必先落一份副本（要副本另有 download_file）。
    """
    url = (a.get("url") or "").strip()
    if not url:
        return DEPS.outcome_failure, "(缺少 url)"
    if not re.match(r"^https?://", url, re.I):
        return DEPS.outcome_failure, ("(url 必须以 http:// 或 https:// 开头。"
                                 "本机路径请用 read_image)")

    ok, conf = resolve_caption_vision_config()
    if not ok:
        return DEPS.outcome_failure, str(conf)

    question = (a.get("question") or "").strip()[:500]
    text = caption_image({"url": url}, question, cfg=(ok, conf))
    if isinstance(text, str) and text.startswith("[错误"):
        return DEPS.outcome_failure, text
    return DEPS.outcome_success, text


# ---------- 多模态（图片进上下文） ----------
# 只在**首轮**把带附件的那条 user 消息换成 content 数组：work 上下文每次
# LLM 调用都整份重发，若一直留着图片，maxTurns=4 就是 4 次带图，白烧 token。
def content_has_image(content):
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get("type") == "image_url"
               for p in content)


def messages_have_images(messages):
    return any(isinstance(m, dict) and content_has_image(m.get("content"))
               for m in messages)


def media_to_text(content):
    """把多模态 content 数组压回纯文本（丢图片，只留 text 部分）。"""
    if not isinstance(content, list):
        return content if isinstance(content, str) else str(content or "")
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(str(p.get("text") or ""))
    return "\n".join(x for x in parts if x)


def strip_images(messages):
    """把消息里的多模态图片剥掉，只留文本。端点不收图时的退路。"""
    out = []
    for m in messages:
        if isinstance(m, dict) and content_has_image(m.get("content")):
            nm = dict(m)
            nm["content"] = media_to_text(m.get("content"))
            out.append(nm)
        else:
            out.append(m)
    return out


def image_item(a, raw):
    """一张图片 → OpenAI 兼容的 image_url 片段（data URI 内联）。"""
    b64 = base64.b64encode(raw).decode("ascii")
    mime = a.get("mime") or "image/png"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def read_injectable(p):
    """读一张准备注入上下文的图。返回 (bytes, 是否因过大/读不到被跳过)。

    上限用 FS_MAX_FILE_BYTES（4 MiB）：base64 后是 5.3 MiB，再乘历史轮次会
    把请求体推得很大。手机端没有图像库可以缩图，超限就只落盘不进上下文。
    """
    try:
        if p.stat().st_size > DEPS.max_file_bytes():
            return None, True
        raw = p.read_bytes()
    except OSError:
        return None, True
    if not raw:
        return None, True
    return raw, False


def build_vision_content(text, atts):
    """文本 + 可注入图片 → OpenAI 兼容的多模态 content 数组。

    返回 (content, skipped)：skipped 是不进上下文的图片名（过大或读不到）。
    调用方要在文本里说明这些图的情况，免得用户以为她「看见了」。
    """
    content = [{"type": "text", "text": text}]
    skipped = []
    for a in atts:
        if not a.get("isImage"):
            continue
        p = DEPS.resolve_upload(a["id"])
        if p is None:
            skipped.append(a["name"]); continue
        raw, too_big = read_injectable(p)
        if too_big or raw is None:
            skipped.append(a["name"]); continue
        content.append(image_item(a, raw))
    return content, skipped


# ========== 图片路由（对齐桌面端 Cyrene-Agent 的 image-router） ==========
# 桌面端那份的设计声明，这里照抄口径：
#   「所有图片入口（附件 / 工具 / 频道 / 动态）先问这里，不允许各自判断。」
#   「三种结果，不允许模糊状态。」
#
# 为什么手机端也需要这一层：本机主通道是那台中转站，实测收不了图。若各处自己
# 判断（附件那边看 DEPS.support_probe()、工具那边看配置），就会出现「同一个配置下
# 附件说看不见、工具说看得见」这种自相矛盾的状态。收口到一处，结论只有三种。
IMAGE_ROUTE_DIRECT = "direct"     # 主模型自己看图（base64 直传）
IMAGE_ROUTE_CAPTION = "caption"   # 交给独立视觉模型转述成文字
IMAGE_ROUTE_REJECT = "reject"     # 当前配置看不了图，带面向用户的说明

# reject 的文案。对齐桌面端措辞，但把入口名换成本机的「设置 → 视觉」。
IMAGE_REJECT_REASON = (
    "当前主模型不是多模态，且未配置独立视觉模型。"
    "请在「设置 → 视觉」中配置视觉端点，或把主模型换成多模态的。"
)


def multimodal_enabled():
    """主模型是否按多模态走。三态语义见 DEFAULT_SETTINGS["model"]["multimodal"]。

    auto 的判据是 DEPS.support_probe()：本文件原有的探测结论。
      None = 还没试过（默认按能收图处理，让第一次请求去证伪）
      True = 端点收下了图
      False = 已确认不收（实测 502 那条路径）
    之所以 auto 认 False 为「非多模态」，是因为那份探测是**实测**结论，
    比起用户的猜测或配置声明，实测更该被信任。
    """
    try:
        mm = DEPS.settings().get("model", {}).get("multimodal", "auto")
    except (NameError, AttributeError):
        mm = "auto"
    if mm == "on":
        return True
    if mm == "off":
        return False
    # auto 的两段判据：
    #   ① 已有实测结论（True/False）→ 一律以实测为准，这是最可信的来源；
    #   ② 还没探测过（None）时，若用户已把独立视觉端点配齐，就把图交给它。
    # 为什么要加 ②：不加的话 None 会被当成「主模型能收图」，直接判 direct，
    # 主模型收不了图时用户看到的是一次 502/554，而他明明配好了视觉通道却
    # 一次都没被用上（实测 2026-10-10：配置齐、/vision/status 显示
    # route=direct、面板测试返回的 model 是主模型而非视觉端点）。
    # 用户专门配了视觉端点，本来就是想让它看图，这里让它优先是符合意图的。
    if DEPS.support_probe() is None and vision_ready():
        return False
    return DEPS.support_probe() is not False


def resolve_image_route(source="attachment", multimodal=None):
    """统一的图片路由判定。返回 dict：{"mode", "config"?, "reason"?}。

    判定顺序严格对齐桌面端 resolveImageRoute：
      ① 主模型多模态          → direct
      ② 独立视觉三项齐 + 开关 → caption
      ③ 都不满足              → reject + 面向用户的 reason

    source 目前只用于未来扩展与日志（桌面端有 attachment/tool/channel/moments
    四种来源），手机端当前都是同一条判据；保留参数是为了调用点读起来有意义，
    也免得以后细分来源时要改所有调用签名。

    multimodal 参数供测试注入（None = 用真实判定），生产调用不传。
    """
    if multimodal is None:
        mm_on = multimodal_enabled()
    else:
        mm_on = bool(multimodal)
    if mm_on:
        return {"mode": IMAGE_ROUTE_DIRECT}

    if vision_ready():
        return {
            "mode": IMAGE_ROUTE_CAPTION,
            "config": {
                "api_base": str(vision_cfg("api_base", "") or "").strip(),
                "api_key": str(vision_cfg("api_key", "") or ""),
                "model": str(vision_cfg("model", "") or "").strip(),
            },
        }

    return {"mode": IMAGE_ROUTE_REJECT, "reason": IMAGE_REJECT_REASON}


def resolve_caption_vision_config():
    """转述用的视觉配置：成功给 (True, config)，失败给 (False, 面向用户的错误)。

    对齐桌面端 resolveCaptionVisionConfig。它比 resolve_image_route 少一种结果：
    direct 在那边会「回落到主模型自己看图」，但**工具**场景下这条路走不通 ——
    read_image 的职责就是把图变成文字给她看，主模型能自己看图时这个工具本就
    不该被调用（工具描述里写了「不要用于」）。所以这里 direct 也返回
    ok=True + 主模型配置，让调用方照常发一次请求；真被误调也就是多花一次调用，
    不会出错。

    返回 config 一律是三个字段齐的 dict（api_base / api_key / model）。
    """
    route = resolve_image_route("tool")
    mode = route.get("mode")
    if mode == IMAGE_ROUTE_CAPTION:
        return True, route["config"]
    if mode == IMAGE_ROUTE_REJECT:
        return False, route.get("reason") or IMAGE_REJECT_REASON
    # direct：主模型自己能看图，用主模型配置发（现状行为保留）
    try:
        m = DEPS.settings().get("model", {})
    except (NameError, AttributeError):
        m = {}
    return True, {"api_base": str(m.get("api_base", "") or "").strip(),
                  "api_key": str(m.get("api_key", "") or ""),
                  "model": str(m.get("model", "") or "").strip()}


# ========== 视觉描述缓存（对齐桌面端 TtlResultCache） ==========
# 桌面端那份的注释把要害写清楚了，这里照搬口径：
#   「每次调用都是真金白银的视觉模型请求。模型对同一张图反复看（"再看下那个图…"）
#     时直接复用 30 分钟内的描述。key 必须带 userQuery：同一个图不同问题的描述不同。」
#
# 与桌面端唯一的差别：手机端**只用内存**，不落盘。
# 桌面端也是纯内存 TTL 缓存，但手机还有一条额外理由 —— 磁盘已用 916G/943G（98%），
# 任何「顺手写一份到磁盘」的设计都是给自己挖坑。内存方案同时解决体积与磁盘两件事。
VISION_CAPTION_CACHE_MAX = 64        # 条数上限：手机内存有限，别无限攒

_vision_cache = {}                   # key -> (ts, text)；dict 保序，天然可做 FIFO 淘汰
_vision_cache_lock = threading.Lock()

# 上一次调用的结果，供 /vision/status 与面板自检显示（不含 key、不含图片内容）
VISION_LAST = {}


def _vision_cache_ttl():
    """当前生效的缓存 TTL（秒）。0 = 不缓存（用户把 cacheTtlMin 拉到 0）。"""
    try:
        return max(0, int(vision_cfg("cacheTtlMin", 30) or 0)) * 60
    except (TypeError, ValueError):
        return 30 * 60


def vision_cache_get(key):
    """读缓存。命中返回 (ts, text)，否则 None。

    TTL 是**读取时**按当前配置判的，不是写入时定死的 —— 这样用户在面板上
    把 30 分钟改成 5 分钟能立刻生效，不需要重启服务或重建缓存。
    """
    if not key:
        return None
    ttl = _vision_cache_ttl()
    if ttl <= 0:
        return None
    now = time.time()
    with _vision_cache_lock:
        item = _vision_cache.get(key)
        if item is None:
            return None
        ts, text = item
        if now - ts > ttl:
            # 顺手清掉过期项。不清也不影响正确性，但会让淘汰顺序变脏。
            _vision_cache.pop(key, None)
            return None
        return ts, text


def vision_cache_put(key, text):
    """写缓存。

    ⚠ 调用方必须先确认这是**成功**的描述。桌面端那条判据在这里同样成立：
    错误描述不缓存，下次重试（否则一次网络抖动会被固化 30 分钟）。
    这个判断收在调用方而不是这里，是为了让「要不要缓存」这个决定看得见。
    """
    if not key or not text:
        return
    with _vision_cache_lock:
        # 满了先淘汰最旧的。popitem(last=False) 取的是最早插入的键，
        # 因为 dict 保序且我们从不原地更新已有键的顺序 —— 被命中的键不重排，
        # 所以这是 FIFO 而不是严格 LRU。对「同一张图看几次」的用法足够，
        # 而且比维护 LRU 链表少一半代码和一半出错机会。
        while len(_vision_cache) >= VISION_CAPTION_CACHE_MAX:
            try:
                _vision_cache.popitem(last=False)
            except KeyError:
                break
        _vision_cache[key] = (time.time(), text)


def vision_cache_clear():
    """清空缓存。返回清掉的条数。"""
    with _vision_cache_lock:
        n = len(_vision_cache)
        _vision_cache.clear()
        return n


def vision_cache_count():
    with _vision_cache_lock:
        return len(_vision_cache)


# ========== 独立视觉协议层（对齐桌面端 vision-captioner） ==========
# 桌面端工具文件里写明的分工，这里照搬：
#   「协议构造全部委托 vision-captioner（唯一多模态协议层），
#     工具文件只做参数校验 + 视觉配置检查 + 调用，不碰 image_url 格式细节。」
# 手机端同样把协议收在这几个函数里，工具 handler 不自己拼 payload。
#
# 错误前缀是约定的一部分：调用方靠 "[错误" 前缀判断成功与否（缓存层就吃这条），
# 前缀本身也直接面向模型 —— 她会照着「[错误·配置] …」如实告诉用户看不了。
VISION_ERR_CONFIG = "[错误·配置]"
VISION_ERR_NETWORK = "[错误·网络]"
VISION_ERR_TOO_BIG = "[错误·超限]"
VISION_ERR_FORMAT = "[错误·格式]"

# 没给 question 时的默认指令。写成「如实描述」而不是「分析」：她的用途是
# 「看见这张图然后说话」，不是出一份图像分析报告。
VISION_DEFAULT_PROMPT = (
    "请描述这张图片的内容：主要对象、场景、可见的文字，以及整体氛围。"
    "如实描述你看到的，看不清的地方就说看不清，不要猜测。"
)


def vision_endpoint_url(base):
    """把用户填的 api_base 规范化成真正要打的 /chat/completions 地址。

    为什么需要这一层：设置面板那一栏叫「接口地址」，用户很自然会直接粘一条
    完整端点（`https://host/v1/chat/completions`）。天真拼接会得到
    `…/v1/chat/completions/chat/completions`，网关回的往往是 404 / 554 这类
    看起来像「限流」或「未找到」的码，排查时会一路往网络和密钥上找。

    实测（2026-10-10，本机中转站 + 一个能收图的视觉模型）：
      · 原样打完整端点   → 200（带图也答对）
      · 拼接后（旧行为） → 404 Not Found
      · 从 /v1 补端点    → 200

    三种输入都收：
      https://host/v1                    → 补 /chat/completions
      https://host/v1/chat/completions   → 原样
      https://host/v1/chat/completions/  → 去尾斜杠后原样
    """
    b = str(base or "").strip().rstrip("/")
    if not b:
        return ""
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"

# 魔数 → (mime, 规范扩展名)。判真实类型用，见下面 sniff_image_type 的说明。
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"BM", "image/bmp", "bmp"),
)


def sniff_image_type(data):
    """读文件头判真实图片类型。返回 (mime, ext)，认不出返回 None。

    为什么按魔数而不是按扩展名：借 dsh read_image 的那条设计 ——
    「扩展名是声明，魔数与像素校验才是权威」。手机端这份更该这么做，因为
    交接文档里记着实测坑：`builtin-20.gif` / `34.png` / `38.gif` 三张图
    文件头其实是 RIFF/WEBP，扩展名骗人。当时靠手工改名修好了，但那属于
    「人肉兜底」；此处的校验就是为了让同类问题在运行时自己暴露出来，
    而不是等到浏览器渲染不出来。
    """
    if not data or len(data) < 12:
        return None
    for magic, mime, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime, ext
    # WebP：RIFF....WEBP（前 4 字节 RIFF，8-12 字节 WEBP）
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def image_dimensions(data, mime):
    """尽力解析图片像素尺寸。认不出返回 None。

    为什么值得写这几行：返回元数据里的宽高能让她判断「这是不是一张缩略图 /
    是不是长截图」，也让「图太大看不清」这种结论有依据。手机端没有 PIL，
    所以只解析各格式的头部字段 —— 不做完整解码，解析失败一律回 None，
    绝不因为一个尺寸字段让整次转述失败。
    """
    try:
        if mime == "image/png":
            # IHDR 紧跟在 8 字节签名 + 4 字节长度 + 4 字节类型之后
            if len(data) >= 24 and data[12:16] == b"IHDR":
                return (int.from_bytes(data[16:20], "big"),
                        int.from_bytes(data[20:24], "big"))
        elif mime == "image/gif":
            if len(data) >= 10:
                return (int.from_bytes(data[6:8], "little"),
                        int.from_bytes(data[8:10], "little"))
        elif mime == "image/bmp":
            if len(data) >= 26:
                return (int.from_bytes(data[18:22], "little"),
                        int.from_bytes(data[22:26], "little"))
        elif mime == "image/webp":
            # VP8X：24 位宽高减一，各占 3 字节
            if len(data) >= 30 and data[12:16] == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return (w, h)
            # VP8L：14 位宽高打包在一个小端 32 位里
            if len(data) >= 25 and data[12:16] == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            # VP8（有损）：宽高在帧头里，偏移较深，这里不做（回 None 更诚实）
        elif mime == "image/jpeg":
            return _jpeg_dimensions(data)
    except Exception:
        # 尺寸是锦上添花，任何一种解析意外都退回 None，不打断主流程
        return None
    return None


def _jpeg_dimensions(data):
    """扫 JPEG 的 SOF 段取宽高。SOF0/1/2/3/5/6/7/9/10/11/13/14/15 都带尺寸。"""
    i = 2                                   # 跳过 SOI (FFD8)
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # 填充字节 FF 允许重复，跳过
        if marker == 0xFF:
            i += 1
            continue
        # 无参数的段：SOI/EOI/RSTn/TEM
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h)
        i += 2 + seg_len
    return None


def read_image_bounded(path, max_bytes):
    """有界读一张图片。返回 (bytes, None) 或 (None, 错误文本)。

    借 dsh read_image 的两条实现细节：
      ① stat 先短路 —— 明显超限的文件根本不去打开它，省一次 I/O，
         也避免在 400MB 的文件上白读一遍。
      ② 读了 limit+1 个字节来判超限 —— 防「stat 之后文件又长大了」这个
         竞态。只看 stat 就放行的话，两行代码之外文件可能已经被写大。
    """
    try:
        st = path.stat()
    except OSError as e:
        return None, f"{VISION_ERR_FORMAT} 读不到这个文件（{e.strerror or e}）"
    if st.st_size > max_bytes:
        return None, (f"{VISION_ERR_TOO_BIG} 图片 {DEPS.human_size(st.st_size)}，"
                      f"超过上限 {DEPS.human_size(max_bytes)}")
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)     # 多读一个字节，见 ②
    except OSError as e:
        return None, f"{VISION_ERR_FORMAT} 读取失败（{e.strerror or e}）"
    if len(data) > max_bytes:
        return None, (f"{VISION_ERR_TOO_BIG} 图片超过上限 "
                      f"{DEPS.human_size(max_bytes)}")
    if not data:
        return None, f"{VISION_ERR_FORMAT} 文件是空的（0 字节）"
    return data, None


class VisionClient:
    """独立视觉端点客户端。**刻意不复用 LLMClient**。

    不复用的三个理由：
      · 配置来源不同（vision 段 vs model 段），复用就得把两套配置搅在一起；
      · 请求形态不同：视觉转述只发一条 user 消息、不带工具、不流式，
        而 LLMClient 背着 reasoning / tools / 流式 / 工具调用累积这一整套；
      · 失败语义不同：转述失败不该影响主对话，需要独立的短超时与错误前缀。
    重合的只有「往 /chat/completions 发个 JSON」这十几行，不值得为此耦合。
    """

    def __init__(self, cfg):
        # 走 vision_endpoint_url 而不是裸拼：用户把完整端点粘进「接口地址」是
        # 常态，裸拼会让路径重复成 …/chat/completions/chat/completions。
        self.url = vision_endpoint_url(cfg.get("api_base", ""))
        self.key = cfg.get("api_key", "") or ""
        self.model = str(cfg.get("model", "") or "")
        # 超时用 vision 段的短值（默认 60），不跟随主模型的 120：
        # 转述是附带动作，不该把整轮对话拖住。
        try:
            self.timeout = max(10, int(vision_cfg("request_timeout", 60) or 60))
        except (TypeError, ValueError):
            self.timeout = 60

    def describe(self, image_part, question="", max_tokens=800):
        """发一次转述请求。返回 (ok, text)——失败时 text 已带错误前缀。"""
        if not self.url or not self.model:
            return False, f"{VISION_ERR_CONFIG} 视觉端点没配全（需要 api_base + model）"
        prompt = (question or "").strip() or VISION_DEFAULT_PROMPT
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                image_part,
            ]}],
            "max_tokens": int(max_tokens),
            "temperature": 0.3,      # 转述要稳，不要发挥
            # 刻意不带 tools / stream：视觉端点只需要回一段文字
        }
        url = self.url                 # 已在 __init__ 规范化，见 vision_endpoint_url
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status = r.getcode()
                body = r.read()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode(errors="replace")[:400]
            except Exception:
                detail = ""
            return False, (f"{VISION_ERR_NETWORK} 视觉端点返回 HTTP {e.code}"
                           + (f"：{detail}" if detail else ""))
        except Exception as e:
            return False, f"{VISION_ERR_NETWORK} 请求视觉端点失败（{e}）"
        if status != 200:
            return False, f"{VISION_ERR_NETWORK} 视觉端点返回 HTTP {status}"
        try:
            obj = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return False, f"{VISION_ERR_FORMAT} 视觉端点返回的不是 JSON"
        choices = obj.get("choices") or []
        if not choices:
            return False, f"{VISION_ERR_FORMAT} 视觉端点没返回 choices"
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if isinstance(text, list):
            # 少数端点回 content 数组，拼成纯文本
            text = "".join(str(p.get("text") or "")
                           for p in text if isinstance(p, dict))
        text = (text or "").strip()
        if not text:
            # 空回复是视觉端点的常见失败形态（图太大 / 被内容策略挡掉），
            # 明说出来比返回空串好：空串会被当成「她什么都没看见」，
            # 而这句话能让她知道该换个说法或换张图。
            return False, f"{VISION_ERR_FORMAT} 视觉端点返回了空描述（图可能太大或不支持这种格式）"
        return True, text


def vision_max_bytes():
    """本次允许进视觉通道的图片字节上限。

    取 vision.maxMb 与 DEPS.max_file_bytes() 的**较小值**：前者是用户偏好，
    后者是这台设备上已经验证过的硬边界（4 MiB，与 base64 请求体大小挂钩），
    用户把 maxMb 调到 16 也不该突破后者。
    """
    try:
        mb = float(vision_cfg("maxMb", 4) or 4)
    except (TypeError, ValueError):
        mb = 4.0
    return int(min(mb * 1024 * 1024, DEPS.max_file_bytes()))


def caption_image(source, question="", cfg=None):
    """把一张图交给独立视觉模型转述成文字。**唯一的多模态协议层**。

    source 两种形态（对齐桌面端 read_image / read_image_url 的分工）：
      {"path": Path}  → 本地文件：有界读 → 魔数校验 → base64 data URI
      {"url": str}    → 公网地址：原样直传，厂商服务器自己拉图，
                        本机不下载、不转 base64（省流量省内存）

    返回**永远是字符串**：成功是描述正文，失败是带 "[错误·xxx]" 前缀的说明。
    这个「不抛异常、只回文本」的契约，是为了让工具 handler 与上传链路都能
    直接把返回值塞进上下文 —— 出错时她也能如实告诉用户看不了，而不是
    整轮对话因为一次转述失败而炸掉。

    cfg 传入 (ok, config_or_error) 形态的解析结果；不传则自己解析。
    """
    global VISION_LAST
    if cfg is None:
        cfg = resolve_caption_vision_config()
    ok, conf = cfg
    if not ok:
        VISION_LAST = {"ok": False, "at": time.time(), "error": str(conf)[:300]}
        return str(conf)

    max_bytes = vision_max_bytes()
    local_hash = ""
    meta = []

    if isinstance(source, dict) and source.get("path") is not None:
        path = Path(source["path"])
        data, err = read_image_bounded(path, max_bytes)
        if err:
            VISION_LAST = {"ok": False, "at": time.time(), "error": err[:300]}
            return err
        sniffed = sniff_image_type(data)
        if sniffed is None:
            msg = (f"{VISION_ERR_FORMAT} 这不是能识别的图片格式"
                   f"（只认 PNG / JPEG / WebP / GIF / BMP）")
            VISION_LAST = {"ok": False, "at": time.time(), "error": msg[:300]}
            return msg
        mime, ext = sniffed
        # 扩展名与真实类型不符时报出来 —— 对齐 dsh「不匹配时按改名修复提示
        # 失败关闭，而不是被静默接受」的取舍。这里不阻断（图照样能转述），
        # 但在返回文本里点一句，让「扩展名骗人」这类问题不再靠人肉发现。
        claimed = path.suffix.lower().lstrip(".")
        if claimed and claimed not in (ext, "jpeg" if ext == "jpg" else ext):
            meta.append(f"（注意：扩展名是 .{claimed}，实际内容是 {ext}）")
        dims = image_dimensions(data, mime)
        if dims:
            meta.append(f"{dims[0]}×{dims[1]}")
        meta.append(DEPS.human_size(len(data)))
        local_hash = hashlib.sha256(data).hexdigest()
        b64 = base64.b64encode(data).decode("ascii")
        image_part = {"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{b64}"}}

    elif isinstance(source, dict) and source.get("url"):
        url = str(source["url"]).strip()
        if not re.match(r"^https?://", url, re.I):
            msg = f"{VISION_ERR_FORMAT} url 必须以 http:// 或 https:// 开头"
            VISION_LAST = {"ok": False, "at": time.time(), "error": msg[:300]}
            return msg
        # URL 直传：本机不下载。这也是桌面端 read_image_url 的做法。
        image_part = {"type": "image_url", "image_url": {"url": url}}

    else:
        msg = f"{VISION_ERR_FORMAT} 缺少图片来源（path 或 url）"
        VISION_LAST = {"ok": False, "at": time.time(), "error": msg[:300]}
        return msg

    # 缓存查：key 必须带 question —— 同一张图不同问题的描述不同。
    # 本地图用内容哈希（比路径准：同一张图换个文件名仍是同一张），
    # url 用地址本身，与桌面端一致。
    cache_key = (local_hash or image_part["image_url"]["url"]) + "||" + (question or "")
    hit = vision_cache_get(cache_key)
    if hit:
        ts, text = hit
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        mins = max(1, _vision_cache_ttl() // 60)
        VISION_LAST = {"ok": True, "at": ts, "cached": True, "chars": len(text)}
        return f"[缓存] 此描述生成于 {stamp}，{mins} 分钟内复用\n\n{text}"

    client = VisionClient(conf)
    ok2, text = client.describe(image_part, question)
    if not ok2:
        VISION_LAST = {"ok": False, "at": time.time(), "error": text[:300]}
        return text
    # 只有成功描述才写缓存（错误不缓存，下次重试）
    vision_cache_put(cache_key, text)
    VISION_LAST = {"ok": True, "at": time.time(), "cached": False, "chars": len(text)}
    if meta:
        # 元数据放在描述前面：她先知道「这是什么图、多大」，再读内容，
        # 判断「图看不清」这类结论时才有依据。
        return "· ".join(meta) + "\n\n" + text
    return text


# ---------- handler ----------
# web_search 的 handler 已随搜索子系统搬去 cyrene_search.py，
# 由文件上部的转发层绑回 _h_web_search，TOOL_HANDLERS 那边不用动。
# 这里只剩抓取与下载两个。


VISION_RANGES = {
    "request_timeout": (10, 300, 60),
    "maxMb": (1, 16, 4),
    "cacheTtlMin": (0, 1440, 30),
}


def normalize_vision(raw):
    """归一化独立视觉模型配置。

    这个函数与 normalize_plugins 的差别：这里全是**偏好设置**，不是用户数据，
    所以走白名单式的回落语义 —— 缺项回落默认值，非法值夹回合法范围。
    （plugins 段的 registry / secrets 反过来，绝不能回落，回落就是清空。）
    """
    v = raw if isinstance(raw, dict) else {}
    out = {
        "enabled": DEPS.as_bool(v.get("enabled"), False),
        "autoCaption": DEPS.as_bool(v.get("autoCaption"), False),
        "toolEnabled": DEPS.as_bool(v.get("toolEnabled"), True),
        # api_base 去掉尾斜杠：拼 "/chat/completions" 时少一次踩坑
        # （与 model.api_base 同一套处理）。
        "api_base": DEPS.as_str(v.get("api_base"), "").strip().rstrip("/")[:500],
        # api_key 不做长度截断 —— 截断会悄悄废掉一个 key，
        # 用户看到的是「鉴权失败」而不是「配置被截断了」。
        # 与 plugin secrets 的处理口径一致。
        "api_key": DEPS.as_str(v.get("api_key"), ""),
        "model": DEPS.as_str(v.get("model"), "")[:200],
    }
    for k, (lo, hi, fb) in VISION_RANGES.items():
        # 整数键：maxMb 允许小数（1.5 MB 这种），但三个里只有它可能带小数，
        # 统一按 float 夹再按需取整更省心 —— 这里全部保留数值语义，
        # 前端展示与比较都不受影响。
        n = DEPS.clamp(v.get(k), lo, hi, fb)
        out[k] = int(n) if k in ("request_timeout", "cacheTtlMin") else round(n, 2)
    return out


def vision_cfg(key, fb=None):
    """读单个视觉配置项。DEPS.settings() 未加载时回落默认值（与 plugins_cfg 同构）。

    为什么要这个包装而不是到处 DEPS.settings().get("vision", {}).get(...)：
    DEPS.settings() 是模块级全局，在 load_settings() 跑之前访问会 NameError。
    视觉子系统可能被早期调用（比如工具注册、路由判定），这层保护是必要的。
    """
    try:
        return DEPS.settings().get("vision", {}).get(key, fb)
    except (NameError, AttributeError):
        return fb


def vision_ready():
    """视觉通道是否可用：开关开着 + 三个端点字段齐（对齐桌面端三项齐全判据）。

    只在「配置齐不齐」这一层下结论，不碰任何网络。真正的可用性由第一次
    调用去证伪 —— 首页判定只负责把「肯定不行」的情况挡在门外。
    """
    if not vision_cfg("enabled", False):
        return False
    base = str(vision_cfg("api_base", "") or "").strip()
    key = str(vision_cfg("api_key", "") or "").strip()
    model = str(vision_cfg("model", "") or "").strip()
    return bool(base and key and model)


# 视觉工具 id。单独拎出来，是因为「给不给这两个工具」的判定要同时作用于
# 三处：系统提示的工具清单（enabled_tools）、请求里的 tools 数组
# （enabled_tool_names）、以及 /tools 面板的展示。写死字面量迟早会漏一处。
VISION_TOOL_IDS = ("read_image", "read_image_url")


def vision_tools_allowed():
    """要不要把视觉工具摆给她。

    判据只有 vision.toolEnabled 这一条（默认 True），**不要求端点已配好** ——
    这是刻意的：端点没定时她仍能看到这两个工具，调了会拿到「请在设置 → 视觉
    里配置」的可读提示。这比「工具时有时无」好：用户刚打开面板时能看见工具，
    才知道有这么个能力存在，也才会去配。
    """
    return bool(vision_cfg("toolEnabled", True))


