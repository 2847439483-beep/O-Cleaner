# -*- coding: utf-8 -*-
"""跨设备自检：换一台 Windows 机器后，用它确认本工具能否正常工作。

为什么需要：这个工具依赖几项容易在不同机器上出问题的东西 ——
PowerShell 是否可用、控制台编码、Python 版本、盘符结构、编码规则。
与其等用户遇到奇怪报错，不如一次性自查并给出可操作的结论。

运行：
    python -X utf8 tools/doctor.py
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
HERE = TOOLS.parent
sys.path.insert(0, str(HERE))

OK, BAD, WARN = [], [], []


def ck(name: str, cond: bool, detail: str = "", soft: bool = False) -> None:
    if cond:
        OK.append(name)
        print(f"  [OK]   {name}" + (f"   {detail}" if detail else ""))
    elif soft:
        WARN.append(name)
        print(f"  [注意] {name}" + (f"   {detail}" if detail else ""))
    else:
        BAD.append(name)
        print(f"  [问题] {name}" + (f"   {detail}" if detail else ""))


def main() -> int:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print()
    print("  O-Cleaner 环境自检")
    print("  " + "=" * 66)

    print("\n[1] 运行环境")
    ck("操作系统是 Windows", os.name == "nt",
       platform.platform(), soft=(os.name != "nt"))
    v = sys.version_info
    ck("Python 版本 >= 3.8", v >= (3, 8), f"{v.major}.{v.minor}.{v.micro}")
    ck("Python 支持 f-string 与类型注解", v >= (3, 8))

    print("\n[2] PowerShell（本工具靠它统计目录体积）")
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "$PSVersionTable.PSVersion.ToString()"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        ok = p.returncode == 0 and (p.stdout or "").strip()
        ck("powershell.exe 可用", bool(ok), (p.stdout or "").strip()[:20])
    except Exception as e:  # noqa: BLE001
        ck("powershell.exe 可用", False, str(e))
    # 执行策略：批处理里已用 -ExecutionPolicy Bypass，这里只做提示
    try:
        p2 = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                             "Get-ExecutionPolicy"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        pol = (p2.stdout or "").strip()
        ck("执行策略不阻塞运行", True, f"{pol}（启动脚本已带 Bypass，无需改动）")
    except Exception:
        pass

    print("\n[3] 控制台编码（中文能否正确显示与传递）")
    ck("stdout 编码可用", bool(sys.stdout.encoding), str(sys.stdout.encoding))
    try:
        import app  # noqa: E402
        r = app.run_ps("'{\"t\":\"中文测试\"}'")
        ck("PowerShell 往返中文不乱码", r.get("t") == "中文测试",
           repr(r.get("t")))
    except Exception as e:  # noqa: BLE001
        ck("PowerShell 往返中文不乱码", False, str(e))

    print("\n[4] 盘符与容量")
    import app  # noqa: E402
    drives = app.list_drives()
    ck("能枚举盘符", bool(drives),
       "、".join(f"{d['letter']}:({d['free_h']} 可用)" for d in drives))
    ck("系统盘可识别", any(app.is_system_drive(d["letter"]) for d in drives),
       next((d["letter"] + ":" for d in drives
             if app.is_system_drive(d["letter"])), "无"))

    print("\n[5] 配置与规则")
    for f in ("drives.json", "targets.json", "filetypes.json", "convert.json"):
        p = HERE / "config" / f
        ck(f"存在 config/{f}", p.exists())
    t = app.load_targets()
    ck("targets.json 可解析", bool(t.get("targets")),
       f"{len(t.get('targets', []))} 个目标")
    kb = app.load_filetypes()
    ck("filetypes.json 可解析", bool(kb.get("folder_rules")),
       f"{len(kb.get('folder_rules', []))} 条目录规则")
    # 配置里不该出现写死的盘符/用户名
    blob = json.dumps(t, ensure_ascii=False) + json.dumps(kb, ensure_ascii=False)
    ck("配置未写死 C: 盘路径", "C:\\\\" not in blob.replace("C:\\\\\\\\", ""))
    ck("配置未写死用户名", os.path.basename(os.path.expanduser("~")) not in blob,
       f"（检查是否出现 {os.path.basename(os.path.expanduser('~'))}）")

    print("\n[6] 安全护栏")
    for bad, why in ((r"C:\Windows\System32", "系统目录"),
                     (r"C:\Program Files", "程序目录"),
                     (r"C:\Windows\Installer", "安装缓存"),
                     ("C:\\", "盘符根")):
        ok, _ = app.check_deletable(bad, "tree")
        ck(f"拒绝删除{why}", not ok)
    ok_good, why_good = app.check_deletable(
        os.path.join(os.environ.get("TEMP", "C:\\Temp")), "tree")
    ck("允许清理普通目录", ok_good, why_good)

    print("\n[7] 测试工具链（贡献规则时需要）")
    ck("selftest 存在", (TOOLS / "selftest.py").exists())
    ck("规则 linter 存在", (TOOLS / "lint_rules.py").exists())
    ck("编码规范化工具存在", (TOOLS / "fix-encoding.ps1").exists())
    try:
        p = subprocess.run(["node", "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        ck("node 可用（界面渲染检查需要）", p.returncode == 0,
           (p.stdout or "").strip(), soft=True)
    except Exception:
        ck("node 可用（界面渲染检查需要）", False, "未安装，仅影响开发自测", soft=True)

    print("\n[8] Steam（可选功能）")
    try:
        s = app.discover_steam()
        if s.get("found"):
            ck("检测到 Steam 库", True,
               f"{s['game_count']} 个游戏 / {s['total_size_h']}")
        else:
            ck("检测到 Steam 库", True, "未安装 Steam 或库为空（功能会自动隐藏）",
               soft=True)
    except Exception as e:  # noqa: BLE001
        ck("Steam 检测", False, str(e), soft=True)

    print("\n" + "=" * 66)
    print(f"  通过 {len(OK)} 项，问题 {len(BAD)} 项，注意 {len(WARN)} 项")
    if BAD:
        print("\n  需要处理：")
        for x in BAD:
            print("    - " + x)
    if WARN:
        print("\n  可忽略（不影响使用）：")
        for x in WARN:
            print("    - " + x)
    print("=" * 66 + "\n")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
