#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#   昔涟 · 手机版 Web Agent —— 原地更新脚本
#   仓库：https://github.com/morisukesu/cyrene-web-mobile
#
#   把已经部署好的这一份更新到仓库最新，你自己的东西一样不动：
#     保留  cyrene_mobile/.config.json、data/、plugins/、plugins_inbox/、_plugin_tmp/
#     替换  cyrene_mobile/ 里的 prompts/ runtime/ skills/ setup.sh 等
#     顺带  把根目录的 install.sh、update.sh 也换成新版
#   动手之前先把旧代码备份到 cyrene_mobile.bak-<时间戳>/（--no-backup 可关）。
#
#   这份如果用 git clone 下来的，直接 git pull 更省事；本脚本是给
#   「手动拷进去 / adb push 进去、不带 .git」的那种部署用的。
# ============================================================
set -euo pipefail

REPO="morisukesu/cyrene-web-mobile"
BRANCH="main"
MODE="auto"
DO_BACKUP=1
FROM_DIR=""

usage() {
  cat <<'EOF'
昔涟 · 手机版 Web Agent —— 原地更新

  bash update.sh               有新版本就更新；更新前在跑的话自动重启
  bash update.sh --check       只看有没有新版，一个文件都不动
  bash update.sh --no-start    更新但不碰服务
  bash update.sh --start       更新完一定拉起服务
  bash update.sh --no-backup   不留备份
  bash update.sh --from DIR    从本地目录更新（DIR 里要有 cyrene_mobile/）
  bash update.sh --branch dev  换分支，默认 main

保留自己的东西：cyrene_mobile/.config.json、data/、plugins/、plugins_inbox/
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check)     MODE="check" ;;
    --no-start)  MODE="off" ;;
    --start)     MODE="on" ;;
    --no-backup) DO_BACKUP=0 ;;
    --from)      FROM_DIR="${2:-}"; [ -n "$FROM_DIR" ] || { echo "--from 后面要跟一个目录" >&2; exit 2; }; shift ;;
    --from=*)    FROM_DIR="${1#--from=}" ;;
    --branch)    BRANCH="${2:-}"; [ -n "$BRANCH" ] || { echo "--branch 后面要跟分支名" >&2; exit 2; }; shift ;;
    --branch=*)  BRANCH="${1#--branch=}" ;;
    -h|--help)   usage; exit 0 ;;
    *)           echo "不认识的参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then
  C_G='\033[32m'; C_Y='\033[33m'; C_R='\033[31m'; C_C='\033[36m'; C_B='\033[1m'; C_N='\033[0m'
else
  C_G=''; C_Y=''; C_R=''; C_C=''; C_B=''; C_N=''
fi
info()  { printf "${C_C}▸${C_N} %s\n" "$1"; }
ok()    { printf "${C_G}✓${C_N} %s\n" "$1"; }
warn()  { printf "${C_Y}⚠${C_N} %s\n" "$1"; }
die()   { printf "${C_R}✗${C_N} %s\n" "$1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/cyrene_mobile"
[ -d "$APP_DIR" ] || die "找不到 $APP_DIR —— 请在仓库根目录（和 install.sh 同一层）运行本脚本。"
[ -f "$APP_DIR/runtime/cyrene_web.py" ] || die "这份目录看起来不完整：缺 cyrene_mobile/runtime/cyrene_web.py"

# 挑一个真的能跑起来的 python3：有些系统上 python3 只是个空壳别名
# （Windows 的 Store 别名就是这样），command -v 找得到、跑起来却报错。
PY=""
for c in python3 python; do
  p="$(command -v "$c" 2>/dev/null || true)"
  [ -n "$p" ] || continue
  if "$p" -c 'import sys' >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || die "找不到能用的 python。先 bash install.sh，或在 Termux 里 pkg install python"

PORT="$("$PY" - "$APP_DIR/.config.json" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    d = {}
print((d.get("server") or {}).get("web_port") or 28443)
PYEOF
)"

# 代码指纹：把 prompts/ runtime/ skills/ setup.sh 里每个文件的相对路径与内容哈希
# 汇总成一个短指纹。用它判断「有没有新版」，比单看一个文件准。
fp_of() {
  "$PY" - "$1" <<'PYEOF'
import hashlib, os, sys
root = sys.argv[1]
targets = ["prompts", "runtime", "skills", "setup.sh"]
items = []
for t in targets:
    p = os.path.join(root, t)
    if os.path.isfile(p):
        items.append(p)
    elif os.path.isdir(p):
        for dp, dns, fns in os.walk(p):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for fn in fns:
                items.append(os.path.join(dp, fn))
h = hashlib.sha256()
for p in sorted(items):
    h.update(os.path.relpath(p, root).encode("utf-8"))
    h.update(b"\0")
    with open(p, "rb") as f:
        h.update(hashlib.sha256(f.read()).hexdigest().encode())
print("%d %s" % (len(items), h.hexdigest()[:12]))
PYEOF
}

# 列出两边不一样的文件（最多 10 行），只读，不影响更新
diff_list() {
  "$PY" - "$1" "$2" <<'PYEOF'
import hashlib, os, sys
old_root, new_root = sys.argv[1], sys.argv[2]
targets = ["prompts", "runtime", "skills", "setup.sh"]

def walk(root):
    out = {}
    for t in targets:
        p = os.path.join(root, t)
        if os.path.isfile(p):
            out[t] = p
        elif os.path.isdir(p):
            for dp, dns, fns in os.walk(p):
                dns[:] = [d for d in dns if d != "__pycache__"]
                for fn in fns:
                    fp = os.path.join(dp, fn)
                    out[os.path.relpath(fp, root)] = fp
    return out

def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

a, b = walk(old_root), walk(new_root)
changed = []
for n in sorted(set(a) | set(b)):
    if n not in a or n not in b or sha(a[n]) != sha(b[n]):
        changed.append(n)
for n in changed[:10]:
    tag = "新增" if n not in a else ("删除" if n not in b else "改动")
    print("    %s  %s" % (tag, n))
if len(changed) > 10:
    print("    …… 还有 %d 个" % (len(changed) - 10))
print("    合计 %d 个文件有变化" % len(changed))
PYEOF
}

printf "\n${C_B}============================================${C_N}\n"
printf "${C_B}  昔涟 · 手机版 Web Agent  更新${C_N}\n"
printf "${C_B}============================================${C_N}\n\n"

read -r OLD_N OLD_FP <<< "$(fp_of "$APP_DIR")"
info "当前：$OLD_N 个代码文件，指纹 $OLD_FP"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/cyrene-update.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

if [ -n "$FROM_DIR" ]; then
  [ -d "$FROM_DIR/cyrene_mobile" ] || die "--from 指的目录里要有 cyrene_mobile/ 子目录：$FROM_DIR"
  NEW_ROOT="$(cd "$FROM_DIR" && pwd)"
  [ "$NEW_ROOT" = "$SCRIPT_DIR" ] && die "--from 指的就是当前这份目录，没有可更新的内容。"
  ok "从本地目录取新版：$NEW_ROOT"
else
  info "从 GitHub 拉取 $BRANCH 分支..."
  URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  TGZ="$WORK/branch.tar.gz"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 2 --connect-timeout 15 -o "$TGZ" "$URL" \
      || die "下载失败：$URL（没网的话，把仓库包解压好，再用 --from 指过来）"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$TGZ" "$URL" || die "下载失败：$URL（没网的话，把仓库包解压好，再用 --from 指过来）"
  else
    die "既没有 curl 也没有 wget。先 pkg install curl，或用 --from 指本地目录"
  fi
  ok "下载完成（$(du -h "$TGZ" 2>/dev/null | cut -f1)）"
  tar -xzf "$TGZ" -C "$WORK" || die "解压失败，多半是没下全，重跑一次"
  NEW_ROOT="$(find "$WORK" -maxdepth 2 -type d -name cyrene_mobile | head -n 1)"
  [ -n "$NEW_ROOT" ] || die "解压后没看到 cyrene_mobile 目录"
  NEW_ROOT="$(cd "$NEW_ROOT/.." && pwd)"
fi
NEW_APP="$NEW_ROOT/cyrene_mobile"

info "先做语法自检（这时还没动你的任何文件）"
"$PY" -m py_compile "$NEW_APP/runtime/cyrene_web.py" \
  || die "新版本的 cyrene_web.py 语法没过，已放弃更新，你的文件一个都没动。"
if command -v node >/dev/null 2>&1 && [ -f "$NEW_APP/runtime/plugin_host.cjs" ]; then
  node --check "$NEW_APP/runtime/plugin_host.cjs" >/dev/null 2>&1 \
    || warn "plugin_host.cjs 语法自检没过（只影响插件功能，继续）"
fi
ok "语法自检通过"

read -r NEW_N NEW_FP <<< "$(fp_of "$NEW_APP")"
info "新版：$NEW_N 个代码文件，指纹 $NEW_FP"

if [ "$OLD_FP" = "$NEW_FP" ]; then
  ok "已经是最新的了，没有要换的东西。"
  exit 0
fi
if [ "$MODE" = "check" ]; then
  warn "有新版本可以更新，跑 bash update.sh 就行。"
  exit 0
fi

info "变化的文件："
diff_list "$APP_DIR" "$NEW_APP" || true

if [ "$DO_BACKUP" = "1" ]; then
  TS="$(date +%Y%m%d-%H%M%S)"
  BK="$SCRIPT_DIR/cyrene_mobile.bak-$TS"
  info "备份旧代码到 $(basename "$BK")/"
  mkdir -p "$BK"
  for item in prompts runtime skills setup.sh; do
    if [ -e "$APP_DIR/$item" ]; then cp -a "$APP_DIR/$item" "$BK/"; fi
  done
  if [ -f "$APP_DIR/.config.json" ]; then cp -a "$APP_DIR/.config.json" "$BK/"; fi
  ok "备份好了（data/ 和 plugins/ 没动，所以不备）"
fi

info "替换代码（保留 .config.json、data/、plugins/、plugins_inbox/、_plugin_tmp/）"
KEEP=".config.json data plugins plugins_inbox _plugin_tmp __pycache__"
for src in "$NEW_APP"/* "$NEW_APP"/.[!.]*; do
  [ -e "$src" ] || continue
  name="$(basename "$src")"
  keep=0
  for k in $KEEP; do
    if [ "$name" = "$k" ]; then keep=1; fi
  done
  if [ "$keep" = "1" ]; then continue; fi
  rm -rf "$APP_DIR/$name"
  cp -a "$src" "$APP_DIR/"
  info "  $name"
done
ok "代码已换成新版"

info "同步根目录脚本（install.sh / update.sh）"
for f in install.sh update.sh; do
  if [ -f "$NEW_ROOT/$f" ]; then
    cp -f "$NEW_ROOT/$f" "$SCRIPT_DIR/$f.new"
    mv -f "$SCRIPT_DIR/$f.new" "$SCRIPT_DIR/$f"
    chmod +x "$SCRIPT_DIR/$f" 2>/dev/null || true
  fi
done
ok "脚本已对齐新版"

WAS_RUNNING=0
if command -v pgrep >/dev/null 2>&1 && pgrep -f 'cyrene_web\.py' >/dev/null 2>&1; then
  WAS_RUNNING=1
fi

want_start=0
case "$MODE" in
  on)   want_start=1 ;;
  off)  want_start=0 ;;
  auto) if [ "$WAS_RUNNING" = "1" ]; then want_start=1; fi ;;
esac

if [ "$want_start" = "1" ]; then
  info "重启服务..."
  if [ "$WAS_RUNNING" = "1" ]; then
    pkill -f 'cyrene_web\.py' 2>/dev/null || true
    sleep 3
  fi
  if command -v pgrep >/dev/null 2>&1 && pgrep -f 'cyrene_web\.py' >/dev/null 2>&1; then
    ok "  进程已经在跑（守卫自己拉起来了）"
  else
    mkdir -p "$APP_DIR/data"
    ( cd "$APP_DIR" && nohup "$PY" runtime/cyrene_web.py >> data/cyrene-web.log 2>&1 & )
    ok "  已拉起，日志：cyrene_mobile/data/cyrene-web.log"
    sleep 2
  fi
  if command -v curl >/dev/null 2>&1; then
    hit=0
    i=0
    while [ "$i" -lt 12 ]; do
      code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
      if [ "$code" = "200" ]; then hit=1; break; fi
      i=$((i + 1))
      sleep 1
    done
    if [ "$hit" = "1" ]; then
      ok "  /health 正常，端口 $PORT"
    else
      warn "  探活没拿到 200（可能端口不是 $PORT，或还没起完）。看一眼：pgrep -f cyrene_web.py"
    fi
  fi
else
  if [ "$WAS_RUNNING" = "1" ]; then
    warn "按你的参数没动服务。想让它加载新版：pkill -f cyrene_web.py，然后重新 cyrene-web"
  else
    info "服务本来就没在跑，就没替你开。想启动敲：cyrene-web"
  fi
fi

printf "\n${C_G}${C_B}更新完成${C_N}\n"
printf "  代码指纹：%s  →  %s\n" "$OLD_FP" "$NEW_FP"
if [ "$DO_BACKUP" = "1" ]; then
  printf "  备份：%s\n" "$(basename "$BK")"
  printf "  想退回去：cp -a %s/. cyrene_mobile/ 然后 pkill -f cyrene_web.py\n" "$(basename "$BK")"
fi
printf "\n"
