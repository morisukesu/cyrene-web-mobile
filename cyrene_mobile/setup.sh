#!/data/data/com.termux/files/usr/bin/bash
# 昔涟 · 手机版一键部署脚本
# 在 Termux 里运行：bash setup.sh

set -e

CYRENE_DIR="$HOME/cyrene"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "========================================"
echo "  昔涟 · 手机版 部署"
echo "========================================"

# ---- 1. 装 Python ----
echo ""
echo "[1/4] 检查 Python..."
if ! command -v python3 &> /dev/null; then
    echo "  安装 Python..."
    pkg update -y
    pkg install -y python
else
    echo "  Python 已存在: $(python3 --version)"
fi

# ---- 2. 创建目录 ----
echo ""
echo "[2/4] 创建目录..."
mkdir -p "$CYRENE_DIR/prompts"
mkdir -p "$CYRENE_DIR/skills"
mkdir -p "$CYRENE_DIR/runtime"

# ---- 3. 复制文件 ----
echo ""
echo "[3/4] 复制文件..."

# prompts
if [ -d "$REPO_DIR/prompts" ]; then
    cp -r "$REPO_DIR/prompts/"* "$CYRENE_DIR/prompts/"
    echo "  prompts: $(ls "$CYRENE_DIR/prompts" | wc -l) 个文件"
fi

# skills
if [ -d "$REPO_DIR/skills" ]; then
    cp -r "$REPO_DIR/skills/"* "$CYRENE_DIR/skills/"
    echo "  skills: $(ls "$CYRENE_DIR/skills" | wc -l) 个目录"
fi

# runtime
if [ -f "$REPO_DIR/runtime/cyrene_phone.py" ]; then
    cp "$REPO_DIR/runtime/cyrene_phone.py" "$CYRENE_DIR/runtime/"
    echo "  runtime: cyrene_phone.py"
fi

# ---- 4. 创建启动脚本 ----
echo ""
echo "[4/4] 创建启动脚本..."
cat > "$CYRENE_DIR/start.sh" << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"
exec python3 runtime/cyrene_phone.py
EOF
chmod +x "$CYRENE_DIR/start.sh"

# 软链到 bin
ln -sf "$CYRENE_DIR/start.sh" "$PREFIX/bin/cyrene"

echo ""
echo "========================================"
echo "  部署完成！"
echo "========================================"
echo ""
echo "启动方式："
echo "  方法1: 输入 cyrene"
echo "  方法2: cd ~/cyrene && bash start.sh"
echo "  方法3: python3 ~/cyrene/runtime/cyrene_phone.py"
echo ""
echo "首次运行会要求输入 API Key"
echo ""
