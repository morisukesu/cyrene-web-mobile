#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
插件适配器 · Windows → Termux

把桌面端 Cyrene 插件（目录或 ZIP）清成手机端 Termux 能装的形态，并出一份体检报告。

做什么（全部无损）：
  1. 剔掉备份/垃圾文件（*.bak-*、*.pre_*、*.old、__pycache__、.DS_Store …）
  2. 文本文件统一 LF、去掉 UTF-8 BOM
  3. 校验 manifest.json：apiVersion=1、id 合法、entry 是裸文件名且真实存在
  4. 预判宿主上限：ZIP ≤32MB、解包 ≤128MB、成员 ≤4000
  5. 打包成可直接 POST /plugins/import 的 ZIP
  6. 报告里列出「不会替你改」的东西：require("electron")、ipcRenderer 面板、
     Windows 盘符路径、.bat/.ps1、非内置 npm 依赖（照 27.3 的手工修法处理）

不做什么：不改 JS 逻辑、不删运行时依赖、不动源目录。

用法：
  python adapt-plugin.py <插件目录或.zip>          # 转换 + 打包
  python adapt-plugin.py <src> -o 输出目录
  python adapt-plugin.py --scan <插件根目录>        # 批量体检，只出报告
  python adapt-plugin.py <src> --no-zip --json

退出码：0 无阻断项 / 1 有阻断项 / 2 参数或读取失败
"""

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------- 宿主约束（据 cyrene_web.py）
ZIP_MAX_MB = 32               # PLUGIN_ZIP_MAX_MB
UNPACK_MAX_MB = 128           # PLUGIN_UNPACK_MAX_MB
ZIP_MAX_FILES = 4000          # PLUGIN_ZIP_MAX_FILES
API_VERSION = 1               # PLUGIN_API_VERSION
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")     # PLUGIN_ID_RE

TEXT_EXT = {
    ".cjs", ".js", ".mjs", ".json", ".md", ".markdown", ".html", ".htm",
    ".css", ".txt", ".sh", ".yml", ".yaml", ".svg", ".xml", ".csv", ".ini", ".cfg",
}
SKIP_DIRS = {"__pycache__", ".git", ".svn", "node_modules/.cache"}
JUNK_RE = re.compile(r"""(?ix)
    ( \.bak ([.\-_].*)?
    | \.old ([.\-_].*)?
    | \.orig ([.\-_].*)?
    | \.pre_[^/]*
    | \.tmp$
    | ~$
    | ^\.DS_Store$
    | ^Thumbs\.db$
    | ^desktop\.ini$
    )""")
WINDOWS_ONLY = {".bat", ".cmd", ".ps1", ".exe", ".dll", ".node", ".pdb"}
BUILTIN_MODULES = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http",
    "http2", "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream", "string_decoder", "sys",
    "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib", "test", "sea", "sqlite",
}

RE_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
RE_REQUIRE_ELECTRON = re.compile(r"""require\(\s*['"]electron['"]\s*\)""")
RE_IPC = re.compile(r"\bipcRenderer\b")
RE_WIN_ABSPATH = re.compile(r"""(?<![\w/])[A-Za-z]:[\/]""")

LEVEL_BLOCK = "阻断"
LEVEL_HIGH = "高"
LEVEL_MID = "中"
LEVEL_LOW = "低"
LEVEL_ORDER = {LEVEL_BLOCK: 0, LEVEL_HIGH: 1, LEVEL_MID: 2, LEVEL_LOW: 3}


def is_junk(name):
    return bool(JUNK_RE.search(name))


def is_text_file(p):
    return p.suffix.lower() in TEXT_EXT


def to_termux_text(raw):
    """去 BOM + CRLF→LF。返回 (新字节, 是否改过, 说明)。"""
    notes = []
    out = raw
    if out.startswith(b"\xef\xbb\xbf"):
        out = out[3:]
        notes.append("去 BOM")
    if b"\r" in out:
        out = out.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        notes.append("CRLF->LF")
    return out, (out != raw), "+".join(notes)


class Finding:
    def __init__(self, level, code, msg, where=""):
        self.level, self.code, self.msg, self.where = level, code, msg, where

    def as_dict(self):
        return {"level": self.level, "code": self.code,
                "msg": self.msg, "where": self.where}


def collect_files(root):
    """收集相对路径（posix 风格），跳过缓存目录。"""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(rel == d or rel.startswith(d + "/") for d in SKIP_DIRS):
            continue
        out.append(rel)
    return out


def scan_source(root, files):
    """静态体检。返回 (findings, manifest)。"""
    f = []
    cache = {}

    def read(rel):
        if rel not in cache:
            try:
                cache[rel] = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                cache[rel] = ""
        return cache[rel]

    # ---- manifest ----
    mf = None
    for rel in files:
        if rel == "manifest.json" or rel.endswith("/manifest.json"):
            if mf is None or rel.count("/") < mf.count("/"):
                mf = rel
    manifest = {}
    if mf is None:
        f.append(Finding(LEVEL_BLOCK, "NO_MANIFEST",
                         "找不到 manifest.json，宿主认不出插件根", ""))
    else:
        try:
            manifest = json.loads(read(mf))
            if not isinstance(manifest, dict):
                raise ValueError("顶层不是对象")
        except Exception as e:
            f.append(Finding(LEVEL_BLOCK, "MANIFEST_BAD_JSON",
                             "manifest.json 不是合法 JSON: %s" % e, mf))
            manifest = {}
        if manifest:
            av = manifest.get("apiVersion")
            if av is None:
                f.append(Finding(LEVEL_BLOCK, "NO_API_VERSION",
                                 "manifest 缺 apiVersion（宿主门禁要求 = 1）", mf))
            elif av != API_VERSION:
                f.append(Finding(LEVEL_BLOCK, "API_VERSION_MISMATCH",
                                 "apiVersion=%r，宿主只支持 %d" % (av, API_VERSION), mf))
            pid = manifest.get("id")
            if not pid:
                f.append(Finding(LEVEL_BLOCK, "NO_ID", "manifest 缺 id", mf))
            elif not ID_RE.match(str(pid)):
                f.append(Finding(LEVEL_BLOCK, "BAD_ID",
                                 "id=%r 不符合 %s" % (pid, ID_RE.pattern), mf))
            entry = manifest.get("entry")
            if not entry:
                f.append(Finding(LEVEL_BLOCK, "NO_ENTRY", "manifest 缺 entry", mf))
            elif "/" in entry or "\\" in entry or ".." in entry:
                f.append(Finding(LEVEL_BLOCK, "ENTRY_NOT_BARE",
                                 "entry 必须是裸文件名，当前 %r" % entry, mf))
            elif entry not in files:
                f.append(Finding(LEVEL_BLOCK, "ENTRY_MISSING",
                                 "entry 指向的 %r 在包里不存在" % entry, mf))
            panel = manifest.get("settingsPanel")
            htmls = sorted(r for r in files if r.lower().endswith((".html", ".htm")))
            if panel and panel not in files:
                f.append(Finding(LEVEL_BLOCK, "PANEL_MISSING",
                                 "settingsPanel 指向的 %r 不存在" % panel, mf))
            elif not panel and htmls:
                f.append(Finding(LEVEL_LOW, "PANEL_UNDECLARED",
                                 "有 HTML 但 manifest 未声明 settingsPanel，手机端面板入口不会出现"
                                 "（候选：%s）" % ", ".join(htmls[:4]), mf))

    # ---- 逐文件 ----
    total = 0
    nm_present = any(r.startswith("node_modules/") for r in files)
    for rel in files:
        p = root / rel
        try:
            total += p.stat().st_size
        except OSError:
            continue
        if is_junk(p.name):
            f.append(Finding(LEVEL_LOW, "JUNK", "备份/垃圾文件（会自动剔除）", rel))
        if p.suffix.lower() in WINDOWS_ONLY:
            f.append(Finding(LEVEL_MID, "WINDOWS_ONLY_FILE",
                             "Windows 专有文件，Termux 上跑不了（原样保留，请自查引用它的代码）", rel))
        if not is_text_file(p):
            continue
        txt = read(rel)
        for i, line in enumerate(txt.splitlines(), 1):
            if RE_REQUIRE_ELECTRON.search(line):
                f.append(Finding(LEVEL_HIGH, "REQUIRE_ELECTRON",
                                 "裸 require(\"electron\")：宿主启动时会抛 Cannot find module，"
                                 "整颗插件判 failed。用 try/catch 把它包起来，或从启动路径移走",
                                 "%s:%d" % (rel, i)))
            if RE_IPC.search(line):
                f.append(Finding(LEVEL_MID, "IPC_RENDERER",
                                 "用到 ipcRenderer：手机端没有 IPC 端点，面板会白屏；"
                                 "要加「拿不到就显示只读说明」的降级", "%s:%d" % (rel, i)))
            if RE_WIN_ABSPATH.search(line):
                f.append(Finding(LEVEL_MID, "WINDOWS_ABSPATH",
                                 "硬编码 Windows 盘符路径，Termux 上不存在", "%s:%d" % (rel, i)))
        mods = set()
        for m in RE_REQUIRE.finditer(txt):
            name = m.group(1)
            if name.startswith((".", "/")):
                continue
            top = name.split("/")[0]
            if top.startswith("node:") or top in BUILTIN_MODULES:
                continue
            mods.add(name)
        if mods:
            f.append(Finding(LEVEL_LOW if nm_present else LEVEL_HIGH, "EXTERNAL_DEP",
                             "依赖非内置模块 %s —— 手机端%s"
                             % (", ".join(sorted(mods)),
                                "包内自带 node_modules，注意体积" if nm_present
                                else "包里没有 node_modules，require 会失败；改成本地 .cjs 或别搬"),
                             rel))

    total_mb = total / 1048576
    if total_mb > UNPACK_MAX_MB:
        f.append(Finding(LEVEL_BLOCK, "TOO_BIG_UNPACK",
                         "解包后 %.1f MB，超过宿主上限 %d MB" % (total_mb, UNPACK_MAX_MB), ""))
    elif total_mb > ZIP_MAX_MB:
        f.append(Finding(LEVEL_MID, "BIG_SOURCE",
                         "源码 %.1f MB 已超 ZIP 上限 %d MB，压缩后仍超限就会被拒"
                         % (total_mb, ZIP_MAX_MB), ""))
    if len(files) > ZIP_MAX_FILES:
        f.append(Finding(LEVEL_BLOCK, "TOO_MANY_FILES",
                         "%d 个成员，超过上限 %d" % (len(files), ZIP_MAX_FILES), ""))
    return f, manifest


def safe_unpack(zip_path, work):
    """把输入 ZIP 安全解到 work，返回插件根（含 manifest.json 的最浅层）。"""
    work = Path(work)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
                raise ValueError("ZIP 内路径不安全: %r" % info.filename)
            target = (work / name).resolve()
            if not str(target).startswith(str(work.resolve())):
                raise ValueError("ZIP 内路径越界: %r" % info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 256 * 1024)
    best = None
    for p in sorted(work.rglob("manifest.json")):
        rel = p.relative_to(work)
        if best is None or len(rel.parts) < len(best.parts):
            best = rel
    if best is None:
        raise ValueError("ZIP 里找不到 manifest.json")
    return work / best.parent


def build(src_root, out_dir, make_zip=True, shim=True):
    files = collect_files(src_root)
    findings, manifest = scan_source(src_root, files)

    pid = str(manifest.get("id") or src_root.name)
    ver = str(manifest.get("version") or "0")
    pkg = "%s-%s-termux" % (pid, ver)

    pkg_dir = Path(out_dir) / pkg
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True, exist_ok=True)

    dropped, converted = [], []
    for rel in files:
        src = src_root / rel
        if is_junk(Path(rel).name):
            dropped.append(rel)
            continue
        dst = pkg_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if is_text_file(src):
            raw, changed, note = to_termux_text(src.read_bytes())
            dst.write_bytes(raw)
            if changed:
                converted.append((rel, note))
        else:
            shutil.copy2(src, dst)

    # ---- 手机端 Electron 通道转换（主进程侧替身 + 面板侧桥） ----
    stub_rel = None
    panel_notes = []
    verdict, reasons = classify_plugin(src_root, files, manifest, findings)
    if shim:
        needs = False
        for rel in files:
            if rel.lower().endswith((".cjs", ".js", ".mjs")):
                try:
                    if RE_EL_REQUIRE_JS.search((src_root / rel).read_text(
                            encoding="utf-8", errors="replace")):
                        needs = True
                        break
                except OSError:
                    pass
        if needs:
            install_electron_stub(pkg_dir)
            stub_rel = "node_modules/electron/index.js"
            converted.append((stub_rel, "electron 替身"))
        panel_pick, panel_warn = ensure_settings_panel(pkg_dir, manifest, files)
        if panel_pick:
            panel_notes.append({"file": "manifest.json",
                                "notes": ["补 settingsPanel=%s" % panel_pick]})
        for w in panel_warn:
            panel_notes.append({"file": "manifest.json", "notes": [w]})
        for rel in files:
            if not rel.lower().endswith((".html", ".htm")):
                continue
            dst = pkg_dir / rel
            if not dst.is_file():
                continue
            try:
                old_txt = dst.read_text(encoding="utf-8")
            except OSError:
                continue
            new_txt, notes = convert_panel_html(old_txt, pid)
            if notes:
                dst.write_text(new_txt, encoding="utf-8")
                converted.append((rel, "、".join(notes)))
                panel_notes.append({"file": rel, "notes": notes})

    zip_path, zip_mb = None, None
    if make_zip:
        zip_path = Path(out_dir) / ("%s.zip" % pkg)
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for p in sorted(pkg_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(pkg_dir).as_posix())
        zip_mb = zip_path.stat().st_size / 1048576
        if zip_mb > ZIP_MAX_MB:
            findings.append(Finding(LEVEL_BLOCK, "ZIP_TOO_BIG",
                                    "打好的 ZIP %.1f MB，超过宿主上限 %d MB"
                                    % (zip_mb, ZIP_MAX_MB), ""))

    findings.sort(key=lambda x: (LEVEL_ORDER.get(x.level, 9), x.code, x.where))
    return {
        "source": str(src_root), "id": pid, "version": ver,
        "packageDir": str(pkg_dir),
        "zip": str(zip_path) if zip_path else None,
        "zipMB": round(zip_mb, 2) if zip_mb is not None else None,
        "members": len([p for p in pkg_dir.rglob("*") if p.is_file()]),
        "droppedJunk": dropped,
        "converted": converted,
        "electronStub": stub_rel,
        "panels": panel_notes,
        "verdict": verdict,
        "reasons": reasons,
        "findings": [x.as_dict() for x in findings],
    }


def render_report(rep):
    L = ["# 插件适配报告 · %s v%s" % (rep["id"], rep["version"]), ""]
    L.append("- 源：`%s`" % rep["source"])
    L.append("- 产物目录：`%s`" % rep["packageDir"])
    if rep["zip"]:
        L.append("- ZIP：`%s`（**%s MB** / 上限 %d MB）"
                 % (rep["zip"], rep["zipMB"], ZIP_MAX_MB))
    L.append("- 成员数：%d / 上限 %d" % (rep["members"], ZIP_MAX_FILES))
    L.append("- 自动剔除备份垃圾：%d 个" % len(rep["droppedJunk"]))
    L.append("- 文本规范化（LF / 去 BOM）：%d 个" % len(rep["converted"]))
    vd = rep.get("verdict")
    if vd:
        label = {"direct": "可直接用", "review": "需人工复核", "unusable": "不可用"}[vd]
        L.append("- 手机端可用性：**%s**" % label)
    if rep.get("electronStub"):
        L.append("- Electron 替身：已装 `%s`（主进程 require 不再抛错）" % rep["electronStub"])
    if rep.get("panels"):
        L.append("- 面板转换：%d 个 HTML 已接桥（%s）"
                 % (len(rep["panels"]), "、".join(x["file"] for x in rep["panels"])))
    L.append("")
    if rep.get("reasons"):
        L.append("**判定依据（需要你复核的地方）**")
        L.append("")
        for r in rep["reasons"]:
            L.append("- %s" % r)
        L.append("")
    if rep["droppedJunk"]:
        L.append("剔除清单：" + "、".join("`%s`" % x for x in rep["droppedJunk"][:12])
                 + (" 等 %d 个" % len(rep["droppedJunk"]) if len(rep["droppedJunk"]) > 12 else ""))
        L.append("")

    by_level = {}
    for x in rep["findings"]:
        by_level.setdefault(x["level"], []).append(x)
    if not by_level:
        L.append("## 体检\n\n没有发现任何问题。搬过去装一下就行。")
        return "\n".join(L)

    L.append("## 体检（按严重度）")
    L.append("")
    for lv in (LEVEL_BLOCK, LEVEL_HIGH, LEVEL_MID, LEVEL_LOW):
        items = by_level.get(lv)
        if not items:
            continue
        L.append("### %s（%d）" % (lv, len(items)))
        L.append("")
        seen = {}
        for x in items:
            seen.setdefault((x["code"], x["msg"]), []).append(x["where"])
        for (code, msg), wheres in seen.items():
            w = [y for y in wheres if y]
            loc = ""
            if w:
                loc = "　→ " + ("、".join("`%s`" % y for y in w[:6])
                                + (" 等 %d 处" % len(w) if len(w) > 6 else ""))
            L.append("- **[%s]** %s%s" % (code, msg, loc))
        L.append("")
    L.append("> 阻断项要先处理，不然装上去就是 failed；高/中项按上面每条的说明手工改。")
    return "\n".join(L)


def cmd_scan(root, out_dir, quiet=False):
    dirs = [d for d in sorted(root.iterdir()) if d.is_dir() and not is_junk(d.name)]
    if (root / "manifest.json").exists():
        dirs = [root]
    reports = []
    for d in dirs:
        files = collect_files(d)
        findings, manifest = scan_source(d, files)
        total = sum((d / r).stat().st_size for r in files if (d / r).exists())
        reports.append({
            "source": str(d), "id": str(manifest.get("id") or d.name),
            "version": str(manifest.get("version") or "0"),
            "sourceMB": round(total / 1048576, 2), "members": len(files),
            "findings": [x.as_dict() for x in sorted(
                findings, key=lambda y: (LEVEL_ORDER.get(y.level, 9), y.code))],
        })
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scan.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 插件批量体检", "", "- 根目录：`%s`" % root, "- 插件数：%d" % len(reports), "",
             "| 插件 | id | 版本 | 源码MB | 文件 | 阻断 | 高 | 中 | 低 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        c = {lv: 0 for lv in LEVEL_ORDER}
        for x in r["findings"]:
            c[x["level"]] = c.get(x["level"], 0) + 1
        lines.append("| %s | %s | %s | %.2f | %d | %d | %d | %d | %d |" % (
            Path(r["source"]).name, r["id"], r["version"], r["sourceMB"],
            r["members"], c[LEVEL_BLOCK], c[LEVEL_HIGH], c[LEVEL_MID], c[LEVEL_LOW]))
    lines.append("")
    lines.append("## 各插件明细")
    lines.append("")
    for r in reports:
        c = {lv: 0 for lv in LEVEL_ORDER}
        for x in r["findings"]:
            c[x["level"]] = c.get(x["level"], 0) + 1
        lines.append("### %s (%s)" % (Path(r["source"]).name, r["id"]))
        lines.append("")
        seen = {}
        for x in r["findings"]:
            seen.setdefault((x["level"], x["code"], x["msg"]), []).append(x["where"])
        for (lv, code, msg), wheres in sorted(seen.items(),
                                              key=lambda kv: LEVEL_ORDER.get(kv[0][0], 9)):
            w = [y for y in wheres if y]
            loc = ""
            if w:
                loc = "　→ " + "、".join("`%s`" % y for y in w[:4]) + (
                    " 等 %d 处" % len(w) if len(w) > 4 else "")
            lines.append("- **[%s/%s]** %s%s" % (lv, code, msg, loc))
        lines.append("")
    (out_dir / "SCAN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not quiet:
        print("\n".join(lines[:len(reports) + 8]))
    print("\n[scan] 明细 -> %s" % (out_dir / "scan.json"))
    print("[scan] 报告 -> %s" % (out_dir / "SCAN.md"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把桌面端插件清成 Termux 能装的形态（保守模式：只清理 + 体检，不改 JS 逻辑）")
    ap.add_argument("src", nargs="?", help="插件目录 或 插件 ZIP")
    ap.add_argument("-o", "--out", default="_plugin_out", help="输出根目录（默认 _plugin_out）")
    ap.add_argument("--scan", metavar="DIR", help="批量体检一个目录下的所有插件，只出报告")
    ap.add_argument("--no-zip", action="store_true", help="只产出目录，不打 ZIP")
    ap.add_argument("--no-shim", action="store_true",
                    help="不注入 Electron 替身与面板桥（默认注入）")
    ap.add_argument("--json", action="store_true", help="额外写一份 report.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    out_root = Path(args.out)

    if args.scan:
        root = Path(args.scan)
        if not root.is_dir():
            print("不是目录: %s" % root, file=sys.stderr)
            return 2
        return cmd_scan(root, out_root, quiet=args.quiet)

    if not args.src:
        ap.print_help()
        return 2

    src = Path(args.src)
    tmp = None
    try:
        if src.is_file() and src.suffix.lower() == ".zip":
            tmp = out_root / "_unpack"
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True, exist_ok=True)
            real_src = safe_unpack(src, tmp)
        elif src.is_dir():
            real_src = src
        else:
            print("源不存在或不是目录/ZIP: %s" % src, file=sys.stderr)
            return 2
        out_root.mkdir(parents=True, exist_ok=True)
        rep = build(real_src, out_root, make_zip=not args.no_zip,
                    shim=not args.no_shim)
    except Exception as e:
        print("[失败] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2
    finally:
        if tmp is not None and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    md = render_report(rep)
    report_path = Path(rep["packageDir"]) / "_ADAPT_REPORT.md"
    report_path.write_text(md, encoding="utf-8")
    if args.json:
        (report_path.with_suffix(".json")).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(md)
        print("\n报告 -> %s" % report_path)
        if rep["zip"]:
            print("装机用这个 -> %s" % rep["zip"])
    return 1 if any(x["level"] == LEVEL_BLOCK for x in rep["findings"]) else 0




# ==================== 手机端 Electron 通道转换 ====================
# 桌面端插件到处 require("electron")。手机端没有 Electron，两种表现：
#   · 主进程侧（index.cjs）：宿主 await open() 时抛 Cannot find module → 整颗 failed
#   · 面板 UI（ui.html）  ：<script> 里一句 require 抛错 → 整页白屏
# 两边的解法都不动插件里的 JS 逻辑：
#   · 主进程侧 → 往插件目录塞一份 node_modules/electron 替身，Node 解析时
#                先撞上它，require 不再抛；窗口/对话框安静失效
#   · 面板侧   → 面板里那句 require("electron") 改写成「有桥就用桥，没桥就
#                走真 require」，再把桥内联进 HTML。桌面端桥不存在，照旧
# ==================================================================

ELECTRON_STUB_PKG_JSON = '{\n  "name": "electron",\n  "version": "0.0.0-termux-stub",\n  "main": "index.js",\n  "private": true\n}\n'

ELECTRON_STUB_INDEX_JS = '// Termux 降级替身：手机上没有 Electron。\n// 目的只有一个 —— 让 require("electron") 不再抛 Cannot find module，\n// 插件照常 register；窗口/对话框这类桌面专有功能安静失效。\n// 桌面端不受影响：那边有真 electron，永远走真的，这个文件不会进桌面目录。\n"use strict";\n\nconst TAG = "[electron-stub]";\nfunction note(what) {\n  try { console.error(TAG + " " + what + " 在手机端不可用，已安静跳过"); } catch (_) {}\n}\nfunction unsupported(what) {\n  return function () {\n    note(what);\n    const e = new Error(TAG + " " + what + " 在手机端不可用");\n    e.code = "E_CAPABILITY_UNAVAILABLE";\n    return Promise.reject(e);\n  };\n}\n\nclass Emitter {\n  on() { return this; }\n  once() { return this; }\n  off() { return this; }\n  addListener() { return this; }\n  removeListener() { return this; }\n  removeAllListeners() { return this; }\n  emit() { return false; }\n}\n\nclass WebContents extends Emitter {\n  constructor() { super(); this.send = () => {}; }\n  loadFile() { note("webContents.loadFile"); return Promise.resolve(); }\n  loadURL() { note("webContents.loadURL"); return Promise.resolve(); }\n  openDevTools() {}\n  close() {}\n}\n\nclass BrowserWindow extends Emitter {\n  constructor(opts) {\n    super();\n    this._opts = opts || {};\n    this.webContents = new WebContents();\n  }\n  isDestroyed() { return false; }\n  isMaximized() { return false; }\n  isMinimized() { return false; }\n  isVisible() { return false; }\n  focus() { note("BrowserWindow.focus"); }\n  show() {}\n  hide() {}\n  minimize() {}\n  maximize() {}\n  unmaximize() {}\n  close() {}\n  destroy() {}\n  setMenu() {}\n  setTitle() {}\n  loadFile() { note("BrowserWindow.loadFile"); return Promise.resolve(); }\n  loadURL() { note("BrowserWindow.loadURL"); return Promise.resolve(); }\n}\n\nconst ipcMain = new Emitter();\nipcMain.handle = () => {};\nipcMain.handleOnce = () => {};\nipcMain.removeHandler = () => {};\n\nconst app = new Emitter();\napp.getPath = () => "/data/data/com.termux/files/home";\napp.getAppPath = () => "/data/data/com.termux/files/home";\napp.quit = () => {};\napp.exit = () => {};\napp.whenReady = () => Promise.resolve();\n\nconst dialog = {\n  showOpenDialog: () => { note("dialog.showOpenDialog"); return Promise.resolve({ canceled: true, filePaths: [] }); },\n  showSaveDialog: () => { note("dialog.showSaveDialog"); return Promise.resolve({ canceled: true, filePath: undefined }); },\n  showMessageBox: () => { note("dialog.showMessageBox"); return Promise.resolve({ response: 0 }); },\n};\n\nconst shell = {\n  openPath: () => { note("shell.openPath"); return Promise.resolve(""); },\n  openExternal: () => { note("shell.openExternal"); return Promise.resolve(); },\n  showItemInFolder: () => { note("shell.showItemInFolder"); },\n};\n\nconst contextBridge = { exposeInMainWorld: () => { note("contextBridge.exposeInMainWorld"); } };\n\nconst ipcRenderer = new Emitter();\nipcRenderer.invoke = unsupported("ipcRenderer.invoke");\nipcRenderer.send = () => { note("ipcRenderer.send"); };\nipcRenderer.sendSync = () => undefined;\nipcRenderer.postMessage = () => {};\n\nclass WebContentsView {\n  constructor(opts) {\n    this._opts = opts || {};\n    this.webContents = new WebContents();\n  }\n}\n\nconst session = {\n  fromPartition() { note("session.fromPartition"); return new Emitter(); },\n  defaultSession: new Emitter(),\n};\nconst nativeTheme = Object.assign(new Emitter(), {\n  shouldUseDarkColors: false,\n  themeSource: "light",\n});\nconst screen = new Emitter();\nscreen.getPrimaryDisplay = () => ({ workAreaSize: { width: 900, height: 700 } });\nscreen.getAllDisplays = () => [];\nconst Menu = Object.assign(new Emitter(), {\n  setApplicationMenu() {},\n  buildFromTemplate() { return new Emitter(); },\n});\nconst MenuItem = Emitter;\nconst clipboard = { writeText() {}, readText() { return ""; } };\n\nmodule.exports = {\n  BrowserWindow, BaseWindow: BrowserWindow,\n  WebContentsView, WebContents, ipcMain, app, dialog, shell,\n  session, nativeTheme, screen, Menu, MenuItem, clipboard,\n  contextBridge, ipcRenderer,\n  __termuxStub: true,\n};\n'

PANEL_SHIM_JS = '/* 手机端面板桥 —— 由转换脚本自动注入，替代 Electron 的 ipcRenderer。\n   面板在 iframe 里跑：没有 Electron、也没有 require，所以 ipcRenderer.send /\n   invoke 全部转成对宿主的 fetch（POST /plugins/<id>/ipc）。\n   桌面端不受影响：那边 shim 不会注入，面板里那句走的是真 require。 */\n(function () {\n  "use strict";\n  var PLUGIN_ID = "__PLUGIN_ID__";\n  function post(channel, args) {\n    return fetch("/plugins/" + encodeURIComponent(PLUGIN_ID) + "/ipc", {\n      method: "POST",\n      headers: { "Content-Type": "application/json" },\n      body: JSON.stringify({ channel: channel, args: args || [] })\n    }).then(function (r) { return r.json(); });\n  }\n  var api = {\n    invoke: function (channel) {\n      var args = Array.prototype.slice.call(arguments, 1);\n      /* 窗口控制类通道在手机上无窗口可操作，直接给空结果，\n         免得面板的 await 一直挂着 */\n      if (/-win-(minimize|maximize|close)$/.test(String(channel))) {\n        return Promise.resolve(null);\n      }\n      return post(channel, args).then(function (d) {\n        if (d && d.ok) return d.result;\n        throw new Error((d && d.error) || "面板通道调用失败");\n      });\n    },\n    send: function (channel) {\n      var args = Array.prototype.slice.call(arguments, 1);\n      if (/-win-(minimize|maximize|close)$/.test(String(channel))) return;\n      post(channel, args).catch(function () {});\n    },\n    on: function () { return api; },\n    once: function () { return api; },\n    off: function () { return api; },\n    addListener: function () { return api; },\n    removeListener: function () { return api; },\n    removeAllListeners: function () { return api; },\n    sendSync: function () { return null; },\n    postMessage: function () {}\n  };\n  /* 面板里那句 require("electron") 会被脚本改写成：\n     (window.__cyrenePanelBridge ? … .electron : require("electron"))\n     所以这里把 electron 这个对象挂到 window 上 */\n  window.electron = { ipcRenderer: api };\n  window.__cyrenePanelBridge = {\n    electron: window.electron,\n    ipcRenderer: api\n  };\n})();\n'

PANEL_FIT_JS = '\n/* 面板自适应 —— 由转换脚本自动注入。\n   手机端的设置弹层只有手机那么宽，桌面插件面板却按 860 左右的窗口设计：\n   min-width / 固定 px / 两列栅格一摆，右边就被切掉了。\n   这里量一次「自然宽度」，比可用宽度大就整体缩放，让它完整落进手机屏。\n   用 zoom 而不是 transform：zoom 会重排，不会在底部留一条空白或横滚动条。\n   量之前先清掉上次的 zoom，否则量到的是缩过的宽度，会越缩越小。 */\n(function () {\n  "use strict";\n  var root = document.documentElement;\n  function fit() {\n    try {\n      root.style.zoom = "";\n      var avail = root.clientWidth || window.innerWidth || 0;\n      var natural = root.scrollWidth || 0;\n      if (avail > 0 && natural > avail + 1) {\n        root.style.zoom = String(avail / natural);\n      }\n    } catch (e) { /* 量不到就保持原样 */ }\n  }\n  if (document.readyState === "loading") {\n    document.addEventListener("DOMContentLoaded", fit);\n  } else {\n    fit();\n  }\n  window.addEventListener("resize", fit);\n  window.addEventListener("load", fit);\n})();\n'



RE_EL_REQUIRE_JS = re.compile(r"""require\(\s*(['"])electron\1\s*\)""")
EL_GUARD = ('(window.__cyrenePanelBridge ? window.__cyrenePanelBridge.electron '
            ': require("electron"))')

# 手机上根本不存在的宿主能力（manifest.deps 里出现这些，装上也是残的）
DEPS_ABSENT_ON_PHONE = {"scheduler", "speech-input", "channels"}
# 需要桌面端本机服务/软件的信号词（扫代码用，命中即判「需人工」）
DESKTOP_ONLY_HINTS = {
    "chromium": "要本机 Chromium",
    "BrowserWindow.loadURL": "要独立窗口加载网页",
    "comfyui": "要本机 ComfyUI",
    "8188": "要本机 ComfyUI(8188)",
    "indextts": "要桌面端 TTS",
    "zipvoice": "要桌面端 TTS",
}


def guard_electron_require(text):
    """把面板里那句 require("electron") 换成「有桥用桥，没桥走真 require」。

    只动这一个表达式，周围语法原样保留 —— 解构、赋值、直接调用都吃得下。
    桌面端 window.__cyrenePanelBridge 不存在，三元走 else，语义与原来完全一致。
    """
    return RE_EL_REQUIRE_JS.sub(lambda _m: EL_GUARD, text)


def convert_panel_html(text, pid):
    """面板 HTML 转换：改写 electron require + 内联注入面板桥。

    返回 (新文本, 说明列表)。不碰 electron 的 HTML 原样返回。
    """
    notes = []
    uses_el = bool(RE_EL_REQUIRE_JS.search(text))
    uses_ipc = "ipcRenderer" in text
    if not uses_el and not uses_ipc:
        return text, notes
    out = guard_electron_require(text)
    if out != text:
        notes.append("electron require 改走桥")
    shim = PANEL_SHIM_JS.replace("__PLUGIN_ID__", pid)
    tag = ("<script>\n" + shim + "</script>\n"
           + "<script>\n" + PANEL_FIT_JS + "</script>\n")
    m = re.search(r"<head[^>]*>", out, re.I)
    if m:
        out = out[:m.end()] + "\n" + tag + out[m.end():]
    else:
        m2 = re.search(r"<script", out, re.I)
        out = (out[:m2.start()] + tag + out[m2.start():]) if m2 else (tag + out)
    notes.append("注入面板桥")
    return out, notes


def install_electron_stub(pkg_dir):
    """往包内塞一份 node_modules/electron 替身。

    Node 解析 require("electron") 时会从插件目录往上找 node_modules，先撞上
    这份替身，于是不再抛 Cannot find module。插件里的 JS 一个字节都不用改。
    """
    d = Path(pkg_dir) / "node_modules" / "electron"
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(ELECTRON_STUB_PKG_JSON, encoding="utf-8")
    (d / "index.js").write_text(ELECTRON_STUB_INDEX_JS, encoding="utf-8")
    return (d / "index.js")


def ensure_settings_panel(pkg_dir, manifest, files):
    """manifest 没声明 settingsPanel、但包里有面板 HTML 时，自动补一条。

    为什么需要：手机端的面板入口只看 manifest.settingsPanel，没声明就压根
    不会出现入口 —— 面板转得再好也点不到。桌面端这类插件往往是靠 open()
    弹独立窗口显示 UI 的，没有这个字段。

    只在候选唯一时补；多个候选只报告，让人自己挑，不猜。
    """
    if not isinstance(manifest, dict) or manifest.get("settingsPanel"):
        return None, []
    cands = [r for r in files
             if r.lower().endswith((".html", ".htm")) and not is_junk(Path(r).name)]
    cands = [r for r in cands if "/" not in r and "\\" not in r]   # 必须是裸文件名
    if not cands:
        return None, []
    pref = [r for r in cands
            if re.search(r"(panel|studio|settings|ui)\.html?$", r, re.I)]
    pick = pref[0] if len(pref) == 1 else (cands[0] if len(cands) == 1 else None)
    if pick is None:
        return None, ["面板候选不唯一，没敢自动补 settingsPanel：%s"
                      % "、".join(sorted(cands)[:4])]
    mf = Path(pkg_dir) / "manifest.json"
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return None, []
    if not isinstance(data, dict):
        return None, []
    data["settingsPanel"] = pick
    mf.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10),
                    encoding="utf-8")
    manifest["settingsPanel"] = pick
    return pick, []


def classify_plugin(root, files, manifest, findings):
    """判断这颗插件「转完在手机上能不能真用」。

    返回 (verdict, reasons)。verdict 取 direct / review / unusable。
    这是启发式判断，不是保证 —— 报告里会写明依据，让人自己复核。
    """
    reasons = []
    verdict = "direct"

    if any(f.code == "NO_MANIFEST" for f in findings):
        return "unusable", ["没有 manifest.json，宿主认不出插件"]

    deps = manifest.get("deps") if isinstance(manifest, dict) else None
    for d in (deps or []):
        if d in DEPS_ABSENT_ON_PHONE:
            reasons.append(f"manifest.deps 声明了 {d}，手机端没有这套子系统")
    if reasons:
        verdict = "review"

    for f in findings:
        if f.code in ("NO_API_VERSION", "API_VERSION_MISMATCH", "BAD_ID",
                      "NO_ENTRY", "ENTRY_NOT_BARE", "ENTRY_MISSING",
                      "MANIFEST_BAD_JSON", "TOO_BIG_UNPACK", "TOO_MANY_FILES"):
            return "unusable", [f"{f.code}: {f.msg}"]

    for rel in files:
        if not rel.lower().endswith((".cjs", ".js", ".mjs")):
            continue
        try:
            txt = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = txt.lower()
        for needle, why in DESKTOP_ONLY_HINTS.items():
            if needle.lower() in low:
                reasons.append(f"{rel} 命中「{why}」")
    if reasons:
        verdict = "review"

    for f in findings:
        if f.code == "EXTERNAL_DEP" and "没有 node_modules" in f.msg:
            # electron 已经由替身兜住了，它不再是「缺口」
            bare = f.msg.replace("依赖非内置模块 ", "").split(" ——")[0]
            rest = [m.strip() for m in bare.split(",") if m.strip() and m.strip() != "electron"]
            if not rest:
                continue
            reasons.append("%s 依赖没打包进来的 npm 模块: %s" % (f.where, ", ".join(rest)))
            verdict = "review"

    seen = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return verdict, seen


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
