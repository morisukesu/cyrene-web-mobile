<div align="center">

# 昔涟 · 手机版 Web Agent

**在 Android / Termux 上跑起来的本地 AI Agent Web 服务**

四模式对话 · 26 个设备/文件/联网工具 · 原生 Function Calling 多轮 Agent Loop · 技能按需加载

作者 **森亞ミミカ** · MIT License

</div>

---

## 这是什么

把「昔涟」这个 AI 助手做成一个**纯 Python 单文件 Web 服务**，跑在安卓手机的 Termux 里。
手机浏览器打开一个局域网地址，就能和一个能调用手机硬件、读写文件、联网搜索的多轮 Agent 对话。

- **零第三方依赖**：后端只用 Python 标准库（`http.server` / `urllib` / `threading`），Termux 自带的 python 就能跑。
- **真·多轮 Agent**：原生 Function Calling 驱动的 Agent Loop，一轮里可并行调用多个工具，工具结果回灌继续推理。
- **四种对话模式**：Chat / Work / Code / Learn，各自独立的系统提示词与工具策略。
- **手机硬件工具**：电量、手电筒、通知、震动、剪贴板、WiFi、TTS、拍照、定位等（需 Termux:API）。
- **文件与联网工具**：读写编辑文件、glob/grep 搜索、web_search、fetch_url、download_file。
- **技能系统**：`skills/` 下的技能以「清单」形式注入，正文按需通过 `invoke_skill` 拉取，省 token。
- **本地托管前端**：marked + highlight.js 全部本地化，不依赖 CDN，离线可用。

## 项目结构

```
cyrene-web-mobile/
├── cyrene_mobile/
│   ├── runtime/
│   │   ├── cyrene_web.py      # 后端主程序（单文件，HTTP 服务 + Agent Loop + 工具）
│   │   ├── cyrene_phone.py    # 早期 CLI 原型（终端对话，保留作参考）
│   │   └── static/            # 前端：app.js / app.css / settings.css + marked / highlight.js
│   ├── prompts/               # 四模式系统提示词、人格、世界书、技能纪律
│   ├── skills/                # 技能包（每个含 SKILL.md + manifest.json）
│   └── data/                  # 运行时生成：会话、用量（不入库）
├── install.sh                 # Termux 一键部署脚本
├── config.example.json        # 配置模板
├── LICENSE
└── README.md
```

## 一键部署（Termux）

> 前置：安卓手机已安装 [Termux](https://github.com/termux/termux-app)（建议从 F-Droid 或 GitHub Releases 装，Play 商店版已停更）。

在 Termux 里依次执行：

```bash
pkg update -y && pkg install -y git python
git clone https://github.com/morisukesu/cyrene-web-mobile.git
cd cyrene-web-mobile
bash install.sh
```

`install.sh` 会自动：检查/安装 Python → 尝试装 termux-api 桥 → 生成配置 → 注册 `cyrene-web` 启动命令 → 启动服务。

启动后终端会打印访问地址，例如：

```
✓ Web 服务已启动: http://0.0.0.0:28443
  局域网: http://192.168.x.x:28443
```

用手机浏览器打开那个地址即可。之后在任意目录输入 `cyrene-web` 就能再次启动。

### 配置 API

首次部署后需要填入大模型接口。两种方式任选：

1. **网页面板**（推荐）：打开服务地址 → 设置 → 模型，填 `api_base` / `api_key` / `model`，保存即生效。
2. **编辑配置文件**：修改 `cyrene_mobile/.config.json` 的 `model` 分区。

接口需兼容 OpenAI 的 `/v1/chat/completions` 格式，并支持 Function Calling（工具调用）。
官方 OpenAI、各类 OpenAI 兼容中转站、本地推理服务（如 LM Studio / vLLM 暴露的兼容端点）均可。

### 硬件工具（可选）

电量、手电筒、通知、TTS、拍照等工具依赖 **Termux:API**：

1. 安装 [Termux:API 这个 Android App](https://github.com/termux/termux-api)（与 Termux 同源，F-Droid / GitHub Releases）。
2. 在 Termux 里执行 `pkg install termux-api`（`install.sh` 已尝试自动装）。
3. 在系统设置里给 Termux:API 授予相应权限（通知、定位、相机等）。

未装 Termux:API 不影响 Web 对话与文件/联网工具，仅硬件类工具不可用。

## 手动启动

不想用一键脚本时：

```bash
cd cyrene_mobile
python3 runtime/cyrene_web.py
```

常用环境变量 / 配置：

| 项 | 默认 | 说明 |
|---|---|---|
| `server.web_port` | `28443` | 监听端口 |
| `server.bind_host` | `0.0.0.0` | 监听地址；只想本机访问改 `127.0.0.1` |
| `CYRENE_FS_ROOT` | `$HOME` | 文件工具的根目录沙箱边界 |

## 安全提示

- 服务默认绑定 `0.0.0.0`，**对局域网开放**，且内置 `shell` 工具可执行任意命令。
  不需要外部访问时，请在「设置 → 服务」把 `bind_host` 改成 `127.0.0.1`。
- `.config.json` 含你的 API Key，已在 `.gitignore` 中排除，**不要提交到仓库**。
- 请仅在你信任的网络与设备上运行。

## 开发与验证

后端是单文件 `cyrene_web.py`，直接读源码即可。项目配套有一套离线验证脚本
（提示词注入、Agent Loop、工具、面板等），在本仓库的开发分支/历史中演进，
发布版聚焦可运行的核心。

## 致谢与署名

- 作者：**森亞ミミカ**
- 许可：[MIT License](./LICENSE)
- 前端组件：[marked](https://github.com/markedjs/marked)、[highlight.js](https://github.com/highlightjs/highlight.js)（均本地托管）

---

<div align="center">
<i>把一段陪伴，装进口袋里。</i>
</div>
