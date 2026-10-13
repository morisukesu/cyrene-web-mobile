# -*- coding: utf-8 -*-
"""cyrene_tts —— 语音合成子系统（`cyrene_web` 拆出来的第五块）。

它原来是什么
------------
「让昔涟出声」的那两条嗓子，加一层气口增强：

  引擎分派     `tts_speak` 按配置在 system / minimax / custom 之间选
  system       `termux-tts-speak`，手机自带、离线、免费，阻塞播完才返回
  minimax      云端合成（HTTP 版 v1/t2a_v2），音色与桌面端一致
  custom       自定义云端接口，约定对齐桌面端 custom-cloud-engine
  气口增强     把「哈哈」「嗯…」这类语气词补成 (laughs)/(emm) 标记，
               规则照桌面端 minimax-vocal-enhancer.js 搬的；整段最多补 2 个
  音频缓存     `data/tts/` 下按 (引擎 + 参数 + 文本) 哈希存一份，同一句不重复花钱

它依赖宿主什么
--------------
由宿主经 `bind_deps()` 注入：

  · `settings()`    配置读取（取值函数，SETTINGS 换版也读得到新的）
  · `data_dir()`    音频缓存根目录（取值函数，同上）
  · `clamp()`       区间归位（customTimeoutMs 之类）

⚠ 内核没起来时这三个都有内置兜底，模块加载期不会炸。但 `settings()` 一旦
  是兜底的**空 dict**，引擎会静默退回 system —— 用户配好的 minimax 不会生效。
  所以 `deps_ready()` 把 settings / data_dir 列为必需项，启动自检要盯。

红线同前：**绝不 import cyrene_web**，方向只有「宿主 → 内核 → 子系统」。
"""

from pathlib import Path

# 兜底基准目录：与宿主的 BASE_DIR 同址（cyrene_mobile/），这样内核没起来时
# 缓存也落在同一个 data/ 下，不会在旁边长出第二个缓存树。
_BASE_DIR = Path(__file__).parent.parent

import base64
import hashlib
import json
import re
import subprocess
import threading
import urllib.error
import urllib.request


__all__ = [
    "TTS_PROC", "TTS_LOCK", "MINIMAX_T2A_URL",
    "MAX_VOCAL_TAGS", "MINIMAX_VOCAL_TAGS", "MINIMAX_VOCAL_RULES",
    "enhance_minimax_text", "tts_engine", "tts_speak", "tts_stop",
    "bind_deps", "deps_ready", "DEPS", "PLUGIN",
]


def _def_settings():
    """配置还没注入时的空配置：模块加载期不至于炸。"""
    return {}


def _def_data_dir():
    """缓存目录兜底：与宿主的 BASE_DIR/data 同址。"""
    return _BASE_DIR / "data"


def _def_clamp(v, lo, hi, fb):
    """`_clamp` 的兜底实现，语义与宿主那份一致（含 NaN 归 fb）。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return fb
    if n != n:                      # NaN
        return fb
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


class _Deps:
    """宿主注入的资源袋。"""

    __slots__ = ("settings", "data_dir", "clamp")

    def __init__(self):
        self.settings = _def_settings
        self.data_dir = _def_data_dir
        self.clamp = _def_clamp

    def missing(self):
        # 这两个必须换成宿主的实时取值函数：settings 决定走哪条嗓子，
        # data_dir 决定音频缓存落在哪。留着兜底就等于「静默按默认值跑」，
        # 不报错但行为全变 —— 所以列进必需项。
        # clamp 有内置兜底，缺了不会走偏，不列。
        need = ("settings", "data_dir")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()
_FRESH_DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入。未知的键直接报错，不静默吞掉。"""
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_tts.bind_deps 不认识这些参数：%s"
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
    """内核在 config / tools / data 三个服务齐了之后调这里。"""
    cfg = ctx.require("config")
    tools = ctx.require("tools")
    data = ctx.require("data")

    bind_deps(
        settings=cfg,
        data_dir=data,
        clamp=tools.clamp,
    )
    ctx.effect(_reset_deps, desc="tts:deps")
    ctx.log("已接到内核（依赖 config / tools / data）")


PLUGIN = {
    "id": "tts",
    "requires": ["config", "tools", "data"],
    "setup": _plugin_setup,
}


# ===========================================================================
# 以下正文由 _make_tts_module.py 从 cyrene_web.py 逐字抽出
# ===========================================================================


# ========== TTS ==========
# 两条嗓子：
#   system  —— termux-tts-speak，手机自带的，离线、免费、阻塞式播完才返回
#   minimax —— 云端合成，音色跟桌面端一致（要填自己的 API Key）
# 桌面端那条走的是 WebSocket；这里用它的 HTTP 版 v1/t2a_v2，
# 纯 urllib 就能调，省得为了 TTS 再引一个 websocket 依赖。
# 合成好的 mp3 按 (引擎+文本) 缓存一份，同一句话不重复花钱。
TTS_PROC = None
TTS_LOCK = threading.Lock()

MINIMAX_T2A_URL = "https://api.minimaxi.com/v1/t2a_v2"

# ---- 气口增强：规则照桌面端 minimax-vocal-enhancer.js 搬的 ----
# 往文本里补 (laughs)/(emm)/(sighs)/(breath) 这类标记，让语气有停顿。
MAX_VOCAL_TAGS = 2
MINIMAX_VOCAL_TAGS = (
    "(laughs)", "(chuckle)", "(coughs)", "(clear-throat)", "(groans)",
    "(breath)", "(pant)", "(inhale)", "(exhale)", "(gasps)", "(sniffs)",
    "(sighs)", "(snorts)", "(burps)", "(lip-smacking)", "(humming)",
    "(hissing)", "(emm)", "(sneezes)",
)
# (正则, 气口, 插在词前还是词后, 这段最多几个, 是否只在句末触发)
MINIMAX_VOCAL_RULES = (
    (re.compile(r"(?<![（(])哈{2,}(?![）)])"), "(laughs)", "after", 1, False),
    (re.compile(r"(?<![（(])嘿{2,}(?![）)])"), "(chuckle)", "after", 1, False),
    (re.compile(r"(?<![（(])嗯[~….]{0,3}(?![）)])"), "(emm)", "before", 1, False),
    (re.compile(r"(?<![a-zA-Z（(])emm+m*[.…]*", re.I), "(emm)", "before", 1, False),
    (re.compile(r"(?<![（(])啊(?![）)])"), "(gasps)", "before", 1, False),
    (re.compile(r"(?<![（(])唉(?![）)])"), "(sighs)", "before", 1, False),
    (re.compile(r"(?<![（(])哎(?![）)])"), "(sighs)", "before", 1, False),
    (re.compile(r"(?:请看下面的代码块|代码如下|见下表|如下所示|如下表所示)[:：]?\s*$"),
     "(breath)", "after", 1, True),
    (re.compile(r"[.…]{2,}\s*$"), "(sighs)", "after", 1, True),
)


def _has_vocal_tag_near(text, index, direction):
    """前后 20 个字里已经有气口标记，就别再叠一个。"""
    seg = text[max(0, index - 20):index] if direction == "before" else text[index:index + 20]
    return any(tag in seg for tag in MINIMAX_VOCAL_TAGS)


def enhance_minimax_text(text, enabled=True):
    """给文本补气口标记。整段最多补 MAX_VOCAL_TAGS 个。"""
    if not enabled or not text:
        return text
    out = text
    total = 0
    for rx, tag, pos, per, tail_only in MINIMAX_VOCAL_RULES:
        if total >= MAX_VOCAL_TAGS:
            break
        if tail_only and not rx.search(out):
            continue
        applied, start = 0, 0
        while applied < per and total < MAX_VOCAL_TAGS:
            m = rx.search(out, start)
            if not m:
                break
            idx = m.start() if pos == "before" else m.end()
            if _has_vocal_tag_near(out, idx, pos):
                start = m.start() + max(1, m.end() - m.start())
                continue
            out = out[:idx] + tag + out[idx:]
            applied += 1
            total += 1
            start = idx + len(tag)
    return out


def tts_engine():
    """现在用哪条嗓子。"""
    eng = str((DEPS.settings().get("tts") or {}).get("engine") or "system").strip().lower()
    if eng in ("minimax", "custom"):
        return eng
    return "system"


def _tts_cache_path(tag, text, ext="mp3"):
    h = hashlib.sha1((tag + "\0" + text).encode("utf-8")).hexdigest()[:20]
    d = DEPS.data_dir() / "tts"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d / (h + "." + ext)


def _minimax_synth(text, cfg):
    """调 MiniMax 合成一段 mp3。返回 (bytes, 错误原因)，成功时错误为空串。"""
    key = str(cfg.get("minimaxKey") or "").strip()
    if not key:
        return None, "还没填 MiniMax 的 API Key（设置 → 语音）"
    voice = str(cfg.get("minimaxVoiceId") or "").strip()
    if not voice:
        return None, "还没填音色 ID（设置 → 语音）"
    payload = {
        "model": str(cfg.get("minimaxModel") or "").strip() or "speech-2.8-hd",
        "text": enhance_minimax_text(
            text, bool(cfg.get("minimaxVocalEnhance", True))),
        "stream": False,
        "voice_setting": {
            "voice_id": voice,
            "speed": float(cfg.get("minimaxSpeed") or 1.0),
            "vol": float(cfg.get("minimaxVolume") or 1.0),
            # pitch 必须是整数，传 0.0 这种浮点会被 API 顶回来（2013）
            "pitch": int(round(float(cfg.get("minimaxPitch") or 0))),
            "english_normalization": False,
        },
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000,
                          "format": "mp3", "channel": 1},
    }
    req = urllib.request.Request(
        MINIMAX_T2A_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return None, "MiniMax 返回 %s：%s" % (e.code, body)
    except Exception as e:
        return None, "连不上 MiniMax：%s" % e
    base = data.get("base_resp") or {}
    code = base.get("status_code")
    if code not in (0, None):
        return None, "MiniMax 报错 %s：%s" % (code, base.get("status_msg") or "")
    hex_audio = (data.get("data") or {}).get("audio") or ""
    if not hex_audio:
        return None, "MiniMax 没返回音频（这段话可能被内容策略挡了）"
    try:
        return bytes.fromhex(hex_audio), ""
    except ValueError:
        return None, "返回的音频不是合法 hex"


def _play_audio_file(path):
    """用 termux-media-player 放一个音频文件。它是后台播的，不等播完。"""
    try:
        subprocess.run(["termux-media-player", "stop"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        p = subprocess.run(["termux-media-player", "play", str(path)],
                           capture_output=True, timeout=20)
    except FileNotFoundError:
        return False, "termux-media-player 不存在（要装 Termux:API）"
    except Exception as e:
        return False, str(e)
    if p.returncode != 0:
        detail = (p.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return False, detail or ("exit %d" % p.returncode)
    return True, "ok"


def _tts_speak_minimax(text, cfg):
    # 语速、音量也进 key —— 不然调了语速而句子没变，命中的还是旧那版音频
    tag = "minimax:%s:%s:%s:%s:%s:%s" % (
        cfg.get("minimaxVoiceId") or "", cfg.get("minimaxModel") or "",
        cfg.get("minimaxSpeed") or 1.0, cfg.get("minimaxVolume") or 1.0,
        cfg.get("minimaxPitch") or 0, bool(cfg.get("minimaxVocalEnhance", True)))
    cache = _tts_cache_path(tag, text)
    try:
        if cache.is_file() and cache.stat().st_size > 0:
            return _play_audio_file(cache)
    except OSError:
        pass
    audio, err = _minimax_synth(text, cfg)
    if audio is None:
        return False, err
    try:
        cache.write_bytes(audio)
    except OSError:
        pass
    return _play_audio_file(cache)


def _custom_synth(text, cfg):
    """调自定义云端接口。返回 (音频字节, 扩展名, 错误原因)。

    约定跟桌面端 custom-cloud-engine 一致：
      POST <接口地址>   Content-Type: application/json
      Key 非空时带 Authorization: Bearer <key>
      body: {text, voiceId?, speed, volume, format}
      响应：直接给音频字节，或者 application/json 里给 audioBase64。
    """
    url = str(cfg.get("customEndpointUrl") or "").strip()
    if not url:
        return None, "", "还没填自定义云端的接口地址（设置 → 语音）"
    fmt = "wav" if str(cfg.get("customFormat") or "").strip().lower() == "wav" else "mp3"
    timeout = DEPS.clamp(cfg.get("customTimeoutMs"), 5, 300, 30)
    body = {
        "text": text,
        "speed": float(cfg.get("customSpeed") or 1.0),
        "volume": float(cfg.get("customVolume") or 1.0),
        "format": fmt,
    }
    voice = str(cfg.get("customVoiceId") or "").strip()
    if voice:
        body["voiceId"] = voice
    headers = {"Content-Type": "application/json"}
    key = str(cfg.get("customApiKey") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return None, "", "接口返回 %s：%s" % (e.code, detail)
    except Exception as e:
        return None, "", "请求失败：%s" % e
    if "json" in ctype:
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None, "", "接口说是 JSON，但解析不了"
        b64 = d.get("audioBase64")
        if not b64:
            return None, "", "JSON 里没有 audioBase64"
        try:
            audio = base64.b64decode(b64)
        except Exception:
            return None, "", "audioBase64 解不开"
        got = str(d.get("format") or "").strip().lower()
        if got in ("wav", "mp3"):
            fmt = got
    else:
        audio = raw
        if "wav" in ctype or "wave" in ctype:
            fmt = "wav"
        elif "mpeg" in ctype or "mp3" in ctype:
            fmt = "mp3"
    if not audio:
        return None, "", "接口返回了空音频"
    return audio, fmt, ""


def _tts_speak_custom(text, cfg):
    tag = "custom:%s:%s:%s:%s:%s" % (
        cfg.get("customEndpointUrl") or "", cfg.get("customVoiceId") or "",
        cfg.get("customSpeed") or 1.0, cfg.get("customVolume") or 1.0,
        cfg.get("customFormat") or "mp3")
    fmt = "wav" if str(cfg.get("customFormat") or "").strip().lower() == "wav" else "mp3"
    cache = _tts_cache_path(tag, text, fmt)
    try:
        if cache.is_file() and cache.stat().st_size > 0:
            return _play_audio_file(cache)
    except OSError:
        pass
    audio, got, err = _custom_synth(text, cfg)
    if audio is None:
        return False, err
    if got != fmt:
        cache = _tts_cache_path(tag, text, got)
    try:
        cache.write_bytes(audio)
    except OSError:
        pass
    return _play_audio_file(cache)


def _tts_speak_system(text, cfg):
    """手机自带的那条嗓子，阻塞播完才返回。"""
    global TTS_PROC
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
    cmd.append(text[:800])
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


def tts_speak(text):
    """念一段话。按设置里的引擎分派，返回 (ok, message)。"""
    t = str(text or "").strip()
    if not t:
        return False, "empty"
    cfg = DEPS.settings().get("tts") or {}
    eng = tts_engine()
    if eng == "minimax":
        return _tts_speak_minimax(t[:1500], cfg)
    if eng == "custom":
        return _tts_speak_custom(t[:1500], cfg)
    return _tts_speak_system(t, cfg)


def tts_stop():
    """停掉正在念的。两条嗓子的停法不一样。"""
    global TTS_PROC
    if tts_engine() in ("minimax", "custom"):
        try:
            subprocess.run(["termux-media-player", "stop"],
                           capture_output=True, timeout=5)
            return True
        except Exception:
            return False
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
