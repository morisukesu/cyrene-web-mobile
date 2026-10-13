# -*- coding: utf-8 -*-
"""cyrene_fs —— 文件工具组（`cyrene_web` 拆出来的第三块）。

它原来是什么
------------
六个直接读写本机文件的工具：`read_file` / `write_file` / `edit_file` /
`glob_files` / `grep_files` / `list_dir`，加上它们共用的**路径守卫**。

在宿主里这三样是挨着的：常量与守卫（178–291）、扫描辅助（325–363）、
六个 handler（496–833）。中间夹着通用小工具、视觉 handler、记忆函数，
所以**不能整块切**，是按段抽出来的。

路径守卫值得多看一眼（对齐桌面端 Code 模式的「绑定可信工作目录」）：
  · 只允许 `FS_ROOT`（默认家目录，可用 `CYRENE_FS_ROOT` 覆盖）之内
  · abspath 消 `..` 判一次，realpath 解符号链接再判一次 —— 两道都过才放行。
    只做 realpath 会让「root 自己是符号链接」时比较错位；只做 abspath 则
    挡不住 `ln -s /` 这类逃逸。
  · 写操作另有一份硬保护名单（配置 / 会话 / 运行时自身），越权直接拒。

`guard_path` / `PathGuardError` / `_rel` / `_human_size` 被六个以上地方共用
（下载、上传、技能目录、后台任务日志、工具派发…），所以宿主那边**保留同名
转发**，那些调用点一处都不用改。

它依赖宿主什么
--------------
由宿主经 `bind_deps()` 注入，共三项：

  · `protected_files`  返回「不许写」的路径集合（宿主才知道自己的命脉在哪）
  · `outcome_success` / `outcome_failure`  工具结果常量
  · `int_arg` / `bool_arg` / `decode_bytes`  工具层通用小件

红线与 `cyrene_search.py` 一致：**绝不 import cyrene_web**，方向只有
「宿主 → 内核 → 子系统」。

handler 名字保留原来的下划线写法（`_h_read_file` 等）
-------------------------------------------------------
`TOOLS` 表里存的是 handler 名字符串，改名字就得连带改表。为搬家去改表不划算，
所以新模块里也照原样叫 `_h_read_file` —— 看着像私有名，实际是本模块的对外入口，
宿主转发层认的就是它。
"""

import fnmatch
import json
import os
import re
import shutil
import uuid
from pathlib import Path


__all__ = [
    "FS_ROOT_DEFAULT", "FS_SKIP_DIRS", "FS_SKIP_SUFFIXES",
    "FS_MAX_WALK", "FS_GLOB_LIMIT", "FS_GREP_LIMIT",
    "FS_READ_DEFAULT_LINES", "FS_READ_MAX_LINES",
    "FS_MAX_FILE_BYTES", "FS_WRITE_MAX_BYTES", "FS_LIST_LIMIT",
    "PathGuardError", "guard_path",
    "bind_deps", "deps_ready", "DEPS", "PLUGIN",
    "_h_read_file", "_h_write_file", "_h_edit_file",
    "_h_glob_files", "_h_grep_files", "_h_list_dir",
]


class _Deps:
    """宿主注入的资源袋。每项没绑定时是 None，取用时抛清楚的错。"""

    __slots__ = ("protected_files", "outcome_success", "outcome_failure",
                 "int_arg", "bool_arg", "decode_bytes")

    def __init__(self):
        self.protected_files = None
        self.outcome_success = "success"
        self.outcome_failure = "failure"
        self.int_arg = None
        self.bool_arg = None
        self.decode_bytes = None

    def missing(self):
        need = ("protected_files", "int_arg", "bool_arg", "decode_bytes")
        return [k for k in need if not getattr(self, k)]


DEPS = _Deps()

_FRESH_DEPS = _Deps()


def bind_deps(**kw):
    """宿主一次性注入。未知的键直接报错，不静默吞掉。"""
    unknown = [k for k in kw if k not in _Deps.__slots__]
    if unknown:
        raise ValueError("cyrene_fs.bind_deps 不认识这些参数：%s"
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
    """内核在 net / tools / config / fs 四个服务齐了之后调这里。"""
    tools = ctx.require("tools")
    host = ctx.require("fs")

    bind_deps(
        protected_files=host.protected_files,
        outcome_success=tools.outcome_success,
        outcome_failure=tools.outcome_failure,
        int_arg=tools.int_arg,
        bool_arg=tools.bool_arg,
        decode_bytes=tools.decode_bytes,
    )
    ctx.effect(_reset_deps, desc="fs:deps")
    ctx.log("已接到内核（依赖 tools / fs）")


#: 内核挂载描述。fs 服务由宿主提供（它才知道自己的根目录与命脉文件在哪）。
PLUGIN = {
    "id": "fs",
    "requires": ["tools", "fs"],
    "setup": _plugin_setup,
}


# ===========================================================================
# 以下正文由 _make_fs_module.py 从 cyrene_web.py 逐字抽出
# ===========================================================================


# ========== 文件工具实现层（阶段 2a）==========
# 与 termux-api 工具的根本差异：这些工具不 fork 子进程，直接在服务进程内跑
# Python。所以 run_tool 需要一个 `handler` 分支（见下方 TOOLS 注释与 run_tool）。
#
# handler 约定：handler(args) -> (outcome, result_text)
#   outcome 取 DEPS.outcome_success / DEPS.outcome_failure / OUTCOME_UNKNOWN
#   args 是**已按 params 类型归一**的 dict（int 还是 int，bool 还是 bool），
#   与走命令行的 resolve_args（全转 str）不同，见 resolve_typed()。
#
# 路径守卫（对齐桌面端 Code 模式「绑定可信工作目录」）：
#   · 只允许 FS_ROOT（默认 $HOME，可用 CYRENE_FS_ROOT 覆盖）之内
#   · abspath 消 `..` 判一次，realpath 解符号链接再判一次 —— 两道都过才放行。
#     只做 realpath 会让「root 自己是符号链接」时比较错位；只做 abspath 则
#     挡不住 `ln -s /` 这类逃逸。
#   · 写操作另有一份硬保护名单（配置/会话/运行时自身），越权直接拒。

FS_ROOT_DEFAULT = Path.home()


def _fs_root():
    """允许访问的根目录。每次调用现算，方便单元测试改环境变量或改模块全局。"""
    raw = os.environ.get("CYRENE_FS_ROOT")
    try:
        return Path(raw).resolve() if raw else Path(FS_ROOT_DEFAULT).resolve()
    except (OSError, ValueError):
        return Path(FS_ROOT_DEFAULT).resolve()


# 写入硬保护名单：服务自身的命脉文件。读不受限，写一律拒。
# 改运行时代码必须走部署流程（deploy_web_v8.py + _clean_restart.py），
# 让模型直接 write_file 覆盖正在运行的 .py 等于把服务往死里改。
def _fs_protected():
    """受写保护的宿主文件集合。

    「哪些文件算命脉」是宿主的判断（它的配置、会话、用量，加上运行时自己），
    所以由宿主经 `protected_files` 注入；本模块只负责 resolve 掉符号链接再比对。
    注入缺失时返回空集 —— 会比原来宽松（少了那层保护），但不会因此崩。
    """
    out = set()
    getter = DEPS.protected_files
    if not getter:
        return out
    try:
        items = getter()
    except Exception:
        return out
    for p in (items or ()):
        try:
            out.add(Path(p).resolve())
        except (OSError, ValueError):
            pass
    return out


# 扫描类工具的性能护栏：手机 CPU 弱，$HOME 下可能有几千个文件。
FS_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                ".mypy_cache", ".pytest_cache", ".tox", "site-packages"}
# 扫描类工具跳过的文件名。write_file/edit_file 每次改文件都会留 .bak，
# 写入过程还会短暂存在 .tmpXXXX —— 让 grep 同时命中原文和备份，
# 模型看到的匹配数会翻倍，纯属噪音。要查备份就直接 read_file 那个 .bak。
FS_SKIP_SUFFIXES = (".bak", ".tmp", ".swp", ".orig", ".rej")


def _skip_file(name):
    """扫描时是否跳过这个文件名（备份/临时/残留）。"""
    low = name.lower()
    if low.endswith(FS_SKIP_SUFFIXES):
        return True
    # 本工具自己写的临时文件形如 foo.txt.tmp1a2b3c4d
    if ".tmp" in low:
        return True
    return name.endswith("~")


FS_MAX_WALK = 20000        # 单次 glob/grep 最多遍历多少个条目
FS_GLOB_LIMIT = 100        # glob_files 返回上限
FS_GREP_LIMIT = 250        # grep_files 匹配行上限
FS_READ_DEFAULT_LINES = 2000
FS_READ_MAX_LINES = 20000
FS_MAX_FILE_BYTES = 4 * 1024 * 1024     # 读取上限 4 MiB，防 OOM
FS_WRITE_MAX_BYTES = 8 * 1024 * 1024    # 写入上限 8 MiB
FS_LIST_LIMIT = 500        # list_dir 返回上限


class PathGuardError(Exception):
    """路径越界或不可写。消息直接回给模型，所以要说清为什么被拒。"""


def guard_path(raw, want="any", protect_write=False):
    """把模型给的路径解析成受控的绝对 Path；越界或违规抛 PathGuardError。

    want: "any" 不校验存在性 / "file" 必须是已存在文件 / "dir" 必须是已存在目录
    protect_write: True 时额外过写入保护名单
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathGuardError("(路径为空。请给出文件路径，相对路径按根目录解析)")
    raw = raw.strip().replace("\x00", "")
    root = _fs_root()

    # ~ 展开只认根目录自己，别的用户家目录不给碰
    if raw == "~":
        cand = root
    elif raw.startswith("~/") or raw.startswith("~" + os.sep):
        cand = root / raw[2:]
    else:
        p = Path(raw)
        cand = p if p.is_absolute() else (root / p)

    # 第一道：abspath 消掉 .. 后必须仍在 root 内
    try:
        ap = Path(os.path.abspath(str(cand)))
    except (OSError, ValueError) as e:
        raise PathGuardError(f"(路径无法解析: {e})")
    if ap != root and root not in ap.parents:
        raise PathGuardError(
            f"(路径越界：只允许访问 {root} 之内的文件，拒绝 {ap})")

    # 第二道：解开符号链接后再判一次，防 `ln -s /` 逃逸
    try:
        rp = Path(os.path.realpath(str(ap)))
    except (OSError, ValueError) as e:
        raise PathGuardError(f"(路径无法解析: {e})")
    real_root = Path(os.path.realpath(str(root)))
    if rp != real_root and real_root not in rp.parents:
        raise PathGuardError(
            f"(路径经符号链接指向 {root} 之外，已拒绝: {rp})")

    if protect_write and rp in _fs_protected():
        raise PathGuardError(
            f"(该文件受写入保护，不允许通过工具修改: {rp.name}。"
            f"服务配置与会话数据只能在设置面板里改)")

    if want == "file":
        if not ap.is_file():
            raise PathGuardError(f"(文件不存在或不是普通文件: {ap})")
    elif want == "dir":
        if not ap.is_dir():
            raise PathGuardError(f"(目录不存在: {ap})")
    return ap


def _scan_base(root_raw):
    """解析扫描起点目录（glob/grep 用）。缺省 = FS_ROOT。"""
    if not root_raw or not str(root_raw).strip():
        return _fs_root()
    return guard_path(root_raw, want="dir")


def _walk_files(base):
    """生成 base 下的文件 Path，跳过噪音目录与备份文件，带总条目上限防卡死。"""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames[:] = [d for d in dirnames if d not in FS_SKIP_DIRS]
        for fn in filenames:
            if _skip_file(fn):
                continue
            seen += 1
            if seen > FS_MAX_WALK:
                return
            yield Path(dirpath) / fn


def _rel(p, base):
    try:
        return str(Path(p).relative_to(base)).replace(os.sep, "/")
    except (ValueError, OSError):
        return str(p)


def _human_size(n):
    """字节数转人类可读。手机屏幕窄，保持 5 字以内。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def _h_read_file(a):
    """read_file：按行分段读文本文件，带行号。"""
    try:
        p = guard_path(a.get("path"), want="file")
    except PathGuardError as e:
        return DEPS.outcome_failure, str(e)

    try:
        size = p.stat().st_size
    except OSError as e:
        return DEPS.outcome_failure, f"(读不到文件信息: {e})"
    if size > FS_MAX_FILE_BYTES:
        return DEPS.outcome_failure, (
            f"(文件 {size} 字节，超过读取上限 {FS_MAX_FILE_BYTES} 字节，"
            f"请改用 grep_files 定位后再按 offset/limit 分段读)")

    try:
        data = p.read_bytes()
    except OSError as e:
        return DEPS.outcome_failure, f"(读取失败: {e})"
    if b"\x00" in data[:8192]:
        return DEPS.outcome_failure, (
            f"(这是二进制文件（{_human_size(size)}），不能当文本读)")

    text, enc_note = DEPS.decode_bytes(data)
    lines = text.split("\n")
    total = len(lines)
    if lines and lines[-1] == "":
        total -= 1          # 末尾换行不算一行
        lines = lines[:-1]

    offset = DEPS.int_arg(a.get("offset"), 0, 0, 10 ** 9)
    limit = DEPS.int_arg(a.get("limit"), FS_READ_DEFAULT_LINES, 1, FS_READ_MAX_LINES)
    if offset >= max(total, 1) and total > 0:
        return DEPS.outcome_failure, (
            f"(offset={offset} 超出文件末尾。该文件共 {total} 行)")

    chunk = lines[offset:offset + limit]
    width = len(str(offset + len(chunk))) if chunk else 1
    body = "\n".join(f"{offset + i + 1:>{width}} | {ln}" for i, ln in enumerate(chunk))

    head = f"文件: {_rel(p, _fs_root())}  共 {total} 行 / {_human_size(size)}"
    if offset or len(chunk) < total:
        head += f"  · 本次显示第 {offset + 1}-{offset + len(chunk)} 行"
    notes = [head]
    if enc_note:
        notes.append(enc_note)
    if offset + len(chunk) < total:
        notes.append(f"（还有 {total - offset - len(chunk)} 行未显示，"
                     f"需要就传 offset={offset + len(chunk)} 继续读）")
    notes.append("（左侧数字是行号，仅供定位。edit_file 的 old_string 要用文件原文，"
                 "不要把行号一起复制进去）")
    return DEPS.outcome_success, "\n".join(notes) + "\n" + body


def _h_write_file(a):
    """write_file：整文件覆写。已存在则先备份 .bak，落盘走临时文件 + os.replace。"""
    try:
        p = guard_path(a.get("path"), want="any", protect_write=True)
    except PathGuardError as e:
        return DEPS.outcome_failure, str(e)

    content = a.get("content")
    if content is None:
        return DEPS.outcome_failure, "(缺少 content 参数)"
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)

    raw = content.encode("utf-8")
    if len(raw) > FS_WRITE_MAX_BYTES:
        return DEPS.outcome_failure, (
            f"(内容 {len(raw)} 字节，超过写入上限 {FS_WRITE_MAX_BYTES} 字节，请分批写)")

    existed = p.exists()
    if existed and p.is_dir():
        return DEPS.outcome_failure, f"(目标是个目录，不能当文件写: {p})"

    bak_note = ""
    # 临时名必须唯一：dispatch_tools 并行跑，两个线程同时写同一文件时
    # 用 pid 会撞名，导致一方 replace 掉另一方写了一半的内容。
    tmp = Path(str(p) + f".tmp{uuid.uuid4().hex[:8]}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            # 写前备份：只留最近一份，避免 .bak 无限堆积
            shutil.copy2(str(p), str(p) + ".bak")
            bak_note = f"，原文件已备份为 {p.name}.bak"
        tmp.write_bytes(raw)
        os.replace(str(tmp), str(p))       # 原子替换，中途崩不会留半截文件
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return DEPS.outcome_failure, f"(写入失败: {e})"

    nlines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return DEPS.outcome_success, (
        f"已写入 {_rel(p, _fs_root())}（{len(raw)} 字节，{nlines} 行{bak_note}）")


def _h_edit_file(a):
    """edit_file：精确字符串替换。匹配数不等于预期就报错，绝不猜。"""
    try:
        p = guard_path(a.get("path"), want="file", protect_write=True)
    except PathGuardError as e:
        return DEPS.outcome_failure, str(e)

    old = a.get("old_string")
    new = a.get("new_string")
    if not isinstance(old, str) or old == "":
        return DEPS.outcome_failure, "(old_string 为空。要整文件覆写请用 write_file)"
    if not isinstance(new, str):
        return DEPS.outcome_failure, "(new_string 缺失或类型不对)"
    if old == new:
        return DEPS.outcome_failure, "(old_string 与 new_string 相同，无需修改)"

    try:
        data = p.read_bytes()
    except OSError as e:
        return DEPS.outcome_failure, f"(读取失败: {e})"
    if len(data) > FS_MAX_FILE_BYTES:
        return DEPS.outcome_failure, (
            f"(文件 {len(data)} 字节过大，超过 {FS_MAX_FILE_BYTES} 字节上限，"
            f"请改用 write_file 分段重写)")
    if b"\x00" in data[:8192]:
        return DEPS.outcome_failure, "(二进制文件，不能做文本替换)"

    text, _ = DEPS.decode_bytes(data)
    n = text.count(old)
    replace_all = DEPS.bool_arg(a.get("replace_all"), False)

    if n == 0:
        hint = ""
        # 最常见的原因是空白/缩进对不上，给个可操作提示而不是干巴巴报错
        probe = old.strip()
        if probe and probe in text:
            hint = "（提示：去掉首尾空白后能匹配到，说明你给的 old_string 缩进或换行与文件不一致，请先 read_file 核对原文）"
        return DEPS.outcome_failure, f"(在文件里找不到 old_string，未做任何修改{hint})"
    if n > 1 and not replace_all:
        return DEPS.outcome_failure, (
            f"(old_string 在文件里出现 {n} 次，无法确定改哪一处，未做任何修改。"
            f"请把上下文加长到唯一，或显式传 replace_all=true 全部替换)")

    result = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    raw = result.encode("utf-8")
    tmp = Path(str(p) + f".tmp{uuid.uuid4().hex[:8]}")   # 并行安全，同 write_file
    try:
        shutil.copy2(str(p), str(p) + ".bak")
        tmp.write_bytes(raw)
        os.replace(str(tmp), str(p))
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return DEPS.outcome_failure, f"(写入失败: {e})"

    return DEPS.outcome_success, (
        f"已替换 {n if replace_all else 1} 处 → {_rel(p, _fs_root())}"
        f"（原文件备份为 {p.name}.bak）")


def _h_glob_files(a):
    """glob_files：按通配模式找文件，返回相对路径列表。"""
    pattern = a.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return DEPS.outcome_failure, "(缺少 pattern，例如 **/*.py 或 *.md)"
    pattern = pattern.strip()
    try:
        base = _scan_base(a.get("root"))
    except PathGuardError as e:
        return DEPS.outcome_failure, str(e)

    hits = []
    truncated = False
    for f in _walk_files(base):
        rel = _rel(f, base)
        name = f.name
        # 支持三种写法：纯文件名模式 / 带目录的模式 / ** 递归模式
        if (fnmatch.fnmatch(name, pattern)
                or fnmatch.fnmatch(rel, pattern)
                or ("/" not in pattern and fnmatch.fnmatch(rel, "*/" + pattern))):
            hits.append(rel)
            if len(hits) >= FS_GLOB_LIMIT:
                truncated = True
                break

    if not hits:
        return DEPS.outcome_success, f"(在 {_rel(base, _fs_root()) or '.'} 下没有匹配 {pattern} 的文件)"
    hits.sort()
    head = f"匹配 {pattern}：{len(hits)} 个文件" + ("（已到上限，可能还有更多）" if truncated else "")
    return DEPS.outcome_success, head + "\n" + "\n".join(hits)


def _h_grep_files(a):
    """grep_files：正则搜文件内容，带行号与可选上下文。"""
    pattern = a.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return DEPS.outcome_failure, "(缺少 pattern)"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return DEPS.outcome_failure, f"(正则表达式不合法: {e})"

    try:
        base = _scan_base(a.get("root"))
    except PathGuardError as e:
        return DEPS.outcome_failure, str(e)

    fileglob = (a.get("glob") or "").strip() if isinstance(a.get("glob"), str) else ""
    ctx = DEPS.int_arg(a.get("context"), 0, 0, 10)
    only_names = DEPS.bool_arg(a.get("files_with_matches"), False)

    out = []
    nmatch = 0        # 真正匹配上的行数（不含 context 行）
    nline = 0         # 输出行数（含 context 行），受 FS_GREP_LIMIT 约束
    nfile = 0
    truncated = False
    for f in _walk_files(base):
        rel = _rel(f, base)
        if fileglob and not (fnmatch.fnmatch(f.name, fileglob)
                             or fnmatch.fnmatch(rel, fileglob)):
            continue
        try:
            if f.stat().st_size > FS_MAX_FILE_BYTES:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue                      # 跳过二进制
        text, _ = DEPS.decode_bytes(data)
        lines = text.split("\n")
        idxs = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not idxs:
            continue
        nfile += 1
        nmatch += len(idxs)
        if only_names:
            out.append(f"{rel}（{len(idxs)} 处）")
            nline += 1
            if nline >= FS_GREP_LIMIT:
                truncated = True
                break
            continue

        shown = set()
        hitset = set(idxs)
        groups = []
        for i in idxs:
            lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
            if groups and lo <= groups[-1][1]:
                groups[-1] = (groups[-1][0], max(groups[-1][1], hi))
            else:
                groups.append((lo, hi))
        for lo, hi in groups:
            for j in range(lo, hi):
                if j in shown:
                    continue
                shown.add(j)
                mark = ":" if j in hitset else "-"
                out.append(f"{rel}{mark}{j + 1}{mark} {lines[j]}")
                nline += 1
                if nline >= FS_GREP_LIMIT:
                    truncated = True
                    break
            if truncated:
                break
            if ctx:
                out.append("--")
        if truncated:
            break

    if not out:
        return DEPS.outcome_success, f"(在 {_rel(base, _fs_root()) or '.'} 下没有匹配 /{pattern}/ 的内容)"
    head = (f"匹配 /{pattern}/：{nmatch} 处，分布在 {nfile} 个文件"
            + (f"（已到 {FS_GREP_LIMIT} 行输出上限，结果被截断，请缩小 root 或加 glob 过滤）"
               if truncated else ""))
    return DEPS.outcome_success, head + "\n" + "\n".join(out)


def _h_list_dir(a):
    """list_dir：列目录内容，目录在前、文件在后，带大小。"""
    try:
        p = guard_path(a.get("path"), want="dir") if str(a.get("path") or "").strip() \
            else _fs_root()
    except PathGuardError as e:
        return DEPS.outcome_failure, str(e)

    try:
        entries = sorted(os.scandir(str(p)), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return DEPS.outcome_failure, f"(列目录失败: {e})"

    dirs, files = [], []
    for e in entries[:FS_LIST_LIMIT]:
        try:
            if e.is_dir():
                dirs.append(f"{e.name}/")
            else:
                files.append(f"{e.name}  ({_human_size(e.stat().st_size)})")
        except OSError:
            files.append(f"{e.name}  (?)")

    total = len(entries)
    lines = dirs + files
    if total > FS_LIST_LIMIT:
        lines.append(f"…（还有 {total - FS_LIST_LIMIT} 项未列出）")
    head = (f"目录: {_rel(p, _fs_root()) or '.'}  ·  "
            f"{len(dirs)} 个子目录 / {len(files)} 个文件"
            + ("（已截断）" if total > FS_LIST_LIMIT else ""))
    if not lines:
        return DEPS.outcome_success, head + "\n(空目录)"
    return DEPS.outcome_success, head + "\n" + "\n".join(lines)


