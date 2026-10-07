#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#   昔涟 · 手机版 Web Agent —— Termux 一键部署脚本
#   作者：森亞ミミカ
#   仓库：https://github.com/morisukesu/cyrene-web-mobile
#
#   用法（在 Termux 里）：
#       bash install.sh            # 准备环境并启动服务
#       bash install.sh --no-start # 只准备环境，不启动
# ============================================================
set -euo pipefail

# 彩色输出（终端支持时）
if [ -t 1 ]; then
  C_G='\033[32m'; C_Y='\033[33m'; C_R='\033[31m'; C_C='\033[36m'; C_B='\033[1m'; C_N='\033[0m'
else
  C_G=''; C_Y=''; C_R=''; C_C=''; C_B=''; C_N=''
fi
info()  { printf "${C_C}▶${C_N} %s\n" "$1"; }
ok()    { printf "${C_G}✓${C_N} %s\n" "$1"; }
warn()  { printf "${C_Y}⚠${C_N} %s\n" "$1"; }
die()   { printf "${C_R}✗${C_N} %s\n" "$1" >&2; exit 1; }

START=1
[ "${1:-}" = "--no-start" ] && START=0

# ---- 0. 环境检测 ----
if [ -z "${PREFIX:-}" ] || ! echo "$PREFIX" | grep -q "com.termux"; then
  warn "未检测到 Termux 环境（PREFIX=$PREFIX）。"
  warn "本脚本主要为 Android Termux 设计；在普通 Linux 上也可尝试，但硬件工具不可用。"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/cyrene_mobile"
[ -d "$APP_DIR" ] || die "找不到 $APP_DIR，请在仓库根目录运行本脚本。"

printf "\n${C_B}============================================${C_N}\n"
printf "${C_B}  昔涟 · 手机版 Web Agent  部署${C_N}\n"
printf "${C_B}============================================${C_N}\n\n"

# ---- 1. Python ----
info "[1/5] 检查 Python..."
if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  warn "  未找到 python，正在安装（需要联网）..."
  pkg update -y
  pkg install -y python
fi
PY="$(command -v python3 || command -v python)"
ok "  Python: $("$PY" --version 2>&1)  ($PY)"

# ---- 2. termux-api（硬件工具桥，可选）----
info "[2/5] 检查 termux-api（电量/手电筒/通知等硬件工具需要）..."
if command -v termux-battery-status >/dev/null 2>&1; then
  ok "  termux-api 命令行已就位。"
else
  warn "  未安装 termux-api 命令行桥，尝试安装..."
  if pkg install -y termux-api 2>/dev/null; then
    ok "  termux-api 命令行已安装。"
  else
    warn "  安装失败（不影响 Web 对话，仅硬件工具不可用）。"
  fi
  warn "  硬件工具还需安装 Termux:API 这个 Android App（F-Droid / GitHub Releases），"
  warn "  并在系统设置里授予相应权限。详见 README。"
fi

# ---- 3. 目录准备 ----
info "[3/5] 准备运行目录..."
mkdir -p "$APP_DIR/data"
ok "  数据目录: $APP_DIR/data"

# ---- 4. 配置文件 ----
info "[4/5] 检查配置..."
CFG="$APP_DIR/.config.json"
if [ ! -f "$CFG" ]; then
  if [ -f "$SCRIPT_DIR/config.example.json" ]; then
    cp "$SCRIPT_DIR/config.example.json" "$CFG"
    ok "  已从模板生成 .config.json"
  else
    ok "  未找到模板，服务首次启动会自动生成默认配置。"
  fi
  warn "  ⚠ 请编辑 $CFG 填入你的 api_base / api_key / model，"
  warn "    或启动后在网页「设置 → 模型」面板里填写。"
else
  ok "  .config.json 已存在，保留现有配置。"
fi

# ---- 5. 启动命令软链 ----
info "[5/5] 注册启动命令 cyrene-web..."
if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ]; then
  cat > "$PREFIX/bin/cyrene-web" << EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$APP_DIR"
exec "$PY" runtime/cyrene_web.py "\$@"
EOF
  chmod +x "$PREFIX/bin/cyrene-web"
  ok "  已注册: 之后在任意目录输入 cyrene-web 即可启动。"
else
  warn "  未找到 $PREFIX/bin，跳过软链。可手动启动：cd $APP_DIR && $PY runtime/cyrene_web.py"
fi

printf "\n${C_G}${C_B}部署完成！${C_N}\n\n"
printf "启动方式：\n"
printf "  · 输入命令:  ${C_C}cyrene-web${C_N}\n"
printf "  · 或手动:    ${C_C}cd %s && %s runtime/cyrene_web.py${C_N}\n\n" "$APP_DIR" "$PY"
printf "默认端口 ${C_B}28443${C_N}，启动后用手机浏览器打开终端里显示的地址即可。\n"
printf "按 Ctrl+C 停止服务。\n\n"

if [ "$START" = "1" ]; then
  printf "${C_C}▶ 正在启动服务...${C_N}\n\n"
  cd "$APP_DIR"
  exec "$PY" runtime/cyrene_web.py
fi
