#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
昔涟 · 手机版 Agent 运行时
从 E:\\Cyrene 移植，Termux 原生运行
"""
import os, sys, json, re, time, subprocess, urllib.request, urllib.error
from pathlib import Path

# ========== 配置 ==========
BASE_DIR    = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"
SKILLS_DIR  = BASE_DIR / "skills"
HISTORY_FILE = BASE_DIR / ".history.json"
CONFIG_FILE  = BASE_DIR / ".config.json"

DEFAULT_CONFIG = {
    "api_base": "https://api.openai.com/",
    "api_key": "",
    "model": "gpt-4o-mini",
    "max_history": 30,
}

# ========== 工具定义（Termux 原生） ==========
TOOLS = {
    "battery": {
        "desc": "查手机电量",
        "cmd": ["termux-battery-status"],
    },
    "camera": {
        "desc": "拍照保存到相册",
        "cmd": ["termux-camera-photo", "-c", "0", "/sdcard/DCIM/cyrene_{ts}.jpg"],
    },
    "tts": {
        "desc": "让手机说话",
        "cmd": ["termux-tts-speak", "{text}"],
    },
    "notify": {
        "desc": "发通知",
        "cmd": ["termux-notification", "-t", "昔涟", "-c", "{text}"],
    },
    "vibrate": {
        "desc": "震动",
        "cmd": ["termux-vibrate"],
    },
    "torch": {
        "desc": "开关手电筒",
        "cmd": ["termux-torch", "{on_off}"],
    },
    "location": {
        "desc": "查 GPS 定位",
        "cmd": ["termux-location"],
    },
    "clipboard_set": {
        "desc": "写剪贴板",
        "cmd": ["termux-clipboard-set", "{text}"],
    },
    "clipboard_get": {
        "desc": "读剪贴板",
        "cmd": ["termux-clipboard-get"],
    },
    "wifi_info": {
        "desc": "查 WiFi 连接信息",
        "cmd": ["termux-wifi-connectioninfo"],
    },
    "brightness": {
        "desc": "调亮度 (0-255)",
        "cmd": ["termux-brightness", "{value}"],
    },
    "media_play": {
        "desc": "播放音频文件",
        "cmd": ["termux-media-player", "play", "{path}"],
    },
    "media_stop": {
        "desc": "停止播放",
        "cmd": ["termux-media-player", "stop"],
    },
    "shell": {
        "desc": "执行任意 shell 命令（谨慎）",
        "cmd": ["sh", "-c", "{cmd}"],
    },
}

TOOL_NAMES = list(TOOLS.keys())

# ========== Prompt 组装 ==========
def load_prompt(name: str) -> str:
    p = PROMPTS_DIR / name
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""

def build_system_prompt() -> str:
    soul    = load_prompt("soul.md")
    chat_id = load_prompt("chat_identity.md")
    chat_sys= load_prompt("chat_system.md")
    phone_sys = load_prompt("phone_system.md")
    phone_style = load_prompt("phone_style.md")
    quotes  = load_prompt("canon_quotes_lite.md")

    tool_desc_lines = []
    for name, t in TOOLS.items():
        tool_desc_lines.append(f"- {name}: {t['desc']}")
    tools_block = "\n".join(tool_desc_lines)

    sys_prompt = f"""你是昔涟，正在通过 Termux 终端与用户文字交流。

=== 人格核心 (soul.md) ===
{soul}

=== 身份 ===
{chat_id}

=== 系统规则 ===
{chat_sys}

=== 通话规则（如适用） ===
{phone_sys}

=== 语气参考 ===
{quotes}

=== 可用工具 ===
你是手机上的 AI 助手，可以调用以下工具。想调用时在回复末尾单独一行写：
[TOOL] <工具名> [参数]

{tools_block}

规则：
- 每次最多调用一个工具
- 参数用空格分隔，文本参数加引号
- 调用后结果会注入下一轮对话
- 不需要工具就直接回复，不要加 [TOOL] 行

称呼他的方式：用自然的称呼，不要用「用户」「对方」这类生硬的说法。
"""

    return sys_prompt

# ========== LLM 调用 ==========
class LLMClient:
    def __init__(self, cfg):
        self.base = cfg["api_base"].rstrip("/")
        self.key  = cfg["api_key"]
        self.model = cfg["model"]

    def chat(self, messages, temperature=0.7, max_tokens=2000):
        url = self.base + "/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return f"[API 错误 {e.code}] {body[:500]}"
        except Exception as e:
            return f"[网络错误] {e}"

# ========== 工具执行 ==========
def run_tool(tool_line: str) -> str:
    parts = tool_line.strip().split(None, 1)
    if not parts:
        return "空工具调用"
    name = parts[0]
    args_str = parts[1] if len(parts) > 1 else ""

    if name not in TOOLS:
        return f"未知工具: {name}"

    t = TOOLS[name]
    raw_cmd = t["cmd"]

    # 解析参数（简单空格分隔，引号包文本）
    args = []
    if args_str:
        # 简单引号解析
        tokens = re.findall(r'"([^"]*)"|(\S+)', args_str)
        args = [t[0] or t[1] for t in tokens]

    # 替换占位符
    cmd = []
    for seg in raw_cmd:
        seg = seg.replace("{ts}", str(int(time.time())))
        seg = seg.replace("{text}", args[0] if args else "")
        seg = seg.replace("{on_off}", args[0] if args else "on")
        seg = seg.replace("{value}", args[0] if args else "128")
        seg = seg.replace("{path}", args[0] if args else "")
        seg = seg.replace("{cmd}", args_str)
        cmd.append(seg)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip() or result.stderr.strip()
        return out[:1000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "(工具超时)"
    except Exception as e:
        return f"(执行失败: {e})"

# ========== 工具调用解析 ==========
TOOL_PATTERN = re.compile(r'\[TOOL\]\s*(\S+)(.*)', re.DOTALL)

def extract_tool_call(response: str):
    """从回复末尾提取 [TOOL] 行"""
    m = TOOL_PATTERN.search(response)
    if m:
        tool_line = m.group(1) + m.group(2)
        clean_resp = response[:m.start()].rstrip()
        return clean_resp, tool_line.strip()
    return response, None

# ========== 历史 ==========
def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return []

def save_history(msgs):
    HISTORY_FILE.write_text(json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8")

# ========== 配置 ==========
def load_config():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            # merge defaults
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

# ========== 主循环 ==========
def main():
    print("=" * 40)
    print("  昔涟 · 手机版")
    print("  Termux Agent Runtime")
    print("=" * 40)

    cfg = load_config()
    if not cfg.get("api_key"):
        print("\n首次运行，请配置 API Key：")
        key = input("API Key > ").strip()
        if key:
            cfg["api_key"] = key
            save_config(cfg)
        else:
            print("未配置 API Key，退出。")
            return

    client = LLMClient(cfg)
    system_prompt = build_system_prompt()

    msgs = load_history()
    if msgs and len(msgs) > 0:
        print(f"\n(已恢复 {len(msgs)} 条历史)")

    print(f"\n模型: {cfg['model']}")
    print(f"API: {cfg['api_base']}")
    print("输入 exit 退出，输入 /clear 清空历史\n")

    while True:
        try:
            user = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见啦♪")
            break

        if not user:
            continue
        if user.lower() in ("exit", "quit"):
            print("再见啦♪")
            break
        if user == "/clear":
            msgs = []
            save_history(msgs)
            print("(历史已清空)")
            continue
        if user == "/config":
            print(json.dumps(cfg, ensure_ascii=False, indent=2))
            continue

        msgs.append({"role": "user", "content": user})

        # 控制历史长度
        if len(msgs) > cfg["max_history"] * 2:
            msgs = msgs[-cfg["max_history"]:]

        full_msgs = [{"role": "system", "content": system_prompt}] + msgs

        print("昔涟 > ", end="", flush=True)
        resp = client.chat(full_msgs)
        print(resp)

        # 提取工具调用
        clean_resp, tool_line = extract_tool_call(resp)
        if tool_line:
            print(f"  ⚙️  调用: {tool_line}")
            result = run_tool(tool_line)
            print(f"  📎 结果: {result[:300]}")
            msgs.append({"role": "assistant", "content": clean_resp})
            msgs.append({"role": "user", "content": f"[工具结果] {result}"})
            # 让 LLM 基于工具结果再回一句
            full_msgs2 = [{"role": "system", "content": system_prompt}] + msgs
            print("昔涟 > ", end="", flush=True)
            resp2 = client.chat(full_msgs2, max_tokens=500)
            print(resp2)
            msgs.append({"role": "assistant", "content": resp2})
        else:
            msgs.append({"role": "assistant", "content": resp})

        save_history(msgs)

if __name__ == "__main__":
    main()
