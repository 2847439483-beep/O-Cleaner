#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
O-Cleaner —— 磁盘清理助手（本地 WebUI，Python 标准库实现，零第三方依赖）

用法:
    python app.py                      # 启动并自动打开浏览器
    python app.py --port 8770          # 指定端口
    python app.py --no-browser         # 不自动打开浏览器
    python app.py --scan-only          # 命令行扫描一次并打印
    python app.py --scan-only --drive D

设计要点:
  * 只用标准库，不引入任何 pip 依赖（清理磁盘的工具不该反过来往系统盘装包）
  * 路径全部使用 %LOCALAPPDATA% / %DRIVE% 这类变量，不写死盘符与用户名，
    因此整个文件夹可以整体拷贝到其它 Windows 设备上直接运行
  * 删除目标必须位于某个盘符之下，并受硬编码保护名单拦截
  * 逐个文件删除并统计失败项，被占用的文件跳过而不是整体失败
  * 单个目标出意外不会中断整批清理
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import json
import os
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request as _UrlRequest, urlopen as _urlopen

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
WEB_DIR = BASE_DIR / "web"
TARGETS_PATH = CONFIG_DIR / "targets.json"
DRIVES_PATH = CONFIG_DIR / "drives.json"
CONVERT_PATH = CONFIG_DIR / "convert.json"
FILETYPES_PATH = CONFIG_DIR / "filetypes.json"
CUSTOM_PATH = CONFIG_DIR / "custom-rules.json"
# 游戏中文名映射：纯显示用，不参与任何清理判断
GAMENAMES_PATH = CONFIG_DIR / "game-names.json"
HISTORY_PATH = BASE_DIR / "history.json"
SCHEDULE_PATH = BASE_DIR / "schedule.json"

# 绝不允许删除的目录名（即使配置文件被改坏也拦得住）
PROTECTED_DIR_NAMES = {
    "windows", "system32", "syswow64", "winsxs", "installer", "fonts",
    "assembly", "servicing", "program files", "program files (x86)",
    "programdata", "recovery", "system volume information", "$recycle.bin",
    "users", "default", "public", "appdata", "boot", "perflogs", "msocache",
    "onedrive", "documents and settings",
}
# 这些名字只有在“盘符根目录的直接子目录”层级才整体保护；
# 更深的同名目录（例如 C:\Windows\Temp）允许清理，由更深层的规则把关。
PROTECTED_AT_DRIVE_ROOT = {"windows", "program files", "program files (x86)",
                           "programdata", "users", "recovery", "perflogs"}
# 允许作为“清理容器”的目录名（即使名字本身在通用保护名单里）
ALLOWED_CONTAINER_NAMES = {"temp", "tmp", "cache", "caches", ".cache",
                           "logs", "log", "crashdumps", "minidump", "prefetch"}

CREATE_NO_WINDOW = 0x08000000
IS_WINDOWS = os.name == "nt"


# ======================================================================
#  基础工具
# ======================================================================

def is_admin() -> bool:
    if not IS_WINDOWS:
        try:
            return os.geteuid() == 0  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def expand(path: str, drive: str | None = None) -> str:
    """展开环境变量与盘符占位符。drive 形如 'C:' 或 'C'。"""
    out = str(path)
    if drive:
        letter = drive.rstrip(":").upper()
        out = out.replace("%DRIVE_LETTER%", letter)
        out = out.replace("%DRIVE%", letter + ":")
    return os.path.expandvars(os.path.expanduser(out))


def is_drive_specific(target: dict) -> bool:
    """路径里出现 %DRIVE% / %DRIVE_LETTER% 才算“所选磁盘专属项”。"""
    blob = f"{target.get('path', '')}|{target.get('glob', '')}|{target.get('drive_letter', '')}"
    return "%DRIVE%" in blob


def target_id_for(target: dict, drive: str | None) -> str:
    """盘符专属项的 id 加盘符后缀，避免多盘之间 id 冲突。"""
    tid = target["id"]
    if drive and is_drive_specific(target):
        return f"{tid}@{drive.rstrip(':').upper()}"
    return tid


# PowerShell 5.1 通过管道输出时使用控制台的 ANSI 代码页（中文系统为 GBK），
# 而 Python 侧按 UTF-8 解码 —— 结果中文名会变成乱码（软件 -> ����）。
# 强制让子进程把管道输出设为 UTF-8，与控制台 chcp 无关。
PS_UTF8_PREAMBLE = ("$OutputEncoding=[System.Text.Encoding]::UTF8;"
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;")


def run_ps(script: str, timeout: int = 300):
    """执行一段 PowerShell，返回其输出的 JSON（原样、不做类型强制）。

    返回类型取决于脚本：输出单个对象就是 dict，输出数组就是 list。
    调用方用 `"error" in r` 判失败、用 `r.get(...)` 取字段时，
    两种类型都成立（list 上的 `in` 是成员检查、`.get` 不存在会抛错），
    所以**取字段前先确认拿到的是 dict**，或改用 _as_list() 归一化。

    这里曾经写成「如果是 list 就返回 data[0]」，是个代价很高的错误：
    PS_STEAM_ORPHAN_SIZE 返回的是 `[{path,size},…]` 数组，
    被折成首元素后，analyze_dir_partition 里除第一个以外的目录体积**全变 0**，
    表现为「浏览器缓存目录识别对了但显示 0 B」。
    教训：解析层不要替调用方猜形状，原样返回、由调用方显式归一化。
    """
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-Command", PS_UTF8_PREAMBLE + script]
    env = dict(os.environ)
    # 固定子进程输出编码，不依赖父控制台的 chcp，否则报错信息会乱码
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict = {"env": env}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace", **kwargs)
    except subprocess.TimeoutExpired:
        return {"error": "PowerShell 执行超时"}
    out = (p.stdout or "").strip()
    if not out:
        err = (p.stderr or "").strip().replace("\n", " ")[:400]
        return {"error": f"PowerShell 无输出（exit={p.returncode}）：{err}"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": f"PowerShell 输出不是合法 JSON：{out[:300]}"}


def list_drives() -> list[dict]:
    """枚举可用盘符及容量。"""
    out = []
    if not IS_WINDOWS:
        return out
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            total, used, free = shutil.disk_usage(root)
        except OSError:
            continue
        if total <= 0:
            continue
        out.append({
            "letter": letter, "root": root,
            "total": total, "used": used, "free": free,
            "total_h": human(total), "used_h": human(used), "free_h": human(free),
            "percent": round(used / total * 100, 1),
            # 前端据此决定「清理范围」默认值：系统盘看全部，数据盘只看本盘
            "is_system": is_system_drive(letter),
        })
    return out


def get_disk_info(drive: str | None = None) -> dict:
    letter = (drive or "C").rstrip(":").upper()
    root = f"{letter}:\\"
    if IS_WINDOWS and not os.path.exists(root):
        return {"error": f"盘符 {letter}: 不存在"}
    target = root if IS_WINDOWS else "/"
    try:
        total, used, free = shutil.disk_usage(target)
        return {"letter": letter, "total": total, "used": used, "free": free,
                "total_h": human(total), "used_h": human(used), "free_h": human(free),
                "percent": round(used / total * 100, 1) if total else 0,
                # 前端据此决定「清理范围」默认值：系统盘看全部，数据盘只看本盘
                "is_system": is_system_drive(letter)}
    except Exception as exc:
        return {"error": str(exc)}


# ======================================================================
#  PowerShell 测量脚本
#  注意：用 .format() 注入路径，脚本里所有字面量花括号必须写成双花括号
# ======================================================================

# 注意：这些脚本用 str.replace() 注入路径，占位符写成 __PATH__ / __GLOB__ / __DRIVE__。
# 故意不用 .format()：那会要求把 PowerShell 里所有字面量花括号都写成 {{ }}，
# 极易漏改且报错信息晦涩（已经踩过两次）。

PS_MEASURE_TREE = r"""
$ErrorActionPreference='SilentlyContinue'
$p='__PATH__'
$denied = $false
if (Test-Path -LiteralPath $p) {
    $ErrorActionPreference='Stop'
    try {
        $m = Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction Stop |
             Measure-Object -Property Length -Sum
    } catch {
        # 无权限读取。必须与“目录本来就是空的”区分开，
        # 否则界面上会把“读不到”显示成 0 B，看起来像没东西可清。
        $denied = $true
        $m = $null
    }
    $ErrorActionPreference='SilentlyContinue'
}
[pscustomobject]@{size=[int64]$m.Sum; files=[int]$m.Count; denied=$denied} |
    ConvertTo-Json -Compress
"""

PS_MEASURE_GLOB = r"""
$ErrorActionPreference='SilentlyContinue'
$p='__PATH__'; $g='__GLOB__'
$denied = $false
if (Test-Path -LiteralPath $p) {
    $ErrorActionPreference='Stop'
    try {
        $m = @(Get-ChildItem -LiteralPath $p -Filter $g -Force -File -ErrorAction Stop) |
             Measure-Object -Property Length -Sum
    } catch {
        $denied = $true
        $m = $null
    }
    $ErrorActionPreference='SilentlyContinue'
}
[pscustomobject]@{size=[int64]$m.Sum; files=[int]$m.Count; denied=$denied} |
    ConvertTo-Json -Compress
"""

# 回收站：按盘符定位。用 try/catch 把权限问题收敛成 size=-1，
# 而不是让 PowerShell 以非零退出码结束并丢掉 stdout。
PS_RECYCLE = r"""
$ErrorActionPreference='SilentlyContinue'
$drive='__DRIVE__'
$items=@()
$total=0
$count=0
$denied=$false
if ($drive) {
    $root = $drive + ':\$Recycle.Bin'
    if (Test-Path -LiteralPath $root) {
        $ErrorActionPreference='Stop'
        try {
            $all=@(Get-ChildItem -LiteralPath $root -Recurse -Force -File -ErrorAction Stop)
            $count=$all.Count
            $total=[int64](($all | Measure-Object -Property Length -Sum).Sum)
            $items=@($all | Where-Object { $_.Name -notlike '$I*' -and $_.Length -gt 1MB } |
                     Sort-Object Length -Descending |
                     Select-Object -First 40 @{n='name';e={$_.Name}},@{n='size';e={[int64]$_.Length}},@{n='path';e={$_.FullName}})
        } catch {
            $denied=$true
        }
        $ErrorActionPreference='SilentlyContinue'
    }
}
[pscustomobject]@{size=$total; files=$count; items=$items; denied=$denied} |
    ConvertTo-Json -Compress -Depth 4
"""


def _json_items(raw) -> list[dict]:
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for i in (raw or []):
        try:
            out.append({"name": i.get("name", ""), "size": int(i.get("size") or 0),
                        "path": i.get("path", "")})
        except Exception:
            continue
    return out


def measure_target(t: dict, drive: str | None = None) -> dict:
    """返回 {size, files, [items], [missing], [error]}"""
    try:
        path = expand(t["path"], drive)
        if not os.path.isdir(path):
            return {"size": 0, "files": 0, "missing": True}
        return _measure_existing(t, path, drive)
    except PermissionError:
        return {"size": 0, "files": 0, "error": "拒绝访问（权限不足或受系统保护）"}
    except Exception as exc:  # noqa: BLE001
        return {"size": 0, "files": 0, "error": f"{type(exc).__name__}: {exc}"}


def _measure_existing(t: dict, path: str, drive: str | None) -> dict:
    kind = t.get("kind", "tree")
    if kind == "recyclebin":
        letter = expand(str(t.get("drive_letter") or ""), drive).rstrip(":") \
            or (drive or "").rstrip(":")
        r = run_ps(PS_RECYCLE.replace("__DRIVE__", letter.replace("'", "''")))
        if "error" in r:
            return {"size": 0, "files": 0, "error": r["error"]}
        if r.get("denied"):
            return {"size": 0, "files": 0,
                    "error": "读取被拒绝（回收站需要管理员权限）；以管理员身份运行后可正常统计"}
        return {"size": int(r.get("size") or 0), "files": int(r.get("files") or 0),
                "items": _json_items(r.get("items"))}

    if kind == "files":
        script = (PS_MEASURE_GLOB
                  .replace("__PATH__", path.replace("'", "''"))
                  .replace("__GLOB__", str(t.get("glob", "*")).replace("'", "''")))
    else:
        script = PS_MEASURE_TREE.replace("__PATH__", path.replace("'", "''"))
    r = run_ps(script)
    if r.get("denied"):
        hint = "需要管理员权限" if t.get("needs_admin") else "权限不足，无法读取"
        return {"size": 0, "files": 0,
                "error": f"读取被拒绝（{hint}）；以管理员身份运行后可正常统计"}
    return {"size": int(r.get("size") or 0), "files": int(r.get("files") or 0)}


# ======================================================================
#  全盘占用树（支持逐层下钻）
# ======================================================================

# 只有达到这些门槛的目录才会被继续展开，否则视为叶子。
# 目的：既能挖到 …\steamapps\common\某款大型游戏（上百 GB）
# 这种深层大项，又不会把几十万个小目录全爬一遍。
#
# 展开门槛用「相对父目录的比例」而非固定值：固定阈值会让一个 500 GB 的盘
# 在所有大于阈值的分支上都做一次全量遍历，几分钟跑不完。
# 取 max(绝对下限, 父目录的 8%)，既控制总耗时，又能沿最粗的分支一路挖到底。
DISK_TREE_MIN_DESCEND_RATIO = 0.08
DISK_TREE_MIN_DESCEND_FLOOR = 300 * 1024 * 1024    # 绝对下限 300 MB
DISK_TREE_MIN_CHILD_MB = 50                         # 子项 ≥50 MB 才进入该层清单
DISK_TREE_MAX_DEPTH = 5                             # 最多 5 层（Steam 游戏在第 4 层）
DISK_TREE_MAX_NODES = 420                           # 总节点上限，控制耗时与数据量
DISK_TREE_MAX_PER_DIR = 40                          # 单个目录最多列出的子项数
# 每层最多沿几条最粗的分支继续深入。这是扫描耗时的主要开关：
# 放大会更全面但更慢（每个深分支都要把整棵子树量一遍）。
DISK_TREE_MAX_DESCENTS = 4

PS_TREE_WALK = r"""
$ErrorActionPreference='SilentlyContinue'
$root = '__ROOT__'
$minChild = __MINCHILD__
$ratio = __RATIO__
$floor = __FLOOR__
$maxDepth = __MAXDEPTH__
$maxPerDir = __MAXPERDIR__
$maxNodes = __MAXNODES__
$maxDescents = __MAXDESCENTS__

$script:out = New-Object System.Collections.ArrayList
$script:budget = $maxNodes
$script:sizeCache = @{}

# 目录体积按需测量并缓存 —— 同一目录只量一次
function Size-Of([string]$p) {
    if ($script:sizeCache.ContainsKey($p)) { return $script:sizeCache[$p] }
    $m = Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    $v = [int64]$m.Sum
    $script:sizeCache[$p] = $v
    return $v
}

function Min-Descend([int64]$parentSize) {
    $d = [int64]($parentSize * $ratio)
    if ($d -lt $floor) { $d = [int64]$floor }
    if ($d -gt 4000000000) { $d = [int64]4000000000 }
    return $d
}

function Walk([string]$p, [int]$depth, [string]$parentKey, [int64]$selfSize) {
    if ($depth -gt $maxDepth -or $script:budget -le 0) { return }
    $minDescend = Min-Descend $selfSize

    $ErrorActionPreference='Stop'
    try { $all = @(Get-ChildItem -LiteralPath $p -Force -ErrorAction Stop) }
    catch { $all = @() }
    $ErrorActionPreference='SilentlyContinue'

    $kids = @()
    foreach ($d in ($all | Where-Object { $_.PSIsContainer })) {
        if ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
        $sz = Size-Of $d.FullName
        if ($sz -lt $minChild) { continue }
        $kids += [pscustomobject]@{ type='dir'; name=$d.Name; path=$d.FullName
            size=$sz; modified=$d.LastWriteTime.ToString('yyyy-MM-dd') }
    }
    foreach ($f in ($all | Where-Object { -not $_.PSIsContainer })) {
        if ($f.Length -lt $minChild) { continue }
        $kids += [pscustomobject]@{ type='file'; name=$f.Name; path=$f.FullName
            size=[int64]$f.Length; modified=$f.LastWriteTime.ToString('yyyy-MM-dd') }
    }
    $kids = @($kids | Sort-Object -Property size -Descending | Select-Object -First $maxPerDir)

    # 只在最粗的几条分支上继续深入，控制总耗时
    $descended = 0
    foreach ($k in $kids) {
        if ($script:budget -le 0) { break }
        $script:budget--
        $key = $k.path.ToLower()
        $hasKids = $false
        if ($k.type -eq 'dir' -and $k.size -ge $minDescend -and $depth -lt $maxDepth `
            -and $descended -lt $maxDescents -and $script:budget -gt 0) {
            $descended++
            $hasKids = $true
        }
        [void]$script:out.Add([pscustomobject]@{ parent=$parentKey; name=$k.name
            path=$k.path; type=$k.type; size=$k.size; depth=$depth
            modified=$k.modified; hasChildren=$hasKids })
        if ($hasKids) { Walk $k.path ($depth + 1) $key $k.size }
    }
}

$total = 0
$ErrorActionPreference='Stop'
try { $tops = @(Get-ChildItem -LiteralPath $root -Force -Directory -ErrorAction Stop) }
catch { $tops = @() }
$ErrorActionPreference='SilentlyContinue'

$topList = @()
foreach ($d in $tops) {
    if ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
    $sz = Size-Of $d.FullName
    $topList += [pscustomobject]@{ name=$d.Name; path=$d.FullName; size=$sz
        modified=$d.LastWriteTime.ToString('yyyy-MM-dd') }
    $total += $sz
}
$topList = @($topList | Sort-Object -Property size -Descending)

foreach ($t in $topList) {
    if ($script:budget -le 0) { break }
    $key = $t.path.ToLower()
    $script:budget--
    $hasKids = ($t.size -ge (Min-Descend $total) -and $script:budget -gt 0)
    [void]$script:out.Add([pscustomobject]@{ parent=$root.ToLower(); name=$t.name
        path=$t.path; type='dir'; size=$t.size; depth=1
        modified=$t.modified; hasChildren=$hasKids })
    if ($hasKids) { Walk $t.path 2 $key $t.size }
}

# 盘符根目录下的大文件
$rootFiles = @()
$ErrorActionPreference='Stop'
try { $rf = @(Get-ChildItem -LiteralPath $root -Force -File -ErrorAction Stop) }
catch { $rf = @() }
$ErrorActionPreference='SilentlyContinue'
foreach ($f in $rf) {
    if ($f.Length -lt $minChild) { continue }
    $rootFiles += [pscustomobject]@{ name=$f.Name; path=$f.FullName
        size=[int64]$f.Length; modified=$f.LastWriteTime.ToString('yyyy-MM-dd') }
}

[pscustomobject]@{
    root=$root.ToLower()
    nodes=$script:out
    rootfiles=$rootFiles
    total=[int64]$total
} | ConvertTo-Json -Compress -Depth 4
"""


# ======================================================================
#  WizTree 加速：借它的 MFT 读取，把全盘扫描从几分钟压到几十秒
# ======================================================================

# 为什么要接 WizTree 而不是自己读 MFT：
#   * 实测 C 盘 Get-ChildItem 递归要 258 秒，E 盘 51 秒，D 盘 33 秒；
#     而 WizTree 用 /export 导出同盘明细：C 盘 29 秒、D 盘几秒（快 9 倍）。
#   * 自己读 NTFS MFT 需要管理员权限、要处理 USN 日志与碎片，
#     解析出错就是错报体积，风险与收益完全不成比例。
#   * WizTree 只在**用户已经装了它**时才被调用，我们不做任何安装行为。
#
# 它对本项目的第二个价值更大：导出的 CSV 含**每一个文件的完整路径**，
# 于是「任意目录逐层拆分子项」变成一次 CSV 解析 + 内存查表，
# 不必再为下钻去扫盘。这也正是用户要的「大清理项点开看二级目录」。
WIZTREE_NAMES = ("WizTree64.exe", "WizTree.exe")
WIZTREE_HINTS = (
    r"C:\Program Files\WizTree",
    r"C:\Program Files (x86)\WizTree",
    r"D:\WizTree",
    r"E:\WizTree",
)

# 官方下载地址（diskanalyzer.com，Antibody Software Limited）。
# 优先用**便携版 zip**：免安装、不需要管理员权限，解压到本目录后整个
# O-Cleaner 文件夹拷到别的电脑也能直接用 —— 这正是「可移动」的关键。
WIZTREE_PORTABLE_URL = "https://diskanalyzer.com/files/wiztree_4_33_portable.zip"
WIZTREE_SETUP_URL = "https://diskanalyzer.com/files/wiztree_4_33_setup.exe"
WIZTREE_HOME = "https://diskanalyzer.com/download"
WIZTREE_DIST_LICENSE = "https://diskanalyzer.com/distribution-license"
# 第三方许可说明。**必须如实告知**：它决定了能不能把 WizTree 随包分发。
WIZTREE_LICENSE_NOTE = (
    "WizTree 是 Antibody Software Limited 的第三方软件，个人使用免费。"
    "本项目不随包分发它 —— 下载与解压都发生在你自己这台机器上。"
    "若要把 WizTree 随你的产品一起分发，须先向官方购买 Distribution License。"
)
# 便携版解压到这里（相对 O-Cleaner 根目录，跟着文件夹走）
WIZTREE_PORTABLE_DIR = BASE_DIR / "portable" / "WizTree"



def find_wiztree() -> str | None:
    """找 WizTree 可执行文件。返回完整路径或 None（没装就静默走原路径）。

    查找顺序（**先看本目录自带的便携版**，这样拷到别的电脑能立刻用）：
      1. O-Cleaner\\portable\\WizTree\\ —— 自动下载或用户手工放的便携版，跟着文件夹走
      2. 常见安装位置
      3. 注册表卸载项记录的 InstallLocation
      4. 控制台 PATH 里能直接调到的 wiztree64

    任何一步失败都继续往下，不抛异常 —— 这是可选加速，
    找不到绝不能让主流程报错。
    """
    if not IS_WINDOWS:
        return None
    # 1) 本目录便携版优先：可移动性的关键
    for n in WIZTREE_NAMES:
        p = str(WIZTREE_PORTABLE_DIR / n)
        if os.path.isfile(p):
            return p
    # 2) 常见安装位置
    for d in WIZTREE_HINTS:
        for n in WIZTREE_NAMES:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    # 注册表：读取卸载项里的 InstallLocation（WizTree 会写这个键）
    try:
        import winreg  # type: ignore
        keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, sub in keys:
            try:
                with winreg.OpenKey(hive, sub) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            name = winreg.EnumKey(root, i)
                            with winreg.OpenKey(root, name) as k:
                                disp = str(winreg.QueryValueEx(k, "DisplayName")[0])
                                if "wiztree" not in disp.lower():
                                    continue
                                loc = str(winreg.QueryValueEx(k, "InstallLocation")[0])
                                for n in WIZTREE_NAMES:
                                    p = os.path.join(loc, n)
                                    if os.path.isfile(p):
                                        return p
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass
    # 最后试 PATH
    import shutil as _sh
    for n in WIZTREE_NAMES:
        p = _sh.which(n)
        if p:
            return p
    return None


def wiztree_available() -> dict:
    """给界面用的探测结果（缓存，避免每帧都查注册表）。"""
    global _WIZ_CACHE
    if _WIZ_CACHE is None:
        p = find_wiztree()
        _WIZ_CACHE = {"found": bool(p), "path": p or ""}
    return _WIZ_CACHE


def _wiz_ok() -> bool:
    """是否启用 WizTree 加速。可用环境变量 OC_NO_WIZTREE=1 关掉
    （自检与排查时要能强制走 PowerShell 路径做对照）。"""
    if os.environ.get("OC_NO_WIZTREE"):
        return False
    return bool(wiztree_available().get("found"))


def wiztree_status() -> dict:
    """给「新用户引导」用的完整状态：找到没有、是不是本目录自带、怎么装。"""
    wz = wiztree_available()
    path = str(wz.get("path") or "")
    portable = bool(path) and os.path.normpath(path).lower().startswith(
        os.path.normpath(str(WIZTREE_PORTABLE_DIR)).lower())
    ver = ""
    if path:
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"(Get-Item -LiteralPath '{path.replace(chr(39), chr(39)*2)}')"
                 ".VersionInfo.FileVersion"],
                capture_output=True, timeout=20,
                creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
            ver = (out.stdout or b"").decode("utf-8", "replace").strip()
        except Exception:
            ver = ""
    return {
        "found": bool(path),
        "path": path,
        "portable": portable,               # 本目录自带 → 拷走就能用
        "version": ver,
        "enabled": _wiz_ok(),
        "portable_dir": str(WIZTREE_PORTABLE_DIR),
        "portable_rel": "portable\\WizTree",
        "download_url": WIZTREE_PORTABLE_URL,
        "setup_url": WIZTREE_SETUP_URL,
        "homepage": WIZTREE_HOME,
        "dist_license_url": WIZTREE_DIST_LICENSE,
        "license_note": WIZTREE_LICENSE_NOTE,
        "cached_drives": sorted(_WIZ_DIRS.keys()),
    }


def acquire_wiztree(timeout: int = 300) -> dict:
    """下载 WizTree **便携版**并解压到本目录，供这台或别的电脑直接使用。

    为什么用便携版 zip 而不是安装程序：
      * 免安装、不需要管理员权限；
      * 解压后就在 O-Cleaner\\portable\\WizTree\\ 里，整个文件夹拷到
        别的电脑（含 U 盘）即可直接用 —— 这是「移动可用」的关键；
      * 不写注册表、不装进 Program Files，卸载就是删文件夹。

    合法性：这是"用户在自己机器上下载并使用"，不是我们随包分发 WizTree。
    随包分发需要官方的 Distribution License，所以**安装包不会包含它**，
    界面上也会把这一点如实写明（见 WIZTREE_LICENSE_NOTE）。
    """
    import io
    import zipfile

    result = {"ok": False, "error": "", "path": "", "version": "",
              "portable_dir": str(WIZTREE_PORTABLE_DIR), "log": []}

    # 已经有了就直接返回，不重复下载
    if wiztree_available().get("found"):
        st = wiztree_status()
        result.update(ok=True, path=st["path"], version=st["version"],
                      already=True)
        result["log"].append("已经可用，无需下载")
        return result

    try:
        WIZTREE_PORTABLE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result["error"] = f"无法创建目录 {WIZTREE_PORTABLE_DIR}：{e}"
        return result

    result["log"].append(f"从官方地址下载便携版：{WIZTREE_PORTABLE_URL}")
    try:
        req = _UrlRequest(
            WIZTREE_PORTABLE_URL, headers={"User-Agent": "O-Cleaner/2.2"})
        with _urlopen(req, timeout=timeout) as resp:
            ctype = str(resp.headers.get("Content-Type") or "")
            raw = resp.read()
    except Exception as e:
        result["error"] = (f"下载失败：{type(e).__name__}: {e}。"
                           f"可手动下载 {WIZTREE_PORTABLE_URL} 后把内容解压到 "
                           f"{WIZTREE_PORTABLE_DIR}")
        return result

    result["log"].append(f"下载完成 {human(len(raw))}")
    # 基本校验：必须是 zip（PK 头）且体积合理，避免把错误页面当成压缩包解开
    if not raw.startswith(b"PK\x03\x04"):
        result["error"] = (f"下载到的不是 zip（Content-Type={ctype}，"
                           f"前 4 字节={raw[:4]!r}），已中止且未写入任何文件")
        return result
    if len(raw) < 1_000_000:
        result["error"] = f"下载体积异常偏小（{human(len(raw))}），已中止"
        return result

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            # 防目录穿越：压缩包里的路径不能含 .. 或绝对路径
            bad = [n for n in names
                   if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/")]
            if bad:
                result["error"] = f"压缩包内含可疑路径，已中止：{bad[:3]}"
                return result
            zf.extractall(str(WIZTREE_PORTABLE_DIR))
        result["log"].append(f"解压 {len(names)} 个文件到 "
                             f"{WIZTREE_PORTABLE_DIR}")
    except Exception as e:
        result["error"] = f"解压失败：{type(e).__name__}: {e}"
        return result

    # 解压后重新探测（find_wiztree 会优先看这个便携目录）
    global _WIZ_CACHE
    _WIZ_CACHE = None
    st = wiztree_status()
    if not st["found"]:
        result["error"] = (f"解压完成但没找到 WizTree64.exe，"
                           f"请检查 {WIZTREE_PORTABLE_DIR} 的内容")
        return result
    result.update(ok=True, path=st["path"], version=st["version"])
    result["log"].append(f"已就绪：{st['path']}")
    return result


def _wiz_ok() -> bool:
    """是否启用 WizTree 加速。可用环境变量 OC_NO_WIZTREE=1 关掉
    （自检与排查时要能强制走 PowerShell 路径做对照）。"""
    if os.environ.get("OC_NO_WIZTREE"):
        return False
    return bool(wiztree_available().get("found"))


def _wiztree_cleanup(csv_path: str) -> bool:
    """删除 WizTree 导出的 CSV。返回是否删掉。

    导出文件可能上到一百多 MB（C 盘 77 万行），解析完就必须清掉。
    删除失败不是致命问题，但**绝不能因此抛异常**打断扫描 —— 表已经在内存里了。
    """
    if not csv_path:
        return False
    try:
        os.remove(csv_path)
        return True
    except OSError:
        return False


def cleanup_stale_wiztree_exports() -> int:
    """清理上次异常退出（崩溃 / 强杀）遗留的导出文件。

    为什么需要：正常路径已经在 finally 里删了，但如果进程被强杀，
    finally 不会执行，那些一百多 MB 的文件就会永远躺在临时目录里。
    启动时扫一遍把遗留清掉，成本可忽略（一次 glob）。
    """
    n = 0
    try:
        pat = re.compile(r"^oc-wiztree-[A-Z]-\d+\.csv$", re.I)
        for name in os.listdir(tempfile.gettempdir()):
            if not pat.match(name):
                continue
            try:
                os.remove(os.path.join(tempfile.gettempdir(), name))
                n += 1
            except OSError:
                continue
    except OSError:
        pass
    return n


def _wiztree_export(drive: str, timeout: int = 300) -> tuple[str | None, str]:
    """调用 WizTree 导出整盘明细 CSV。返回 (csv 路径, 错误信息)。

    命令行：WizTree64.exe <盘符> /export="<csv>"
    这个语法是实测出来的 —— 官方文档在本环境不可达（DNS 被拦），
    而 `/exportfiles` 那种写法会直接弹 GUI 并挂住不返回。
    导出完成后 WizTree 自己退出（实测 exit=0），无需我们杀进程。
    """
    exe = (wiztree_available().get("path") or "")
    if not exe:
        return None, "未安装 WizTree"
    letter = (drive or "C").rstrip(":").upper() + ":"
    csv_path = os.path.join(tempfile.gettempdir(),
                            f"oc-wiztree-{letter.rstrip(':')}-{os.getpid()}.csv")
    try:
        if os.path.exists(csv_path):
            os.remove(csv_path)
    except OSError:
        pass
    cmd = [exe, letter + "\\", f'/export={csv_path}']
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
    except subprocess.TimeoutExpired:
        return None, f"WizTree 导出超时（{timeout} 秒）"
    except OSError as e:
        return None, f"WizTree 启动失败：{e}"
    if not os.path.isfile(csv_path):
        err = (p.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return None, f"WizTree 未生成导出文件（exit={p.returncode}）{err}"
    return csv_path, ""


def _wiztree_children(dirs: dict[str, int], path: str, limit: int = 60) -> list[dict]:
    """从目录体积表里取出某个目录的**直接**子目录（按体积降序）。

    判「直接子目录」用路径里的反斜杠个数，比 endswith/startswith 组合可靠：
    只比较层数就不会把 D:\\a\\bc 误当成 D:\\a\\b 的子项。
    """
    key = os.path.normpath(path).lower()
    pre = key if key.endswith("\\") else key + "\\"
    depth = pre.count("\\")
    out = []
    for p, sz in dirs.items():
        if p == key or not p.startswith(pre):
            continue
        if p.count("\\") != depth:
            continue
        out.append({"path": p, "name": p.rstrip("\\").split("\\")[-1],
                    "size": sz, "type": "dir"})
    out.sort(key=lambda x: -x["size"])
    return out[:limit]


def _dir_has_any_child(path: str) -> bool:
    """廉价探测目录里是否有任何条目（取到第一个就返回）。

    无 WizTree 内存表时无法零成本预判「能否展开」，全量统计体积又太贵；
    一次性 scandir 只花一次系统调用，整棵树几百个节点实测也就几十毫秒。
    权限不足/路径消失一律按「不可展开」处理，绝不让探测本身抛异常。
    """
    try:
        with os.scandir(path) as it:
            for _ in it:
                return True
    except OSError:
        return False
    return False


def _wiztree_parse(csv_path: str) -> dict:
    """解析 WizTree CSV，返回 {"dirs": {路径:体积}, "files": {父目录:[(名,大小)]}, "rows": n}。

    为什么要连「直接文件」也存下来：原来那套展开逻辑每层会同时列出**子目录与大文件**
    （`hasChildren` 与文件条目混在同一层），只看目录会让用户觉得「文件不见了」。
    WizTree 的 CSV 正好把两者都给全了，顺手存下来即可完全复刻原行为。

    只保留达到体积门槛的条目，避免把 C 盘 77 万行全部留在内存里。
    """
    dirs: dict[str, int] = {}
    files: dict[str, list] = {}
    floor = max(0, int(DISK_TREE_MIN_CHILD_MB)) * 1024 * 1024
    n = 0
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as fh:
        fh.readline()                  # WizTree 版本与捐赠提示
        fh.readline()                  # 列名
        for line in fh:
            n += 1
            # 路径一定被双引号包住，所以按「第一个引号闭合」切分比按逗号切分安全
            # （路径里可以含逗号，按逗号切会把路径截断）。
            if not line.startswith('"'):
                continue
            end = line.find('"', 1)
            if end <= 1:
                continue
            path = line[1:end]
            parts = line[end + 1:].lstrip(",").split(",", 1)
            if not parts:
                continue
            try:
                size = int(parts[0])
            except (ValueError, TypeError):
                continue
            low = os.path.normpath(path).lower()
            if path.endswith("\\"):
                if size >= floor:
                    dirs[low] = size
            elif size >= floor:
                parent = os.path.dirname(low)
                files.setdefault(parent, []).append(
                    (path.rstrip("\\").split("\\")[-1], size, path))
    return {"dirs": dirs, "files": files, "rows": n}


def _wiztree_raw_nodes(root: str, table: dict,
                       max_depth: int, min_child: int, ratio: float,
                       floor: int, max_per_dir: int, max_nodes: int,
                       max_descents: int) -> tuple[list[dict], int]:
    """用 WizTree 的体积表复刻 PS_TREE_WALK 的展开策略，产出同一形状的原始节点。

    刻意**逐行对应** PowerShell 那版的逻辑（同样的 minChild 过滤、同样的
    「父目录体积 × ratio 且不低于 floor」下探门槛、同样的每层取前 maxPerDir、
    同样的每条分支最多下探 maxDescents 个、同样的总节点预算），
    否则两条扫描路径会给出不同的树，界面表现就不一致了。
    区别只在于：这里查的是内存表，不再递归遍历磁盘。
    """
    dirs = table["dirs"]
    files = table["files"]
    out: list[dict] = []
    budget = max_nodes
    root_key = os.path.normpath(root).lower()

    def min_descend(parent_size: int) -> int:
        d = int(parent_size * ratio)
        return max(int(floor), min(d, 4_000_000_000))

    def kids_of(path_key: str) -> list[dict]:
        """某一层的候选子项（目录 + 文件），按体积降序取前 max_per_dir。"""
        pre = path_key if path_key.endswith("\\") else path_key + "\\"
        depth = pre.count("\\")
        items = []
        for p, sz in dirs.items():
            if p == path_key or not p.startswith(pre):
                continue
            if p.count("\\") != depth:
                continue
            if sz < min_child:
                continue
            items.append({"type": "dir", "name": p.rstrip("\\").split("\\")[-1],
                          "path": p, "size": sz, "modified": ""})
        for name, sz, full in files.get(path_key, ()):
            if sz >= min_child:
                items.append({"type": "file", "name": name, "path": full.lower(),
                              "size": sz, "modified": ""})
        items.sort(key=lambda x: -x["size"])
        return items[:max_per_dir]

    def walk(path_key: str, depth: int, parent_key: str, self_size: int) -> None:
        nonlocal budget
        if depth > max_depth or budget <= 0:
            return
        md = min_descend(self_size)
        descended = 0
        for k in kids_of(path_key):
            if budget <= 0:
                break
            budget -= 1
            has_kids = False
            if (k["type"] == "dir" and k["size"] >= md and depth < max_depth
                    and descended < max_descents and budget > 0):
                descended += 1
                has_kids = True
            out.append({"parent": parent_key, "name": k["name"], "path": k["path"],
                        "type": k["type"], "size": k["size"], "depth": depth,
                        "modified": k["modified"], "hasChildren": has_kids})
            if has_kids:
                walk(k["path"], depth + 1, k["path"], k["size"])

    # 顶层：盘符根的直接子目录（原版对第一层不做 minChild 过滤）
    tops = []
    pre = root_key if root_key.endswith("\\") else root_key + "\\"
    depth = pre.count("\\")
    for p, sz in dirs.items():
        if p == root_key or not p.startswith(pre) or p.count("\\") != depth:
            continue
        tops.append({"name": p.rstrip("\\").split("\\")[-1], "path": p, "size": sz})
    tops.sort(key=lambda x: -x["size"])
    total = sum(t["size"] for t in tops)

    for t in tops:
        if budget <= 0:
            break
        budget -= 1
        has_kids = t["size"] >= min_descend(total) and budget > 0
        out.append({"parent": root_key, "name": t["name"], "path": t["path"],
                    "type": "dir", "size": t["size"], "depth": 1,
                    "modified": "", "hasChildren": has_kids})
        if has_kids:
            walk(t["path"], 2, t["path"], t["size"])

    # 盘符根下的大文件（原版同样单独列在根层）
    rootfiles = []
    for name, sz, full in files.get(root_key, ()):
        if sz >= min_child:
            rootfiles.append({"name": name, "path": full.lower(),
                              "size": sz, "modified": ""})
    return out, rootfiles, total


def scan_disk_overview(drive: str, max_depth: int = DISK_TREE_MAX_DEPTH) -> dict:
    """磁盘概览：一次遍历生成整棵占用树，供界面逐层下钻。

    两条扫描路径，优先用快的那条：

    1. **WizTree**（若本机装了它）：调用 `WizTree64.exe <盘> /export=<csv>`，
       借它读 NTFS MFT 的能力拿全盘明细。实测 C 盘 258 秒 → 22 秒、
       D 盘 33 秒 → 3 秒（约 9 倍）。导出的 CSV 含每个文件的完整路径，
       于是「任意目录逐层拆子项」也变成内存查表。
    2. **PowerShell 递归**（兜底，用户没装 WizTree 时）：整棵树在**一个**
       PowerShell 进程内递归完成。早期版本每个目录起一个进程来回测量，
       E 盘（304 节点）要跑 5 分钟；合并进单进程后降到 1 分钟左右 ——
       进程启动开销才是主要成本。

    两条路径用**同一套展开参数与同一套节点形状**，所以界面表现一致。
    展开策略：沿「体积最粗的几条分支」逐层深入，
    这样才能挖到 …\\steamapps\\common\\某款大型游戏
    这种第 4 层、单个体积上百 GB 的大项。
    """
    letter = (drive or "C").rstrip(":").upper()
    root = letter + ":\\"
    if not IS_WINDOWS or not os.path.exists(root):
        return {"error": f"盘符 {letter}: 不存在", "nodes": []}

    depth_cap = max(1, min(max_depth, 6))
    engine = "powershell"
    table: dict | None = None

    if _wiz_ok():
        csv_path, werr = _wiztree_export(letter)
        if csv_path:
            try:
                t0 = time.time()
                table = _wiztree_parse(csv_path)
                parsed = time.time() - t0
                roots, rootfiles, total = _wiztree_raw_nodes(
                    root, table, depth_cap,
                    DISK_TREE_MIN_CHILD_MB * 1024 * 1024,
                    DISK_TREE_MIN_DESCEND_RATIO, DISK_TREE_MIN_DESCEND_FLOOR,
                    DISK_TREE_MAX_PER_DIR, DISK_TREE_MAX_NODES,
                    DISK_TREE_MAX_DESCENTS)
                r = {"root": root.lower(), "nodes": roots, "rootfiles": rootfiles,
                     "total": total}
                engine = "wiztree"
                # 表留在内存里，供「逐层拆子项」与后续下钻直接查
                with _lock:
                    _WIZ_DIRS[letter] = {"table": table, "at": time.time(),
                                         "parse_s": round(parsed, 2)}
            except Exception as e:
                table = None
                werr = f"WizTree 导出解析失败：{e}"
            finally:
                # 导出的 CSV 很大（C 盘约 120 MB，含 77 万行），
                # 表已经进内存了就必须删掉它 —— 否则一个「磁盘清理工具」
                # 每扫一次就往临时目录塞一百多 MB，自己变成垃圾制造者。
                # 这条是被实测打脸后加的：连扫十几次后临时目录里攒了 24 个
                # 导出文件、约 1.2 GB。
                _wiztree_cleanup(csv_path)
        if engine != "wiztree":
            # 加速路径失败不影响可用性，静默回退到 PowerShell；失败原因记在 payload 里
            pass
    else:
        werr = "未安装 WizTree"

    if engine != "wiztree":
        script = (PS_TREE_WALK
                  .replace("__ROOT__", root.replace("'", "''"))
                  .replace("__MINCHILD__", str(DISK_TREE_MIN_CHILD_MB * 1024 * 1024))
                  .replace("__RATIO__", repr(DISK_TREE_MIN_DESCEND_RATIO))
                  .replace("__FLOOR__", str(DISK_TREE_MIN_DESCEND_FLOOR))
                  .replace("__MAXDEPTH__", str(depth_cap))
                  .replace("__MAXPERDIR__", str(DISK_TREE_MAX_PER_DIR))
                  .replace("__MAXNODES__", str(DISK_TREE_MAX_NODES))
                  .replace("__MAXDESCENTS__", str(DISK_TREE_MAX_DESCENTS)))
        r = run_ps(script, timeout=1800)
        if "error" in r:
            return {"error": r["error"], "nodes": []}

    # Steam 游戏整合进磁盘树：读一次 Steam 清单，得到各库的 steamapps\\common 路径。
    # 这样全盘占用树里能直接标出「这个目录是一个 Steam 游戏」，
    # 用户不必在「磁盘空间」和「Steam 页面」之间来回对照 —— 这正是本次的诉求。
    # discover_steam() 只读几 KB 清单，实测 0.6 秒，代价可接受。
    steam_common: set[str] = set()
    steam_libs: set[str] = set()
    try:
        ds = discover_steam()
        for g in (ds.get("games") or []):
            lib = str(g.get("library") or "")
            if lib:
                steam_libs.add(os.path.normpath(lib).lower())
        for lib in list(steam_libs):
            steam_common.add(os.path.normpath(
                os.path.join(lib, "steamapps", "common")).lower())
    except Exception:
        pass
    # Epic/GOG 游戏目录索引（带 60 秒缓存，清单级读取，代价可忽略）
    try:
        other_game_dirs, other_game_libs = game_install_dirs()
    except Exception:
        other_game_dirs, other_game_libs = {}, set()

    def mk(n: dict, parent_default: str) -> dict:
        name = str(n.get("name") or "")
        path = str(n.get("path") or "")
        item = {
            "parent": str(n.get("parent") or parent_default).lower(),
            "name": name, "path": path,
            "depth": int(n.get("depth") or 1),
            "type": str(n.get("type") or "dir"),
            "size": int(n.get("size") or 0),
            "modified": str(n.get("modified") or ""),
            "has_children": bool(n.get("hasChildren")),
        }
        info = identify(name, path, item["type"])
        info.pop("match_score", None)      # 内部打分不进 payload
        item.update(info)
        item["size_h"] = human(item["size"])
        # Steam 归属标记：位于某个 steamapps\common\ 正下方的就是游戏本体
        low = os.path.normpath(path).lower()
        parent_low = os.path.dirname(low)
        if parent_low in steam_common:
            item["steam_game"] = True
            item["category"] = "游戏"
            item["cleanable"] = False      # 游戏本体不是垃圾，工具不删
            item["risk"] = "高"
            # 磁盘树里也用中文名，和 Steam 面板保持一致
            disp, zh = game_display_name(None, name)
            if zh:
                item["display_name"] = disp
                item["zh_name"] = zh
            if not item.get("what"):
                item["what"] = "Steam 游戏安装目录，体积来自 Steam 清单与磁盘扫描。"
        elif low in steam_common or low in steam_libs:
            item["steam_library"] = True
            item["category"] = "游戏"
            if not item.get("what"):
                item["what"] = "Steam 游戏库的位置，下面是各款游戏的安装目录。"
        elif low in other_game_dirs:
            # Epic/GOG 游戏本体：按清单里的精确安装路径命中，宁缺毋滥
            plat = other_game_dirs[low]
            item["game_platform"] = plat
            item["category"] = "游戏"
            item["cleanable"] = False      # 游戏本体不是垃圾，工具不删
            item["risk"] = "高"
            if not item.get("what"):
                item["what"] = ("Epic Games 安装的游戏本体。" if plat == "epic"
                                else "GOG 安装的游戏本体。")
        elif low in other_game_libs:
            item["game_library"] = True
            item["category"] = "游戏"
            if not item.get("what"):
                item["what"] = "Epic/GOG 游戏库目录，下面是各款游戏的安装目录。"
        return item

    root_key = str(r.get("root") or root).lower()
    nodes: list[dict] = []
    for n in _as_list(r.get("nodes")):
        try:
            nodes.append(mk(n, root_key))
        except Exception:
            continue
    for f in _as_list(r.get("rootfiles")):
        try:
            nodes.append(mk({**f, "type": "file", "depth": 1}, root_key))
        except Exception:
            continue
    for i, n in enumerate(nodes):
        n["id"] = i
    # 由数据决定「能否展开」，前端不必去猜 ——
    # has_children 表示已经在树里有子节点；此外还有「子项未入树但目录确实可展开」的情况。
    # 有 WizTree 时可以直接查内存表，比只靠树里有没有子节点更准：
    # 被体积门槛挡在树外的目录（<50 MB）照样应该允许点开看。
    wiz_dirs = (_WIZ_DIRS.get(letter, {}).get("table", {}) or {}).get("dirs") \
        if engine == "wiztree" else None
    parent_keys = {n["parent"] for n in nodes}
    for n in nodes:
        key = n["path"].lower()
        if n["type"] != "dir":
            n["can_drill"] = False
            continue
        if n.get("has_children") or key in parent_keys:
            n["can_drill"] = True
        elif wiz_dirs is not None:
            n["can_drill"] = bool(_wiztree_children(wiz_dirs, n["path"], 1))
        else:
            # 无 WizTree：退化为一次性 scandir 探测，让深层下钻在无加速环境也可用
            n["can_drill"] = _dir_has_any_child(n["path"])

    disk = get_disk_info(letter)
    return {
        "drive": letter,
        "root": root_key,
        "nodes": nodes,
        "explained": len([n for n in nodes if n.get("recognized")]),
        "node_count": len(nodes),
        "scanned_total": int(r.get("total") or 0),
        "scanned_total_h": human(int(r.get("total") or 0)),
        "min_child_h": human(DISK_TREE_MIN_CHILD_MB * 1024 * 1024),
        "max_depth": max_depth,
        "disk": disk,
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_admin": is_admin(),
        # 用了哪条扫描路径 —— 界面上要如实告诉用户「快是因为借了 WizTree」
        "engine": engine,
        "engine_label": ("WizTree MFT 加速" if engine == "wiztree"
                         else "PowerShell 递归（未安装 WizTree）"),
        "engine_note": (f"WizTree 导出 {table['rows']:,} 行，解析 "
                        f"{_WIZ_DIRS.get(letter, {}).get('parse_s', '?')} 秒"
                        if engine == "wiztree" else (werr or "")),
        "subdir_ready": bool(engine == "wiztree"),
    }


def list_subdirs(drive: str, path: str, limit: int = 80) -> dict:
    """列出任意目录的直接子目录（含体积），用于「大清理项逐层拆分子项」。

    为什么需要单独一个接口，而不是让前端把整棵树都拿下去：
    C 盘有 77 万行文件记录，全量塞给浏览器既慢又浪费。
    WizTree 的导出表已经常驻内存，所以这里回答任意深度的下钻都是
    O(表大小) 的纯查表操作，实测 0 毫秒级 —— 深度完全不受树深上限约束。

    没有 WizTree（表不存在）时回退到现场测量直接子目录，仍然可用。
    """
    letter = (drive or "C").rstrip(":").upper()
    if not path:
        return {"error": "缺少 path 参数", "children": []}
    p = os.path.normpath(path)
    if not os.path.isdir(p):
        return {"error": "目录不存在", "children": []}

    children: list[dict] = []
    cached = _WIZ_DIRS.get(letter)
    if cached and cached.get("table"):
        table = cached["table"]
        children = _wiztree_children(table["dirs"], p, limit)
        for c in children:
            cp = os.path.normpath(c["path"])
            try:
                c["modified"] = datetime.fromtimestamp(
                    os.path.getmtime(cp)).strftime("%Y-%m-%d")
            except OSError:
                c["modified"] = ""
        # 补上直接大文件，否则「文件去哪了」会让人困惑
        for name, sz, full in (table["files"].get(os.path.normpath(p).lower())
                               or ()):
            children.append({"path": full.lower(), "name": name, "size": sz,
                             "modified": "", "type": "file"})
        children.sort(key=lambda x: -x["size"])
        children = children[:limit]
        engine = "wiztree"
    else:
        engine = "powershell"
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            children.append({"path": e.path, "name": e.name,
                                             "size": _size_of_dir(e.path),
                                             "modified": datetime.fromtimestamp(
                                                 e.stat().st_mtime
                                             ).strftime("%Y-%m-%d"),
                                             "type": "dir"})
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat()
                            children.append({"path": e.path, "name": e.name,
                                             "size": st.st_size,
                                             "modified": datetime.fromtimestamp(
                                                 st.st_mtime).strftime("%Y-%m-%d"),
                                             "type": "file"})
                    except OSError:
                        continue
        except OSError as e:
            return {"error": f"无法读取目录：{e}", "children": []}
        children.sort(key=lambda x: -x["size"])
        children = children[:limit]

    for c in children:
        c["size_h"] = human(c["size"])
        info = identify(c["name"], c["path"], c.get("type") or "dir")
        info.pop("match_score", None)
        c.update(info)
        c["can_drill"] = bool(c.get("type") == "dir"
                              and (os.path.isdir(c["path"])
                                   or (cached and cached.get("table")
                                       and _wiztree_children(
                                           cached["table"]["dirs"], c["path"], 1))))
    # Epic/GOG 游戏标注：子项恰好是某游戏安装目录时给出与占用树一致的身份
    try:
        gdirs, glibs = game_install_dirs()
    except Exception:
        gdirs, glibs = {}, set()
    if gdirs or glibs:
        for c in children:
            if c.get("type") != "dir":
                continue
            clow = os.path.normpath(c["path"]).lower()
            if clow in gdirs:
                plat = gdirs[clow]
                c["game_platform"] = plat
                c["category"] = "游戏"
                c["cleanable"] = False
                c["risk"] = "高"
                if not c.get("what"):
                    c["what"] = ("Epic Games 安装的游戏本体。" if plat == "epic"
                                 else "GOG 安装的游戏本体。")
            elif clow in glibs:
                c["game_library"] = True
                c["category"] = "游戏"
    return {
        "drive": letter, "path": p, "children": children,
        "engine": engine,
        "parent_size": (cached["table"]["dirs"].get(os.path.normpath(p).lower(), 0)
                        if cached and cached.get("table") else 0),
        "listed_total": sum(c["size"] for c in children),
        "listed_total_h": human(sum(c["size"] for c in children)),
    }

def _as_list(v) -> list:
    """把可能是单值/None/列表的 JSON 字段统一成列表。"""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# 指向「当前用户自己的目录」的路径标记。这类路径与盘符无关：
# %LOCALAPPDATA% 永远展开成 Windows 所在盘的用户目录，无论你选了哪个盘。
# 所以它们只能在系统盘上出现 —— 否则会出现极荒谬的结果：
# 选 D 盘时列出「D 盘的 pip 缓存」，而路径实际是 C:\Users\…\pip\Cache。
# 这正是用户反馈「D/E 盘老是冒出包管理器/浏览器/临时文件」的根因：
# 这些目标的 system_only 没标，于是每个盘都挂了一遍 C 盘的内容。
USER_PROFILE_TOKENS = ("%LOCALAPPDATA%", "%APPDATA%", "%USERPROFILE%", "%TEMP%")


def is_user_local(t: dict) -> bool:
    """目标是否指向「当前用户目录」（因而只能在系统盘上成立）。"""
    if t.get("local_only"):
        return True
    path = str(t.get("path") or "").upper()
    return any(tok in path for tok in USER_PROFILE_TOKENS)


def is_system_drive(letter: str) -> bool:
    """判断是否为装有 Windows 的系统盘。

    用于过滤掉其它盘上不存在的系统目标（例如不该在 D 盘列出 D:\\Windows\\Temp）。
    """
    if not IS_WINDOWS:
        return letter.upper() == "C"
    root = letter.rstrip(":").upper() + ":\\"
    return (os.path.isdir(os.path.join(root, "Windows", "System32"))
            or (os.path.isdir(os.path.join(root, "Windows"))
                and os.path.isdir(os.path.join(root, "Users"))))



# ======================================================================
#  配置加载
# ======================================================================

_cache: dict = {}
_lock = threading.Lock()
# WizTree 探测结果（None = 还没探测过）
_WIZ_CACHE: dict | None = None
# 最近一次 WizTree 导出的目录体积表：盘符 → {"dirs": {path:size}, "at": 时间戳}
# 有了它，「点开大项看二级目录」就只是内存查表，不必再扫盘。
_WIZ_DIRS: dict[str, dict] = {}
_WIZ_TTL = 600.0                    # 10 分钟内的表直接复用


def _load_json(path: Path, default, use_cache: bool = True):
    """读 JSON。utf-8-sig 容错记事本保存的 BOM；带 mtime 缓存。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default
    if use_cache and _cache.get(key, (None, None))[1] == mtime:
        return _cache[key][0]
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return default
    if use_cache:
        _cache[key] = (data, mtime)
    return data


def load_targets() -> dict:
    return _load_json(TARGETS_PATH, {"presets": [], "targets": []})


def load_drives_cfg() -> dict:
    return _load_json(DRIVES_PATH, {"app": {}, "profiles": {}})


def load_convert() -> dict:
    return _load_json(CONVERT_PATH, {"display": {}, "units": []})


def load_filetypes() -> dict:
    return _load_json(FILETYPES_PATH, {"folder_rules": [], "file_rules": []})


def load_game_names() -> dict:
    """游戏中文名映射（appid → 中文名）。缺失时返回空表，不影响任何功能。"""
    return _load_json(GAMENAMES_PATH, {"games": {}})


def game_display_name(appid, name: str, installdir: str = "") -> tuple[str, str]:
    """返回 (显示名, 中文名)。

    为什么要单独做这个：Steam 的 appmanifest 里只有 Steam 自己语言（多为英文）
    的游戏名，本地拿不到中文名，中文用户对着一堆英文目录名很难认。
    这张映射表只影响显示 —— 不参与识别、不参与清理判断、不碰文件。

    匹配顺序：appid 精确命中 → 英文名/安装目录模糊命中 → 原名。
    显示成「中文名（原名）」既好认又不丢失 Steam 上的原名，便于用户核对。
    """
    cfg = load_game_names().get("games") or {}
    entry = None
    key = str(appid or "")
    if key and key in cfg:
        entry = cfg[key]
    if entry is None and cfg:
        low = (name or "").lower()
        low_dir = (installdir or "").lower()
        for _aid, e in cfg.items():
            for m in (e.get("match") or []):
                m = str(m).lower()
                if not m:
                    continue
                if m == low or m == low_dir or (len(m) >= 5 and m in low):
                    entry = e
                    break
            if entry:
                break
    zh = str((entry or {}).get("zh") or "").strip()
    if not zh or zh == name:
        return name, ""
    # 原名里已经有中文就别再加前缀了 —— 否则会出现
    # 「纸房子（纸房子 Sweet Home）」这种废话式的双重显示。
    if re.search(r"[\u4e00-\u9fff]", name or ""):
        return name, zh
    return f"{zh}（{name}）", zh


def load_custom_rules() -> list:
    """自定义规则 = 用户勾选的一组固定清理项，可命名保存、一键套用或立即清理。

    结构：{"id","name","ids","drive","auto","created"}。
    旧版本存的是 {"path","kind","glob"} 形式的自由路径规则，语义完全不同，
    这里直接跳过，避免两种含义混在一起。
    """
    data = _load_json(CUSTOM_PATH, {"rules": []}, use_cache=False)
    rules = data.get("rules", []) if isinstance(data, dict) else (data or [])
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        if "ids" not in r or "path" in r:
            continue
        out.append({
            "id": r.get("id") or f"rule-{abs(hash(str(r.get('name', '')))) % 10**8}",
            "name": (r.get("name") or "未命名规则").strip(),
            "ids": [str(i) for i in (r.get("ids") or [])],
            "drive": (r.get("drive") or "C").rstrip(":").upper(),
            "auto": bool(r.get("auto")),
            "created": r.get("created") or "",
        })
    return out


def save_custom_rules(rules: list) -> None:
    _cache.pop(str(CUSTOM_PATH), None)
    _atomic_write(CUSTOM_PATH, {"rules": rules})


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_history() -> list:
    data = _load_json(HISTORY_PATH, [], use_cache=False)
    return data if isinstance(data, list) else []


def append_history(entry: dict) -> None:
    with _lock:
        hist = load_history()
        hist.insert(0, entry)
        _atomic_write(HISTORY_PATH, hist[:200])


# ======================================================================
#  识别引擎
# ======================================================================

def _match_rule(rule: dict, name: str, ext: str, is_dir: bool) -> int:
    """给单条规则打分。0 表示不匹配；分越高越可信。

    短英文关键词（<=4 字符）必须整词匹配，否则会误伤：
    例如 "tim" 会命中 "opTIMization"，曾因此把 DeliveryOptimization
    误判成微信数据。
    """
    keys = rule.get("match") or []
    how = rule.get("kind", "substring")
    low = name.lower()
    if how == "exact":
        return 100 if low in [str(k).lower() for k in keys] else 0
    if how == "extension":
        return 80 if ext and ext in [str(k).lower() for k in keys] else 0

    best = 0
    for k in keys:
        k = str(k).lower()
        if not k:
            continue
        if low == k:
            best = max(best, 95)
            continue
        if k not in low:
            continue
        if all(ord(c) < 128 for c in k) and len(k) <= 4:
            i = low.find(k)
            ok = False
            while i != -1:
                before = low[i - 1] if i > 0 else " "
                after = low[i + len(k)] if i + len(k) < len(low) else " "
                if not before.isalnum() and not after.isalnum():
                    ok = True
                    break
                i = low.find(k, i + 1)
            if not ok:
                continue
        best = max(best, (60 if is_dir else 45) + min(len(k), 12))
    return best


# 未被任何规则识别时，这些扩展名允许用「所在目录」推断身份；
# 其余扩展名已有明确含义（.mp4/.pdf/.exe），路径不应压过它
GENERIC_EXTS = {"", ".bin", ".dat", ".db", ".tmp", ".cache", ".data", ".idx",
                ".blob", ".log", ".json", ".xml", ".txt"}

# 游戏库的标志性路径段。位于这些路径下的 .exe 是游戏主程序而非安装包，
# 绝不能被「安装包 → 可清理」命中 —— 删掉 RDR2.exe 游戏就打不开了。
GAME_INSTALL_MARKERS = ("\\steamapps\\", "\\common\\", "\\steamlibrary\\",
                        "\\epic games\\", "\\gog galaxy\\",
                        "\\ubisoft game launcher\\")


def _in_game_install(path: str) -> bool:
    low = "\\" + (path or "").lower()
    return any(m in low for m in GAME_INSTALL_MARKERS)


def _looks_like_browser_profile(path: str) -> bool:
    """判断目录是否真的是浏览器资料目录。

    为什么需要：名字里带 chrome/edge 的目录未必是浏览器数据。
    实测有台机器上一个与浏览器同名的目录里装的是论文和 ComfyUI 压缩包 —— 是个误命名目录。
    若不加判断，会告诉用户「这里面有书签和密码」，纯属误导。
    真实资料目录一定有 User Data / Default / Profile 这类结构。
    """
    if not os.path.isdir(path):
        return False
    markers = {"user data", "default", "profile", "profiles", "cache",
               "code cache", "gpucache", "local storage", "indexeddb"}
    try:
        names = {e.name.lower() for e in os.scandir(path)}
    except OSError:
        return False
    if names & markers:
        return True
    # 也有可能是「上层」目录（如 ...\Google\Chrome），往下看一层
    for sub in list(names)[:12]:
        try:
            sp = os.path.join(path, sub)
            if os.path.isdir(sp):
                if {e.name.lower() for e in os.scandir(sp)} & markers:
                    return True
        except OSError:
            continue
    return False


def identify(name: str, path: str, kind: str) -> dict:
    """按知识库识别一个文件/目录，返回分类、风险、说明与清理建议。

    两路证据：名称/扩展名，与「所在路径」，分开算，互不污染。
      * 目录没有扩展名线索，一律优先用名称结果；
      * 文件只在扩展名说明不了问题（.bin/.dat/无扩展名）时才用路径兜底。
    这条规则的由来：曾把 …\\某聊天软件目录\\...\\x.7z 判成「下载目录」、
    把 .rar 判成「软件安装目录」，都是路径把扩展名压过了。
    """
    kb = load_filetypes()
    is_dir = (kind == "dir")
    ext = os.path.splitext(name)[1].lower()
    folder_rules = list(kb.get("folder_rules", []))
    file_rules = list(kb.get("file_rules", []))
    # 文件也要考虑目录规则 —— 「文件所在的目录」往往是唯一线索
    # （例如 .ollama\\models\\blobs\\sha256-xxx 只有靠目录规则才能解释）
    rules = folder_rules if is_dir else (folder_rules + file_rules)

    ancestors = []
    _d = os.path.dirname(path.rstrip("\\/")) if path else ""
    for _ in range(4):
        if not _d or len(_d) <= 3:
            break
        _b = os.path.basename(_d)
        if _b:
            ancestors.append(_b)
        _d = os.path.dirname(_d)
    dir_part = "\\".join(ancestors).lower()

    in_game = _in_game_install(path)
    # 浏览器规则的上下文校验（只算一次，避免反复扫目录）
    browser_ok: bool | None = None
    is_browser_rule = {"browser-data", "browser-profile"}
    name_best, name_score = None, 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if in_game and rule.get("id") == "installer-pkg":
            continue
        if rule.get("id") in is_browser_rule and is_dir:
            if browser_ok is None:
                browser_ok = _looks_like_browser_profile(path)
            if not browser_ok:
                continue          # 名字带 chrome 但不是浏览器资料，不套用该规则
        s = _match_rule(rule, name, ext, is_dir)
        if s > name_score:
            name_best, name_score = rule, s

    path_best, path_score = None, 0
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("kind", "substring") != "substring":
            continue
        for k in (rule.get("match") or []):
            k = str(k).lower()
            if len(k) >= 4 and k in dir_part:
                s = 55 + min(len(k), 14)
                if s > path_score:
                    path_best, path_score = rule, s

    if is_dir:
        best, best_score = name_best, name_score
        if best_score == 0 and path_best is not None:
            best, best_score = path_best, path_score
    elif ext in GENERIC_EXTS and path_best is not None and path_score > name_score:
        best, best_score = path_best, path_score
    else:
        best, best_score = name_best, name_score

    if best and best_score > 0:
        return {
            "recognized": True, "rule_id": best.get("id"),
            "title": best.get("title") or name,
            "category": best.get("category", "其它"),
            "risk": best.get("risk", "中"),
            "cleanable": bool(best.get("cleanable")),
            "confidence": best.get("confidence", "low"),
            "confidence_label": (kb.get("confidence_labels", {}) or {})
                                .get(best.get("confidence", "low"), ""),
            "what": best.get("what", ""), "advice": best.get("advice", ""),
            "tip": best.get("tip", ""), "match_score": best_score,
        }

    if is_dir:
        return {
            "recognized": False, "rule_id": None, "title": name,
            "category": "未识别", "risk": "中", "cleanable": False,
            "confidence": "low",
            "confidence_label": "低（知识库里没有这个名称）",
            "what": f"知识库里没有匹配到「{name}」这个名字，无法确定它的用途。"
                    f"它占用了可观空间，但本工具不会猜测内容。",
            "advice": "建议你手动打开这个目录看一眼内容再决定。本工具不会把它列入清理建议。",
            "tip": "如果你知道它是什么，可以在 config\\filetypes.json 里加一条规则，"
                   "以后就能自动识别了。",
            "match_score": 0,
        }
    return {
        "recognized": False, "rule_id": None, "title": name,
        "category": "未识别", "risk": "中", "cleanable": False,
        "confidence": "low",
        "confidence_label": "低（仅按扩展名无法判断）",
        "what": f"「{ext or '无扩展名'}」类型的文件，知识库里没有对应说明。",
        "advice": "请自行确认它的用途。本工具不会建议删除无法识别的文件。",
        "tip": "只有当你确定它没用时再删。",
        "match_score": 0,
    }


# ======================================================================
#  浏览器数据目录分区
# ======================================================================

# 浏览器数据目录同时装着两类东西：
#   可安全清理的缓存（Cache / Code Cache / GPUCache / ShaderCache …）
#   绝不能删的：书签、密码、Cookie、登录状态、扩展、历史记录
# 因此**整目录绝不可标为可清理**，只能把缓存子目录单独列出来。
# 这条规则是被实测逼出来的：一个与浏览器同名的目录有上百 GB，
# 早期把整目录标成「可清理」，勾选就会删掉整个浏览器资料。
BROWSER_CACHE_DIRS = {
    "cache", "code cache", "gpucache", "shadercache", "grshadercache",
    "dawngcache", "dawngraphitecache", "component crx cache",
    "media cache", "application cache", "service worker",
    "cachestorage", "scriptcache", "jscache", "blob_storage",
}
# 注意：**不要**再加一个「命中这些名字就是浏览器资料目录」的常量来辅助判断。
# 只看名字会把一个与浏览器同名、实则装着论文 + ComfyUI 归档的目录误判成浏览器资料，
# 所以判定统一走 _looks_like_browser_profile()：必须在目录内部
# 真的看到 Cache / Code Cache 这类缓存子目录才认。

PS_DIR_KIND = r"""
$ErrorActionPreference='SilentlyContinue'
$p = '__PATH__'
$denied = $false
$dirs = @()
$files = @()
$ErrorActionPreference='Stop'
try { $all = @(Get-ChildItem -LiteralPath $p -Force -ErrorAction Stop) }
catch { $denied = $true; $all = @() }
$ErrorActionPreference='SilentlyContinue'
foreach ($c in $all) {
    if ($c.PSIsContainer) {
        if ($c.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
        $dirs += [pscustomobject]@{ name=$c.Name; path=$c.FullName
            modified=$c.LastWriteTime.ToString('yyyy-MM-dd') }
    } else {
        $files += [pscustomobject]@{ name=$c.Name; path=$c.FullName
            size=[int64]$c.Length; modified=$c.LastWriteTime.ToString('yyyy-MM-dd') }
    }
}
[pscustomobject]@{ denied=$denied; dirs=$dirs; files=$files } |
    ConvertTo-Json -Compress -Depth 4
"""


def _looks_like_game_install(path: str) -> bool:
    """判断目录是否是一个游戏本体（而非缓存）。

    为什么要在分区里单独认它：全盘扫描实测最大的实体就是游戏
    （单款 3A 大作常达上百 GB），而它们**不是垃圾**。
    只把它们归进「建议保留」等于什么都没说，用户还会以为工具漏扫了。
    认出来之后就能给一句有用的话：「想回收请用启动器卸载」。

    判定顺序（先便宜后昂贵）：
      1. 路径里出现 steamapps/common/steamlibrary 等标志段 —— 最可靠也最快；
      2. 名字命中知识库的「游戏」类规则；
      3. 直接子目录里有 .exe —— 兜底，但要真扫目录，所以放最后。
    """
    if not path:
        return False
    if _in_game_install(path):
        return True
    try:
        info = identify(os.path.basename(path.rstrip("\\/")) or "", path, "dir")
        if info.get("recognized") and (info.get("category") or "") in ("游戏", "模拟器"):
            return True
    except Exception:
        pass
    # 兜底：游戏目录根下一定有可执行文件（RDR2.exe / witcher3.exe …）
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".exe"):
                    return True
    except OSError:
        pass
    return False


def analyze_dir_partition(path: str) -> dict:
    """把目录分成「缓存子目录（可清理）/ 其它内容（不要删）」两部分。

    为什么不能只给一句笼统的「建议保留」：一个与浏览器同名的目录可能有上百 GB，
    整体标成可清理会删掉书签密码，但只标「保留」用户也不知道这上百 GB 里
    哪一块动了没事。所以这里按子目录逐个分类并给出理由。

    解释文案必须跟着目录**真实身份**走。踩过的坑：早先把所有保留下来的
    文件都写成「浏览器配置文件，不要删」，结果某款游戏的 levels_3.rpf 也顶着
    这句话 —— 纯属误导。现在按 浏览器资料 / 游戏安装 / 未识别 三种语境分别措辞。
    """
    if not os.path.isdir(path):
        return {"error": "路径不存在", "cleanable": [], "keep": [],
                "kind": "unknown", "title": "路径不存在", "note": ""}
    r = run_ps(PS_DIR_KIND.replace("__PATH__", path.replace("'", "''")), timeout=600)
    if "error" in r:
        return {"error": r["error"], "cleanable": [], "keep": [],
                "kind": "unknown", "title": "", "note": ""}

    # 先定语境，再决定每一行怎么写理由
    is_browser = _looks_like_browser_profile(path)
    is_game = False if is_browser else _looks_like_game_install(path)
    if is_browser:
        kind, title = "browser", "浏览器资料目录"
        note = ("这里同时装着可清缓存和绝不能删的用户数据（书签、密码、"
                "Cookie、登录状态、扩展、历史记录），所以整个目录不能一键清理。")
    elif is_game:
        kind, title = "game", "游戏安装目录"
        note = ("这是游戏本体，体积大但**不是垃圾**。想回收只有一条正路："
                "用 Steam / 启动器卸载这款游戏。工具不会替你删这里的任何文件。")
    else:
        kind, title = "unknown", "未能确定身份的目录"
        note = ("知识库里没有这个目录的规则，工具不对它做任何清理判断。"
                "下面只是把它拆开让你看清体积分布，任何一项都不要直接删。")

    cleanable: list[dict] = []
    keep: list[dict] = []
    for d in _as_list(r.get("dirs")):
        try:
            name = str(d.get("name") or "")
            dp = str(d.get("path") or "")
            entry = {"name": name, "path": dp, "type": "dir",
                     "modified": str(d.get("modified") or ""), "size": 0}
            if name.strip().lower() in BROWSER_CACHE_DIRS:
                entry["why"] = "浏览器/程序缓存目录，删掉会自动重建"
                entry["role"] = "cache"
                cleanable.append(entry)
            else:
                entry["role"] = "keep"
                if is_browser:
                    entry["why"] = "用户数据（书签/密码/登录状态等），不要删"
                elif is_game:
                    entry["why"] = "游戏资源，请用启动器卸载而不是手删"
                else:
                    entry["why"] = "请自行确认内容后再决定，工具不判断"
                keep.append(entry)
        except Exception:
            continue
    for f in _as_list(r.get("files")):
        try:
            keep.append({"name": str(f.get("name") or ""),
                         "path": str(f.get("path") or ""), "type": "file",
                         "size": int(f.get("size") or 0),
                         "modified": str(f.get("modified") or ""),
                         "role": "keep",
                         "why": ("游戏主程序/资源文件，删除会导致游戏无法运行"
                                 if is_game else
                                 "浏览器用户数据文件，不要删" if is_browser else
                                 "请自行确认用途，工具不判断")})
        except Exception:
            continue

    # 体积必须把「目录」也量进去 —— 只算直接文件会严重低报
    # （实测这种目录有上百 GB，但直接文件只有几 GB，其余都在子目录里）
    need_size = [c["path"] for c in cleanable] + [k["path"] for k in keep
                                                  if k["type"] == "dir"]
    if need_size:
        lst = BASE_DIR / ".oc-partition.tmp"
        try:
            lst.write_text("\n".join(need_size[:80]), encoding="utf-8")
            rs = run_ps(PS_STEAM_ORPHAN_SIZE.replace(
                "__LIST__", str(lst).replace("'", "''")), timeout=1800)
            if "error" not in rs:
                sizes = {os.path.normpath(str(x.get("path", ""))).lower():
                         int(x.get("size") or 0)
                         for x in _as_list(rs) if isinstance(x, dict)}
                for x in cleanable + keep:
                    if x["type"] == "dir":
                        x["size"] = sizes.get(os.path.normpath(x["path"]).lower(), 0)
        except OSError:
            pass
        finally:
            try:
                lst.unlink()
            except OSError:
                pass
    cleanable.sort(key=lambda x: -x["size"])
    keep.sort(key=lambda x: -x["size"])
    for x in cleanable + keep:
        x["size_h"] = human(x["size"])
    return {
        "path": path,
        "kind": kind, "title": title, "note": note,
        "denied": bool(r.get("denied")),
        "cleanable": cleanable, "keep": keep,
        "cleanable_size": sum(c["size"] for c in cleanable),
        "cleanable_size_h": human(sum(c["size"] for c in cleanable)),
        "keep_size": sum(c["size"] for c in keep),
        "keep_size_h": human(sum(c["size"] for c in keep)),
    }


# ======================================================================
#  Steam 游戏库（读 Steam 自己的清单，秒级且精确）
# ======================================================================

# 为什么直接读 Steam 的清单而不是扫磁盘：
#   * 快几个数量级 —— 只读几个几十 KB 的文本，不遍历目录
#   * 准 —— 游戏名、安装目录、体积都是 Steam 自己记录的，不用猜
#   * 能回答「Steam 认为装了什么」与「磁盘上实际有什么」的差异：
#     残留的游戏文件夹（已卸载但目录还在）是常见的空间大户

# StateFlags 位含义（Steam 官方文档）
STEAM_STATE_FLAGS = {
    1: "未安装", 2: "更新中", 4: "已安装", 8: "更新待处理", 16: "已启用",
    32: "已预载", 64: "预载未解锁", 128: "已锁定", 256: "已计划更新",
    512: "需要密钥", 1024: "需要许可证", 2048: "正在校验",
}

PS_STEAM = r"""
$ErrorActionPreference='SilentlyContinue'
$roots = @()
foreach ($k in @('HKCU:\Software\Valve\Steam', 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam')) {
    $p = (Get-ItemProperty -Path $k -ErrorAction SilentlyContinue).SteamPath
    if ($p) { $roots += $p }
}
foreach ($p in @('C:\Steam','D:\Steam','D:\steam','E:\Steam','E:\steam',
                 (Join-Path ${env:ProgramFiles(x86)} 'Steam'),
                 (Join-Path $env:ProgramFiles 'Steam'))) {
    if ($p) { $roots += $p }
}

$acfs = @()
foreach ($root in ($roots | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $libPaths = @($root)
    $vdf = Join-Path $root 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $vdf) {
        $txt = Get-Content -LiteralPath $vdf -Raw -ErrorAction SilentlyContinue
        foreach ($m in [regex]::Matches($txt, '"path"\s+"([^"]+)"')) {
            $libPaths += ($m.Groups[1].Value -replace '\\\\', '\')
        }
    }
    foreach ($lp in ($libPaths | Select-Object -Unique)) {
        $sa = Join-Path $lp 'steamapps'
        if (-not (Test-Path -LiteralPath $sa)) { continue }
        foreach ($f in (Get-ChildItem -LiteralPath $sa -Filter 'appmanifest_*.acf' -File -ErrorAction SilentlyContinue)) {
            $acfs += [pscustomobject]@{ library=$lp; path=$f.FullName }
        }
    }
}
[pscustomobject]@{
    roots = @($roots | Select-Object -Unique)
    manifests = @($acfs | ForEach-Object { $_.library + '|' + $_.path })
} | ConvertTo-Json -Compress -Depth 4
"""

# 残留目录体积：单独一次（路径数量少，代价可控）
PS_STEAM_ORPHAN_SIZE = r"""
$ErrorActionPreference='SilentlyContinue'
$paths = @()
if (Test-Path -LiteralPath '__LIST__') {
    $paths = @(Get-Content -LiteralPath '__LIST__' -Encoding UTF8 | Where-Object { $_ })
}
$out = @()
foreach ($p in $paths) {
    if (-not (Test-Path -LiteralPath $p)) { continue }
    $m = Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    $out += ('{{"path":"{0}","size":{1}}}' -f ($p -replace '\\', '/'), [int64]$m.Sum)
}
# 手工拼 JSON 数组，不用 ConvertTo-Json：
# 它在数组**只有一个元素**时会退化成序列化对象自身属性
# （Name/FullName/Length/Mode…），实测因此拿到一堆 path 为 null 的条目、体积全变 0。
# 路径统一转成正斜杠，彻底避开反斜杠转义问题。
'[' + ($out -join ',') + ']'
"""


def _parse_vdf(text: str) -> dict:
    """极简 Valve VDF（KeyValues）解析器。

    够用范围：嵌套对象、带引号的键值、转义反斜杠（"D:\\\\steam" 即 D:\\steam）。
    不支持注释等边角语法 —— 目标文件由 Steam 机器生成。
    """
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\':
                    buf.append(text[i + 1])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            i += 1
            tokens.append("".join(buf))
            continue
        if c in "{}":
            tokens.append(c)
            i += 1
            continue
        j = i
        while j < n and text[j] not in ' \t\r\n"{}':
            j += 1
        tokens.append(text[i:j])
        i = j

    pos = 0

    def parse_block() -> dict:
        nonlocal pos
        out: dict = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                break
            if pos + 1 >= len(tokens):
                break
            key = tok
            nxt = tokens[pos + 1]
            if nxt == "{":
                pos += 2
                out[key] = parse_block()
            else:
                out[key] = nxt
                pos += 2
        return out

    # 最外层形如  "AppState" { ... }  /  "libraryfolders" { ... }
    # 必须把外层键名重新包回去，否则它的子键会变成顶层键 ——
    # 这个 bug 曾导致 Steam 清单解析出 0 个游戏（而库路径看着正常，很难发现）。
    if len(tokens) > 1 and tokens[1] == "{":
        root_key = tokens[0]
        pos = 2
        return {root_key: parse_block()}
    if tokens and tokens[0] == "{":
        pos = 1
    return parse_block()


def _ts(v) -> str:
    try:
        n = int(v or 0)
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d") if n > 0 else ""
    except (ValueError, OSError, OverflowError):
        return ""


def discover_steam() -> dict:
    """发现 Steam 库与已安装游戏。数据全部来自 Steam 自己的清单文件。"""
    empty = {"found": False, "roots": [], "libraries": [], "games": [],
             "orphans": [], "game_count": 0, "total_size": 0,
             "total_size_h": "0 B", "orphan_count": 0}
    if not IS_WINDOWS:
        return empty
    r = run_ps(PS_STEAM, timeout=180)
    if "error" in r:
        return {**empty, "error": r["error"]}

    seen_roots: set[str] = set()
    roots: list[str] = []
    for x in _as_list(r.get("roots")):
        p = os.path.normpath(str(x))
        if p.lower() not in seen_roots:
            seen_roots.add(p.lower())
            roots.append(p)
    manifests = [str(x) for x in _as_list(r.get("manifests"))]
    if not manifests:
        return {**empty, "roots": roots}

    libraries: dict[str, dict] = {}
    games: list[dict] = []
    for entry in manifests:
        lib, _, acf_path = entry.partition("|")
        if not acf_path:
            continue
        # 库路径统一成 Windows 反斜杠形态。Steam 有的库来自 libraryfolders.vdf
        # （里面是 D:\\Steam），有的来自命令行探测（可能给 D:/Steam），
        # 混用会让后续拼出来的路径形如 d:/steam\\steamapps\\... ——
        # os.path 在 Windows 上把 '/' 当参数分隔符，这类路径在某些 API 上会静默失败。
        lib = os.path.normpath(lib.replace("/", "\\"))
        lib_key = os.path.normpath(lib).lower()
        libraries.setdefault(lib_key, {"path": lib, "apps": 0, "size": 0})
        try:
            with open(acf_path, "r", encoding="utf-8", errors="replace") as fh:
                state = _parse_vdf(fh.read()).get("AppState", {})
        except OSError:
            continue
        if not state:
            continue
        try:
            size = int(state.get("SizeOnDisk") or 0)
        except ValueError:
            size = 0
        try:
            flags = int(state.get("StateFlags") or 0)
        except ValueError:
            flags = 0
        installdir = state.get("installdir") or ""
        game_path = (os.path.join(lib, "steamapps", "common", installdir)
                     if installdir else "")
        info = identify(installdir or state.get("name", ""), game_path, "dir")
        info.pop("match_score", None)

        libraries[lib_key]["apps"] += 1
        libraries[lib_key]["size"] += size
        raw_name = state.get("name") or installdir or "未命名"
        disp_name, zh_name = game_display_name(
            state.get("appid"), raw_name, installdir)
        games.append({
            "appid": state.get("appid") or "",
            "name": raw_name,              # Steam 上的原名，保持不动
            "display_name": disp_name,     # 界面显示用（有中文名时是「中文（原名）」）
            "zh_name": zh_name,
            "installdir": installdir,
            "path": game_path,
            "exists": bool(game_path) and os.path.isdir(game_path),
            "size": size, "size_h": human(size),
            "library": lib,
            "installed": bool(flags & 4),
            "state": STEAM_STATE_FLAGS.get(flags, f"状态码 {flags}"),
            "last_updated": _ts(state.get("LastUpdated")),
            "last_played": _ts(state.get("LastPlayed")),
            "buildid": state.get("buildid") or "",
            "category": info.get("category", "游戏"),
            "title": info.get("title", "Steam 游戏"),
            "risk": "高", "cleanable": False,
            "what": info.get("what") or "Steam 安装的游戏本体。",
            "advice": "要卸载请在 Steam 客户端里操作（右键游戏 → 管理 → 卸载），"
                      "它会同时清理库记录，不留残留。",
            "tip": "Steam 的『已安装 → 按大小排序』可按体积找出最久没玩的大作。",
        })

    # 多份 libraryfolders.vdf 会列出同一个库，同一游戏因此被读多次
    # （实测同一款游戏会被读到 3 次）。先按安装路径去重，
    # 再**用去重后的结果反推库统计** —— 否则库的「应用数/体积」会翻倍虚高，
    # 与游戏列表对不上，用户会以为哪里出错了。
    dedup: dict[str, dict] = {}
    for g in games:
        key = os.path.normpath(g["path"]).lower() if g["path"] else f"appid:{g['appid']}"
        if key not in dedup or g["size"] > dedup[key]["size"]:
            dedup[key] = g
    games = sorted(dedup.values(), key=lambda g: -g["size"])

    lib_stats: dict[str, dict] = {}
    for g in games:
        k = os.path.normpath(g["library"]).lower()
        slot = lib_stats.setdefault(k, {"path": os.path.normpath(g["library"]),
                                        "apps": 0, "size": 0})
        slot["apps"] += 1
        slot["size"] += g["size"]

    # 按盘符聚合：用户明确要求看到「D 盘 / E 盘各占了多少」。
    # 这里同时给出该盘的容量与占用率，才能回答「Steam 吃掉了这个盘多少」。
    by_drive: dict[str, dict] = {}
    disk_cache: dict[str, dict] = {}
    for v in lib_stats.values():
        letter = os.path.splitdrive(v["path"])[0].rstrip(":").upper()
        if not letter:
            continue
        d = by_drive.setdefault(letter, {"drive": letter, "apps": 0, "size": 0,
                                         "libraries": []})
        d["apps"] += v["apps"]
        d["size"] += v["size"]
        d["libraries"].append({"path": v["path"], "apps": v["apps"],
                               "size": v["size"], "size_h": human(v["size"])})
    for letter, d in by_drive.items():
        info = disk_cache.setdefault(letter, get_disk_info(letter))
        d["size_h"] = human(d["size"])
        d["disk_total"] = info.get("total", 0)
        d["disk_total_h"] = info.get("total_h", "")
        d["disk_free_h"] = info.get("free_h", "")
        d["disk_percent"] = info.get("percent", 0)
        d["share"] = (round(d["size"] / info["total"] * 100, 1)
                      if info.get("total") else 0)
        d["libraries"].sort(key=lambda x: -x["size"])

    lib_list = sorted(
        ({**v, "size_h": human(v["size"]), "exists": os.path.isdir(v["path"])}
         for v in lib_stats.values()),
        key=lambda x: -x["size"])

    # 差异核对：库里存在但 Steam 清单里没有的游戏目录 ——
    # 卸载不干净留下的残留，是最值得回收的一类
    known = {os.path.normpath(g["path"]).lower() for g in games if g["path"]}
    orphans: list[dict] = []
    for lib in lib_list:
        common = os.path.join(lib["path"], "steamapps", "common")
        if not os.path.isdir(common):
            continue
        try:
            for e in os.scandir(common):
                if not e.is_dir(follow_symlinks=False):
                    continue
                if os.path.normpath(e.path).lower() in known:
                    continue
                orphans.append({"name": e.name, "path": e.path,
                                "library": lib["path"], "size": 0,
                                "size_h": "未测量"})
        except OSError:
            continue

    # 残留目录测体积：这些是「Steam 已无记录但目录还在」的空间，
    # 通常是最值得回收的一类，不测体积就没有意义
    if orphans:
        lst = BASE_DIR / ".steam-orphans.tmp"
        try:
            lst.write_text("\n".join(o["path"] for o in orphans[:40]),
                           encoding="utf-8")
            rs = run_ps(PS_STEAM_ORPHAN_SIZE.replace(
                "__LIST__", str(lst).replace("'", "''")), timeout=900)
            if "error" not in rs:
                sizes = {os.path.normpath(str(s.get("path", ""))).lower():
                         int(s.get("size") or 0)
                         for s in _as_list(rs) if isinstance(s, dict)}
                for o in orphans:
                    sz = sizes.get(os.path.normpath(o["path"]).lower(), 0)
                    o["size"] = sz
                    o["size_h"] = human(sz)
                orphans = [o for o in orphans if o["size"] > 0]
                orphans.sort(key=lambda o: -o["size"])
        except OSError:
            pass
        finally:
            try:
                lst.unlink()
            except OSError:
                pass

    total = sum(g["size"] for g in games)
    return {
        "found": True, "roots": roots,
        "libraries": lib_list, "games": games,
        "by_drive": sorted(by_drive.values(), key=lambda x: -x["size"]),
        "game_count": len(games),
        "total_size": total, "total_size_h": human(total),
        "orphans": orphans[:20], "orphan_count": len(orphans),
        "orphan_size": sum(o["size"] for o in orphans),
        "orphan_size_h": human(sum(o["size"] for o in orphans)),
    }


# ======================================================================
#  Epic / GOG 游戏发现（v2.2 新增）
# ======================================================================

def _epic_game_from_manifest(d: dict) -> dict | None:
    """把一份 Epic .item 清单（JSON dict）归一化成游戏记录；不是游戏的返回 None。

    实测同目录下还躺着引擎/插件/工具类清单（Unreal Engine、Quixel Bridge、
    Fab 插件），它们的共同点是 bIsApplication=False 或 CatalogNamespace="ue"，
    两条判据必须同时用：UE 本体 bIsExecutable=True 单靠可执行标记踢不掉。
    """
    if not isinstance(d, dict):
        return None
    if not d.get("bIsApplication"):
        return None
    if str(d.get("CatalogNamespace") or "").lower() == "ue":
        return None
    loc = str(d.get("InstallLocation") or "").strip()
    if len(loc) < 4 or loc[1] != ":":
        return None
    try:
        size = int(d.get("InstallSize") or 0)
    except (TypeError, ValueError):
        size = 0
    name = str(d.get("DisplayName") or d.get("AppName") or "未命名").strip()
    return {"appid": str(d.get("AppName") or ""), "name": name or "未命名",
            "path": os.path.normpath(loc), "size": size,
            "launch": str(d.get("LaunchExecutable") or "")}


def discover_epic() -> dict:
    """发现 Epic Games 已安装游戏。数据全部来自启动器自己的 .item JSON 清单，
    纯标准库读取几 KB 文件，微秒级，不需要注册表或外部进程。"""
    empty = {"found": False, "games": [], "game_count": 0,
             "total_size": 0, "total_size_h": "0 B"}
    if not IS_WINDOWS:
        return empty
    mdir = os.path.join(os.environ.get("PROGRAMDATA") or r"C:\ProgramData",
                        "Epic", "EpicGamesLauncher", "Data", "Manifests")
    try:
        names = os.listdir(mdir)
    except OSError:
        return empty

    games: list[dict] = []
    for fn in names:
        if not fn.lower().endswith(".item"):
            continue
        try:
            with open(os.path.join(mdir, fn), "r",
                      encoding="utf-8-sig", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        g = _epic_game_from_manifest(data)
        if not g:
            continue
        exists = os.path.isdir(g["path"])
        size = g["size"]
        if size <= 0 and exists:
            # 清单里没记体积（实测引擎类条目如此）才现场测量，游戏类一般都有
            size = _size_of_dir(g["path"])
        info = identify(g["name"], g["path"], "dir")
        info.pop("match_score", None)
        games.append({
            "appid": g["appid"], "platform": "epic",
            "name": g["name"], "display_name": g["name"], "zh_name": "",
            "installdir": os.path.basename(g["path"]),
            "path": g["path"], "exists": exists,
            "size": size, "size_h": human(size),
            "library": os.path.dirname(g["path"]),
            "installed": True,
            "category": "游戏", "title": "Epic 游戏",
            "risk": "高", "cleanable": False,
            "what": info.get("what") or "Epic Games 安装的游戏本体。",
            "advice": "要卸载请在 Epic Games 启动器里操作（库 → 右键游戏 → 卸载），"
                      "它会同步清掉清单记录，不留残留。",
        })

    # 同一安装位置被多份清单引用时按路径去重，保留体积大的那份
    dedup: dict[str, dict] = {}
    for g in games:
        k = g["path"].lower()
        if k not in dedup or g["size"] > dedup[k]["size"]:
            dedup[k] = g
    games = sorted(dedup.values(), key=lambda g: -g["size"])
    total = sum(g["size"] for g in games)
    return {"found": bool(games), "games": games, "game_count": len(games),
            "total_size": total, "total_size_h": human(total)}


def _gog_game_from_values(game_id: str, vals: dict) -> dict | None:
    """把一个 GOG 注册表项的值集合归一化成游戏记录；缺关键字段返回 None。

    拆成纯函数是为了能用合成数据做单元测试 —— 本机没装 GOG 时
    注册表路径走不到，解析逻辑照样被钉死。
    """
    path = str(vals.get("path") or "").strip()
    if len(path) < 4 or path[1] != ":":
        return None
    name = str(vals.get("gameName") or "").strip() or f"GOG 游戏 {game_id}"
    return {"appid": str(game_id), "name": name, "path": os.path.normpath(path)}


def discover_gog() -> dict:
    """发现 GOG Galaxy 已安装游戏。数据来自注册表
    HKLM\\SOFTWARE\\(WOW6432Node\\)GOG.com\\Games —— GOG 客户端自己维护的清单位置。
    注册表不记体积，逐个现场测量（GOG 库通常不大，代价可接受）。"""
    empty = {"found": False, "games": [], "game_count": 0,
             "total_size": 0, "total_size_h": "0 B"}
    if not IS_WINDOWS:
        return empty
    try:
        import winreg
    except ImportError:
        return empty

    games: list[dict] = []
    for root in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                try:
                    sub_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub_name) as sub:
                        vals: dict = {}
                        j = 0
                        while True:
                            try:
                                vn, vv, _ = winreg.EnumValue(sub, j)
                                vals[vn] = vv
                                j += 1
                            except OSError:
                                break
                except OSError:
                    continue
                g = _gog_game_from_values(sub_name, vals)
                if not g:
                    continue
                exists = os.path.isdir(g["path"])
                size = _size_of_dir(g["path"]) if exists else 0
                info = identify(g["name"], g["path"], "dir")
                info.pop("match_score", None)
                games.append({
                    "appid": g["appid"], "platform": "gog",
                    "name": g["name"], "display_name": g["name"], "zh_name": "",
                    "installdir": os.path.basename(g["path"]),
                    "path": g["path"], "exists": exists,
                    "size": size, "size_h": human(size),
                    "library": os.path.dirname(g["path"]),
                    "installed": True,
                    "category": "游戏", "title": "GOG 游戏",
                    "risk": "高", "cleanable": False,
                    "what": info.get("what") or "GOG 安装的游戏本体。",
                    "advice": "要卸载请在 GOG Galaxy 里操作（已安装 → 管理 → 卸载），"
                              "或用游戏目录里的 unins*.exe。",
                })
        finally:
            winreg.CloseKey(key)

    dedup: dict[str, dict] = {}
    for g in games:
        k = g["path"].lower()
        if k not in dedup or g["size"] > dedup[k]["size"]:
            dedup[k] = g
    games = sorted(dedup.values(), key=lambda g: -g["size"])
    total = sum(g["size"] for g in games)
    return {"found": bool(games), "games": games, "game_count": len(games),
            "total_size": total, "total_size_h": human(total)}


def discover_games() -> dict:
    """全平台游戏库汇总（Steam + Epic + GOG），供 /__oc__/games 与磁盘树标注使用。

    Steam 走自己的完整负载（含库/残留/分盘统计），Epic/GOG 以统一游戏记录并入，
    每条记录都带 platform 字段，前端按平台分组显示。"""
    steam = discover_steam()
    epic = discover_epic()
    gog = discover_gog()
    games: list[dict] = []
    for g in (steam.get("games") or []):
        games.append({**g, "platform": "steam"})
    games.extend(epic.get("games") or [])
    games.extend(gog.get("games") or [])
    games.sort(key=lambda g: -g["size"])
    total = sum(g["size"] for g in games)
    return {
        "found": bool(games), "games": games, "game_count": len(games),
        "total_size": total, "total_size_h": human(total),
        "platforms": {
            "steam": {"found": bool(steam.get("found")),
                      "game_count": steam.get("game_count", 0),
                      "total_size_h": steam.get("total_size_h", "0 B")},
            "epic": {"found": bool(epic.get("found")),
                     "game_count": epic.get("game_count", 0),
                     "total_size_h": epic.get("total_size_h", "0 B")},
            "gog": {"found": bool(gog.get("found")),
                    "game_count": gog.get("game_count", 0),
                    "total_size_h": gog.get("total_size_h", "0 B")},
        },
        "steam": steam,   # 完整 Steam 负载原样透传，面板继续用它渲染库/残留/分盘
    }


_GAME_DIRS_CACHE: dict = {"ts": 0.0, "dirs": {}, "libs": set()}


def game_install_dirs() -> tuple[dict, set]:
    """Epic/GOG 游戏安装目录索引：({小写路径: 平台}, {库目录小写路径})。

    磁盘占用树与逐层拆子项都要用它标注「这是游戏本体」。读清单只要几毫秒，
    但 GOG 要现场测体积，所以加 60 秒缓存避免每次展开子项都重扫注册表。
    Steam 不走这里：它有自己更精确的 steamapps\\common 双亲判定。"""
    now = time.time()
    if now - _GAME_DIRS_CACHE["ts"] < 60 and _GAME_DIRS_CACHE["dirs"]:
        return _GAME_DIRS_CACHE["dirs"], _GAME_DIRS_CACHE["libs"]
    dirs: dict[str, str] = {}
    libs: set[str] = set()
    for plat, disc in (("epic", discover_epic), ("gog", discover_gog)):
        try:
            for g in (disc().get("games") or []):
                p = str(g.get("path") or "")
                if not p:
                    continue
                k = os.path.normpath(p).lower()
                dirs[k] = plat
                lib = os.path.dirname(k)
                if lib and len(lib) > 3:   # 库目录是盘符根时不收，防全盘误标
                    libs.add(lib)
        except Exception:
            continue
    _GAME_DIRS_CACHE.update({"ts": now, "dirs": dirs, "libs": libs})
    return dirs, libs


# ======================================================================
#  安全校验与删除
# ======================================================================

def check_deletable(path: str, kind: str = "tree") -> tuple[bool, str]:
    """判断某个清理目标是否允许操作。返回 (是否允许, 拒绝原因)。

    注意：kind="files" 时 path 是**要扫描的目录**（不是要删的文件），
    真正的删除对象是它内部匹配 glob 的文件，所以同样按目录校验。
    """
    norm = os.path.normpath(os.path.abspath(path))
    low = norm.lower()
    # 空路径必须显式拒绝：os.path.abspath("") 会返回**当前工作目录**，
    # 于是空串会伪装成一个合法目录并通过后续检查（对抗性审查发现的漏点）。
    if not str(path).strip():
        return False, "拒绝：路径为空"
    if len(low) < 3 or low[1] != ":" or low[2] != "\\":
        return False, "拒绝：目标不是有效的盘符路径"
    drive_root = low[:3]

    if low == drive_root:
        return False, "拒绝：不允许清理盘符根目录本身"

    if os.path.isdir(norm):
        parts = [p for p in norm[len(drive_root):].split("\\") if p]
    elif os.path.isfile(norm):
        if norm.count("\\") <= drive_root.count("\\"):
            return False, "拒绝：不允许在盘符根目录直接删除文件"
        parts = [p for p in norm[len(drive_root):].split("\\") if p]
    else:
        return False, "拒绝：目标不存在或不可访问"

    if not parts:
        return False, "拒绝：不允许清理盘符根目录"

    if len(parts) == 1 and parts[0].lower() in PROTECTED_AT_DRIVE_ROOT:
        return False, f"拒绝：{parts[0]} 属于受保护的系统目录"

    fatal = {"system32", "syswow64", "winsxs", "installer", "servicing",
             "assembly", "system volume information", "boot"}
    for p in parts:
        if p.lower() in fatal:
            return False, f"拒绝：路径中包含受保护的 {p}"

    leaf = parts[-1].lower()
    if leaf in PROTECTED_DIR_NAMES and leaf not in ALLOWED_CONTAINER_NAMES:
        return False, f"拒绝：{parts[-1]} 属于受保护的目录"

    home = os.path.expanduser("~").lower()
    if low in (home, os.path.join(home, "appdata").lower()):
        return False, "拒绝：不允许清理用户主目录"

    # 不可读时不在这里判定拒绝，交由上层报「需要管理员权限」，
    # 这样 C:\\Windows\\Temp 能给出可读提示而不是硬性拒绝
    return True, ""


def rm(path: str) -> bool:
    """删除单个文件或目录，失败返回 False。"""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            return not os.path.exists(path)
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except (PermissionError, OSError):
        try:
            os.chmod(path, 0o700)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                return not os.path.exists(path)
            os.remove(path)
            return True
        except Exception:
            return False


def _size_of(path: str) -> int:
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            return os.path.getsize(path)
        if os.path.isdir(path):
            return _size_of_dir(path)
    except OSError:
        pass
    return 0


def _size_of_dir(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                fp = os.path.join(root, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def clean_target(t: dict, only_paths: list | None = None,
                 drive: str | None = None) -> dict:
    """删除一个目标的文件。only_paths 非空时只删这些子项。"""
    t0 = time.time()
    path = expand(t["path"], drive)
    kind = t.get("kind", "tree")
    letter = (drive or "").rstrip(":").upper()
    result = {
        "id": target_id_for(t, drive if is_drive_specific(t) else None),
        "target_id": t["id"],
        "label": expand(t.get("label", t["id"]), drive),
        "category": t.get("category", "其它"),
        "path": path, "drive": letter,
        "before": 0, "after": 0, "freed": 0,
        "deleted": 0, "failed": 0, "skipped": [], "error": None,
        "seconds": 0.0,
    }

    ok, why = check_deletable(path, kind)
    if not ok:
        result["error"] = why
        return result

    if kind == "recyclebin":
        before = measure_target(t, drive).get("size", 0)
        if only_paths:
            for p in only_paths:
                sz = _size_of(p)
                if rm(p):
                    result["deleted"] += 1
                    result["freed"] += sz
                    # 成对清理 $I 元数据，避免留下孤儿记录
                    meta = os.path.join(os.path.dirname(p),
                                        "$I" + os.path.basename(p)[2:])
                    if os.path.exists(meta):
                        rm(meta)
                else:
                    result["failed"] += 1
                    result["skipped"].append(os.path.basename(p))
            result["after"] = max(0, before - result["freed"])
        else:
            d = expand(str(t.get("drive_letter") or ""), drive).rstrip(":") or letter
            ps = ("Clear-RecycleBin -DriveLetter " + d +
                  " -Force -ErrorAction SilentlyContinue; '{\"ok\":1}'")
            r = run_ps(ps)
            if "error" in r:
                result["error"] = r["error"]
            after = measure_target(t, drive).get("size", 0)
            result["after"] = after
            result["freed"] = max(0, before - after)
            result["deleted"] = 1 if result["freed"] > 0 else 0
        result.update(before=before, seconds=round(time.time() - t0, 2))
        return result

    if not os.path.isdir(path):
        result["error"] = "路径不存在"
        return result

    # scandir 本身就可能抛 PermissionError（例如普通权限下的 C:\\Windows\\Temp）
    try:
        with os.scandir(path) as it:
            if kind == "files":
                glob = t.get("glob", "*")
                items = [e.path for e in it
                         if e.is_file(follow_symlinks=False)
                         and fnmatch.fnmatch(e.name, glob)]
            else:
                items = [e.path for e in it]
    except PermissionError:
        result["error"] = "拒绝访问：需要管理员权限（或该目录受系统保护）"
        result["seconds"] = round(time.time() - t0, 2)
        return result
    except OSError as exc:
        result["error"] = f"无法读取目录：{exc.strerror or exc}"
        result["seconds"] = round(time.time() - t0, 2)
        return result

    if only_paths:
        keep = {os.path.abspath(p).lower() for p in only_paths}
        items = [p for p in items if os.path.abspath(p).lower() in keep]

    result["before"] = sum(_size_of(p) for p in items)

    atomic = kind in ("tree", "files")
    for item in items:
        if atomic:
            sz = _size_of(item)
            if rm(item):
                result["deleted"] += 1
                result["freed"] += sz
            else:
                result["failed"] += 1
                result["freed"] += max(0, sz - _size_of(item))
                if len(result["skipped"]) < 20:
                    result["skipped"].append(os.path.basename(item))
        else:
            if rm(item):
                result["deleted"] += 1
            else:
                result["failed"] += 1
                if len(result["skipped"]) < 20:
                    result["skipped"].append(os.path.basename(item))

    if atomic:
        result["after"] = max(0, result["before"] - result["freed"])
    else:
        result["after"] = _size_of_dir(path)
        result["freed"] = max(0, result["before"] - result["after"])
    result["seconds"] = round(time.time() - t0, 2)
    return result




# 自动发现的深度上限 = 2（盘符根的直接子目录，及其子目录）。
# 这里刻意用下面两段展开的循环实现，而不是递归 + 深度常量：
# 无界递归会在 C:\ 这种盘上把每个目录都扫一遍，实测拖慢整轮扫描，
# 而且第三层往下的归属判断已经不可靠（分不清是哪个软件的内部缓存）。
AUTO_DISCOVER_DEPTH_PENALTY = 12
# 自动发现的候选上限。清单太长反而让人无从下手，且会拖慢扫描。
AUTO_DISCOVER_LIMIT = 12


def _auto_excluded(path: str, segments: list[str]) -> bool:
    """判断候选是否应被自动发现排除。"""
    low_segs = [s.lower() for s in segments]
    # 回收站：整棵树都不进（$Recycle.Bin 及其内部 SID 子目录）
    if any(s.startswith("$recycle.bin") or s.startswith("$recycle") for s in low_segs):
        return True
    for seg in low_segs:
        if seg in ("programdata", "appdata", "windows", "system volume information"):
            return True
        if seg.startswith("$windows."):
            return True
        # Program Files 下装的是**程序本体**，不是缓存。
        # 这里曾被放过，导致自动发现把 C:\Program Files (x86)\Adobe、
        # C:\Program Files\Microsoft OneDrive 这类程序目录标成「可清理」——
        # 名字命中知识库的「创作软件/云盘」规则，但删掉就是卸载软件。
        # 清理目标必须由预定义清单（精确到缓存子目录）给出，绝不由名字猜。
        if seg in ("program files", "program files (x86)"):
            return True
    name = os.path.basename(path.rstrip("\\/")).lower()
    # 回收站的元数据与 GUID/SID 形态的目录名，没有解释价值
    if name.startswith("$i") or name.startswith("$r"):
        return True
    if len(name) > 30 and name.count("-") >= 3:
        return True
    return False


def _looks_like_installed_program(path: str) -> bool:
    """判断目录里装的是**程序本体**（而不是缓存）。

    为什么必须有这道判断：自动发现按「目录名命中知识库」来判定可清理，
    但知识库的关键词是给「缓存目录」写的，撞上安装目录就出事。实测：
      * 某盘根下一个名字很短的目录（名字里带短词 "ps"）命中 photoshop 规则，
        （match 里有短词 "ps"），里面其实是 Adobe 的安装目录；
      * C:\\Program Files (x86)\\Adobe、C:\\Program Files\\Microsoft OneDrive
        同样被判成「可清理」，勾选等于卸载软件。

    判定只看通用特征，不看名字（看名字正是出问题的原因）：
      * 顶层存在 Uninstall*.exe / unins*.exe —— 安装器留下的卸载程序；
      * **两个以上「版本号形态」的子目录** —— 同时覆盖年份命名
        （Adobe Photoshop 2024 / …2023）与四段式版号
        （WPS Office\\12.1.0.x 与 12.1.0.y）。
        实测：装机目录命中 2 个，而下载目录与云盘临时目录都是 0 个
        —— 这是区分度最好的一条判据。

    刻意**不用**的两条判据（都是实测踩出来的）：
      * 「顶层 .exe 数量」——下载目录里躺着一堆安装包（实测某下载目录顶层就有
        5 个 setup.exe），按数量算会把下载目录误判成程序安装；
      * 「子目录里有卸载程序」——下载目录里每个装过又留下的软件目录都带卸载器，
        同样会误伤。
    卸载程序只在**顶层**出现才算数（如 WPS Office\\ksolaunch.exe 那种）。
    """
    if not os.path.isdir(path):
        return False
    try:
        entries = list(os.scandir(path))
    except OSError:
        return False
    ver_dirs = 0
    for e in entries:
        try:
            n = e.name.lower()
            if e.is_file():
                if n.startswith(("uninstall", "unins")):
                    return True
                continue
            if not e.is_dir():
                continue
            # 版本号形态：年份（2024）或四段式版号（12.1.0.x）
            if (re.search(r"(19|20)\d{2}", n)
                    or re.search(r"^\d+(\.\d+){2,}$", n)):
                ver_dirs += 1
        except OSError:
            continue
    return ver_dirs >= 2


def auto_discovered_targets(drive: str, limit: int = AUTO_DISCOVER_LIMIT) -> list[dict]:
    """在盘符顶层自动寻找「知识库识别为可清理」的目录。

    为什么需要它：预定义目标列表只覆盖常见系统位置（Windows\\Temp、回收站等），
    但磁盘占用的大头往往是**长尾的个人软件**（Telegram、夸克、Photoshop……）。
    没有自动发现时，D 盘只能列出 3 个可清理项，而实际有几十 GB 可回收。

    安全与体验约束：
      * 只接受知识库判定为 cleanable 且置信度非 low 的候选
      * 未识别的目录一律跳过（宁可少提示，也不猜）
      * 排除回收站内部、ProgramData、AppData、Windows 等无价值或已有专门处理的位置
      * 同名的父/子目录只保留最外层的那个，避免「steam」和「steam\\appcache」同时出现
      * 限制候选总数，清单太长反而让人无从下手
    """
    letter = (drive or "C").rstrip(":").upper()
    root = letter + ":\\"
    if not IS_WINDOWS or not os.path.isdir(root):
        return []
    if not load_filetypes().get("folder_rules"):
        return []

    out: list[dict] = []
    seen_paths: set[str] = set()
    seen_keys: set[str] = set()          # 用「识别归属 + 名称」去重

    def consider(path: str, depth: int, parent_hint: str) -> None:
        if len(out) >= limit * 3:        # 先多收，排序后再截断
            return
        low = os.path.normpath(path).lower()
        if low in seen_paths:
            return
        rel = os.path.relpath(path, root)
        segs = [s for s in rel.split("\\") if s]
        if _auto_excluded(path, segs):
            return
        name = os.path.basename(path.rstrip("\\/"))
        if not name:
            return
        info = identify(name, path, "dir")
        if not (info.get("recognized") and info.get("cleanable")) and parent_hint:
            # 名称无意义时退一步看父目录（例如 …\Telegram\tdata 里的 tdata）
            info = identify(parent_hint, path, "dir")
        if not (info.get("recognized") and info.get("cleanable")):
            return
        if info.get("confidence") == "low":
            return
        # 装的是程序本体就绝不推荐清理（见 _looks_like_installed_program 的注释）
        if _looks_like_installed_program(path):
            return
        # 去重键：同一类内容只提示一次（避免 Quark / quark-cloud-drive / QuarkCloudDrive 并列）
        key = f"{info.get('rule_id')}|{name.lower()}"
        if key in seen_keys:
            return
        ok, _why = check_deletable(path, "tree")
        if not ok:
            return
        seen_paths.add(low)
        seen_keys.add(key)
        out.append({
            "id": "auto-" + rel.replace("\\", "-").replace(" ", "_").lower(),
            "base_id": "auto",
            "label": f"{name}（自动发现）",
            "path": path,
            "category": info.get("category") or "缓存",
            "risk": info.get("risk") or "中",
            "needs_admin": False,
            "kind": "tree",
            "drive_letter": letter,
            "discovered": True,
            "auto_score": 100 - depth * AUTO_DISCOVER_DEPTH_PENALTY,
            "short": info.get("what") or "",
            "what": info.get("what") or "",
            "rebuild": info.get("advice") or "",
            "impact": info.get("advice") or "",
            "keep_if": info.get("tip") or "",
        })

    try:
        level1 = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []

    # 第一层：盘符根目录的直接子目录
    for e in level1:
        consider(e.path, 0, "")

    # 第二层：只对「自身没被识别为可清理」的一级目录再下一层。
    #
    # 这里必须判「父目录是否本身就是可清理候选」，不能只看它有没有被收进 out。
    # 反例（实测）：某下载目录本身命中下载目录规则，但它的 8 个子目录
    # （视频剪辑草稿等）被逐个又列了一遍 ——
    # 既冗余，又把**用户自己的创作草稿**推成了「可清理」。
    # 父目录已经代表这块空间了，就不该再拆开推荐它的内容。
    for e in level1:
        low = os.path.normpath(e.path).lower()
        if low in seen_paths:
            continue                      # 父目录已整体可清理，无需再看子目录
        rel = os.path.relpath(e.path, root)
        segs = [s for s in rel.split("\\") if s]
        if _auto_excluded(e.path, segs):
            continue
        pinfo = identify(e.name, e.path, "dir")
        # 顺序很重要：先判「装的是程序」，再判「本身是候选」。
        # 反过来写的话，一个同时命中两条的一级目录会掉进 else 分支被继续下钻 ——
        # 安装器仓库就是如此：它是安装器仓库（_looks_like_installed_program 为真），
        # 于是被继续下钻，把 8 个子目录挨个推成「可清理」。
        if _looks_like_installed_program(e.path):
            continue                      # 装的是程序本体，不下钻也不推荐其子项
        if (pinfo.get("recognized") and pinfo.get("cleanable")
                and pinfo.get("confidence") != "low"):
            continue                      # 父目录本身就是候选，不再拆它的子项
        try:
            sub = [s for s in os.scandir(e.path) if s.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for s in sub[:60]:
            consider(s.path, 1, e.name)

    # 浅层优先、同名合并后按深度与名称排序
    out.sort(key=lambda x: (-x["auto_score"], x["path"].lower()))
    return out[:limit]


def build_targets(drive: str | None, custom_path: str | None = None) -> list[dict]:
    """按所选盘符组装目标列表。"""
    cfg = load_targets()
    letter = (drive or "").rstrip(":").upper()
    sys_drive = is_system_drive(letter) if letter else True
    out: list[dict] = []
    seen: set[str] = set()

    def push(item: dict) -> None:
        key = "|".join([
            os.path.normpath(expand(item.get("path", ""), drive or None)).lower(),
            str(item.get("glob", "")), str(item.get("kind", "tree")),
        ])
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    for t in cfg.get("targets", []):
        if not isinstance(t, dict) or "id" not in t or "path" not in t:
            continue
        if is_drive_specific(t) and not letter:
            continue
        # 标了 system_only 的目标只在装有 Windows 的盘上出现，
        # 避免数据盘（如 D:）列出 D:\Windows\Temp 这类根本不存在的项
        if t.get("system_only") and not sys_drive:
            continue
        # 指向用户目录的目标（%LOCALAPPDATA% 等）同理：它们与盘符无关，
        # 永远落在系统盘上。不拦掉就会在 D/E 盘列出「D 盘的 pip 缓存」，
        # 而路径其实是 C:\Users\…（用户实测反馈过这个现象）。
        if is_user_local(t) and not sys_drive:
            continue
        item = dict(t)
        if letter and is_drive_specific(item):
            item["id"] = f"{t['id']}@{letter}"
        item["base_id"] = t["id"]
        push(item)

    # 用户保存的自定义规则不再作为清理目标出现 ——
    # 它们是「一组被勾选的固定项」，只在侧栏里以便捷方式套用，见 custom-rules 接口。

    if custom_path:
        cp = os.path.abspath(expand(custom_path, letter or None))
        for t in discover_junk_folders(cp):
            push(t)

    # 自动发现：把本盘上「被知识库识别为可清理」的应用目录也纳入清理清单。
    # 这是让数据盘真正有用的关键 —— 否则 D/E 盘只能列出 \Temp 和回收站，
    # 而 Telegram / 夸克 / Photoshop 这些几十 GB 的大户都不在清单里。
    if letter:
        for t in auto_discovered_targets(letter):
            push(t)

    return out


def discover_junk_folders(root: str) -> list[dict]:
    """在自定义目录下发现已知的垃圾文件夹，生成临时目标。"""
    out: list[dict] = []
    if not os.path.isdir(root):
        return out
    root = os.path.abspath(root)

    for sub in JUNK_SUBPATHS:
        p = os.path.join(root, sub)
        if not os.path.isdir(p):
            continue
        is_bin = sub == "$Recycle.Bin"
        out.append({
            **_CUSTOM_TEMPLATE,
            "id": "custom-" + sub.replace("\\", "-").replace("$", "").replace(".", "").lower(),
            "base_id": "custom",
            "label": f"{sub} —— 位于 {root}",
            "path": p, "risk": "低",
            "kind": "recyclebin" if is_bin else "tree",
            "drive_letter": os.path.splitdrive(p)[0].rstrip(":"),
            "short": f"自定义目录中发现的 {sub}。",
            "what": f"在 {root} 下发现了 {sub}，它属于常见的可清理位置。",
            "rebuild": "通常会自动重建。",
            "impact": "清理后对应程序会重新生成所需内容。",
            "keep_if": "不确定用途时先保留。",
        })

    try:
        entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        entries = []
    for e in entries:
        if e.name.lower() not in JUNK_FOLDER_NAMES:
            continue
        out.append({
            **_CUSTOM_TEMPLATE,
            "id": "custom-dir-" + e.name.lower().strip(".").replace(" ", "-"),
            "base_id": "custom",
            "label": f"{e.name} —— 位于 {root}",
            "path": e.path, "risk": "中",
            "kind": "tree",
            "drive_letter": os.path.splitdrive(e.path)[0].rstrip(":"),
            "short": f"自定义目录中名为 {e.name} 的文件夹，按名称判断可能是临时或缓存数据。",
            "what": f"按名称判断，{e.name} 通常是临时文件或缓存目录。O-Cleaner 无法确认它的具体用途，"
                    f"所以标为中风险并默认不勾选。",
            "rebuild": "不确定 —— 取决于哪个程序创建了它。",
            "impact": "如果有程序把工作数据放在这里，清理可能导致它出错。",
            "keep_if": "不确定这个目录是谁建的，就先保留。",
        })
    return out


# ======================================================================
#  单位换算
# ======================================================================

def resolve_units(units: list[dict]) -> list[dict]:
    """解析 bytes_per_unit，支持 '1/3 * movie4k' 这类引用其它单位的表达式。"""
    by_id = {u.get("id"): u for u in units if u.get("id")}
    resolved: dict[str, float] = {}

    def resolve(uid: str, chain: tuple = ()) -> float:
        if uid in resolved:
            return resolved[uid]
        u = by_id.get(uid)
        if not u or uid in chain:
            return 0.0
        val = u.get("bytes_per_unit", 0)
        out = 0.0
        if isinstance(val, (int, float)):
            out = float(val)
        elif isinstance(val, str):
            expr = val.strip()
            for other_id in by_id:
                if other_id != uid and other_id in expr:
                    expr = expr.replace(other_id, repr(resolve(other_id, chain + (uid,))))
            try:
                out = float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 本地配置表达式
            except Exception:
                out = 0.0
        resolved[uid] = out
        return out

    for uid in by_id:
        resolve(uid)
    return [{**u, "bytes": resolved.get(u.get("id"), 0.0)} for u in units]


def fmt_num(v: float) -> str:
    if v >= 100:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:,.1f}".rstrip("0").rstrip(".")
    if v >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3f}".rstrip("0").rstrip(".")


def convert_bytes(nbytes: float, units: list[dict], max_units: int = 6,
                  only: list[str] | None = None) -> list[dict]:
    out = []
    for u in units:
        if u.get("unlimited"):
            continue
        if only and u.get("id") not in only:
            continue
        b = u.get("bytes") or 0
        if b <= 0:
            continue
        cnt = nbytes / b
        if cnt < 0.01:
            continue
        out.append({
            "id": u.get("id"), "label": u.get("label") or u.get("id"),
            "plural": u.get("plural") or u.get("label") or u.get("id"),
            "count": round(cnt, 4), "count_h": fmt_num(cnt),
            "category": u.get("category", "其它"), "note": u.get("note", ""),
            "bytes_per_unit": b, "bytes_per_unit_h": human(b),
        })
    out.sort(key=lambda x: x["count"])
    return out[:max_units]


# ======================================================================
#  历史统计与换算
# ======================================================================

def history_stats(unit_ids: list[str] | None = None) -> dict:
    hist = load_history()
    total_freed = 0
    total_failed = 0
    run_count = 0
    by_category: dict[str, int] = {}
    by_drive: dict[str, int] = {}
    by_target: dict[str, dict] = {}
    timeline = []
    first_ts = last_ts = None

    for h in hist:
        runs = h.get("results") or []
        if not runs:
            continue
        run_count += 1
        total_freed += int(h.get("freed") or 0)
        for r in runs:
            f = int(r.get("freed") or 0)
            total_failed += int(r.get("failed") or 0)
            cat = r.get("category") or "其它"
            by_category[cat] = by_category.get(cat, 0) + f
            dv = (r.get("drive") or "").strip()
            if dv:
                by_drive[dv] = by_drive.get(dv, 0) + f
            tid = r.get("target_id") or r.get("id")
            if tid:
                slot = by_target.setdefault(tid, {"label": r.get("label", tid), "freed": 0})
                slot["freed"] += f
        timeline.append({"time": h.get("time", ""), "freed": int(h.get("freed") or 0),
                         "trigger": h.get("trigger", ""),
                         "targets": len(runs)})
        ts = h.get("time")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

    cfg = load_convert()
    units = resolve_units(cfg.get("units", []))
    disp = cfg.get("display", {}) or {}
    conversions = convert_bytes(total_freed, units,
                                int(disp.get("max_units", 6) or 6), unit_ids)

    top_targets = sorted(
        ({"id": k, "label": v["label"], "freed": v["freed"], "freed_h": human(v["freed"])}
         for k, v in by_target.items()),
        key=lambda x: -x["freed"])[:12]

    return {
        "run_count": run_count,
        "total_freed": total_freed, "total_freed_h": human(total_freed),
        "total_failed": total_failed,
        "first_time": first_ts, "last_time": last_ts,
        "by_category": [{"name": k, "freed": v, "freed_h": human(v)}
                        for k, v in sorted(by_category.items(), key=lambda x: -x[1])],
        "by_drive": [{"drive": k, "freed": v, "freed_h": human(v)}
                     for k, v in sorted(by_drive.items(), key=lambda x: -x[1])],
        "top_targets": top_targets,
        "timeline": timeline[:30],
        "conversions": conversions,
        "units": [{"id": u.get("id"), "label": u.get("label"),
                   "plural": u.get("plural") or u.get("label"),
                   "category": u.get("category", "其它"), "note": u.get("note", ""),
                   "bytes": u.get("bytes", 0), "bytes_h": human(u.get("bytes") or 0),
                   "unlimited": bool(u.get("unlimited")),
                   "raw": u.get("bytes_per_unit")}
                  for u in units],
    }


# ======================================================================
#  定时清理
# ======================================================================

DEFAULT_SCHEDULE = {"enabled": False, "interval_hours": 24, "preset": "safe",
                    "drive": "C", "last_run": 0, "next_run": 0}


def schedule_state() -> dict:
    s = dict(DEFAULT_SCHEDULE)
    s.update(_load_json(SCHEDULE_PATH, {}, use_cache=False) or {})
    return s


def _resolve_preset(targets: list[dict], preset: dict) -> list[str]:
    """把预设解析成目标 id 集合。盘符专属项用带盘符后缀的完整 id 匹配。"""
    sel = preset.get("select", {}) or {}
    base_to_full: dict[str, list[str]] = {}
    for t in targets:
        base_to_full.setdefault(t.get("base_id", t["id"]), []).append(t["id"])

    if "ids" in sel:
        # 显式按 id 指定：完全尊重配置，不做回收站过滤
        # （否则「只清回收站」这类预设会被自己的过滤器清空）
        out: list[str] = []
        for b in sel["ids"]:
            out.extend(base_to_full.get(b, []))
        return out

    risks = set(sel.get("risk", []))
    allow_admin = bool(sel.get("allow_admin", False))
    return [t["id"] for t in targets
            if t.get("risk") in risks and (allow_admin or not t.get("needs_admin"))]


def _apply_preset_to_targets(targets: list[dict], preset_id: str) -> list[dict]:
    cfg = load_targets()
    preset = next((p for p in cfg.get("presets", []) if p["id"] == preset_id), None)
    if not preset:
        return [t for t in targets if not t.get("needs_admin")]
    ids = set(_resolve_preset(targets, preset))
    return [t for t in targets if t["id"] in ids]


def _scheduler_loop() -> None:
    while True:
        time.sleep(30)
        try:
            s = schedule_state()
            if s.get("enabled"):
                nxt = float(s.get("next_run") or 0)
                if nxt and time.time() >= nxt:
                    _run_scheduled(s)
        except Exception:
            traceback.print_exc()


def _run_scheduled(s: dict) -> None:
    drive = (s.get("drive") or "C").rstrip(":").upper()
    targets = build_targets(drive)
    selected = _apply_preset_to_targets(targets, s.get("preset") or "safe")
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "trigger": f"定时自动清理（{drive}:）",
             "results": [], "freed": 0, "failed": 0, "targets": len(selected)}
    for t in selected:
        try:
            r = clean_target(t, drive=drive)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            r = _error_result(t, drive, exc)
        entry["results"].append(r)
        entry["freed"] += r["freed"]
        entry["failed"] += r["failed"]
    entry["freed_h"] = human(entry["freed"])
    append_history(entry)
    with _lock:
        s["last_run"] = time.time()
        s["next_run"] = time.time() + float(s.get("interval_hours", 24)) * 3600
        _atomic_write(SCHEDULE_PATH, s)


def _error_result(t: dict, drive: str | None, exc: Exception) -> dict:
    return {"id": target_id_for(t, drive), "target_id": t.get("base_id", t.get("id")),
            "label": t.get("label", t.get("id")), "category": t.get("category", "其它"),
            "path": expand(t.get("path", ""), drive),
            "drive": (drive or "").rstrip(":").upper(),
            "before": 0, "after": 0, "freed": 0, "deleted": 0, "failed": 0,
            "skipped": [], "seconds": 0.0,
            "error": f"内部错误：{type(exc).__name__}: {exc}"}


# ======================================================================
#  HTTP 服务
# ======================================================================

def build_state(drive: str | None = None, custom_path: str | None = None,
                scope: str = "all") -> dict:
    cfg = load_targets()
    dcfg = load_drives_cfg()
    letter = (drive or "C").rstrip(":").upper()
    targets = build_targets(letter, custom_path)
    if scope == "drive":
        targets = [t for t in targets if is_drive_specific(t)]
    return {
        "app": dcfg.get("app", {}),
        "presets": cfg.get("presets", []),
        "scope": scope,
        "targets": [{
            "id": t["id"], "base_id": t.get("base_id", t["id"]),
            "label": expand(t.get("label", t["id"]), letter),
            "path": expand(t["path"], letter), "category": t.get("category", "其它"),
            "risk": t.get("risk", "无"), "needs_admin": bool(t.get("needs_admin")),
            "kind": t.get("kind", "tree"), "glob": t.get("glob", ""),
            "drive_specific": is_drive_specific(t),
            "scope": "drive" if is_drive_specific(t) else "user",
            "temp_target": bool(t.get("temp_target")),
            "discovered": bool(t.get("discovered")),
            "short": t.get("short", ""), "what": t.get("what", ""),
            "rebuild": t.get("rebuild", ""), "impact": t.get("impact", ""),
            "keep_if": t.get("keep_if", ""),
        } for t in targets],
        "disk": get_disk_info(letter),
        "drives": list_drives(),
        "current_drive": letter,
        "custom_path": custom_path or "",
        "is_admin": is_admin(),
        "schedule": schedule_state(),
        "history": load_history()[:10],
        "stats": history_stats(),
        "units": resolve_units(load_convert().get("units", [])),
        "custom_rules": load_custom_rules(),
        "config_dir": str(CONFIG_DIR),
        # 首屏引导要用：加速是否可用、能不能自动装、许可证怎么算
        "wiztree": wiztree_status(),
    }


def build_scan(drive: str | None = None, custom_path: str | None = None,
               scope: str = "all") -> dict:
    letter = (drive or "C").rstrip(":").upper()
    targets = build_targets(letter, custom_path)
    if scope == "drive":
        targets = [t for t in targets if is_drive_specific(t)]
    results = []
    for t in targets:
        info = measure_target(t, letter)
        results.append({
            "id": t["id"], "base_id": t.get("base_id", t["id"]),
            "size": info.get("size", 0), "size_h": human(info.get("size", 0)),
            "files": info.get("files", 0),
            "items": [{**i, "size_h": human(i["size"])} for i in (info.get("items") or [])],
            "missing": bool(info.get("missing")), "error": info.get("error"),
        })
    results.sort(key=lambda r: (r["missing"], -r["size"]))
    return {"results": results, "disk": get_disk_info(letter),
            "is_admin": is_admin(), "drive": letter,
            "scan_time": datetime.now().strftime("%H:%M:%S")}


class Handler(BaseHTTPRequestHandler):
    server_version = "OCleaner/2.2"

    def log_message(self, fmt, *args):
        pass

    # ---------- CSRF 防护 ----------
    # 威胁模型：用户浏览器里开着本工具，同时又打开了一个恶意网页。
    # 那个网页可以向 http://127.0.0.1:端口 发请求（浏览器不阻止访问回环地址），
    # 于是能借用户之手清空清理历史、弹资源管理器、甚至改配置。
    # 对抗性审查实测：跨站 DELETE /history 与 POST /reveal 都返回了 200。
    # 服务端既不校验 Origin 也不发 CORS 头，浏览器因此读不到响应，
    # 但**副作用照样发生** —— 必须显式拦。
    #
    # 规则：写操作若带了 Origin/Referer 且来源不是本机，一律拒绝。
    # 用 curl 等工具不带 Origin 时仍放行（那是本机用户自己的操作）。
    def _origin_ok(self) -> bool:
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if not v:
                continue
            try:
                host = (urlparse(v).hostname or "").lower()
            except Exception:
                return False
            if host not in ("127.0.0.1", "localhost", "::1", ""):
                return False
        return True

    def _deny_csrf(self) -> bool:
        """跨站写操作直接 403。返回 True 表示已拒绝。"""
        if self._origin_ok():
            return False
        self._json({"error": "拒绝跨站请求：本工具只接受从本机界面发起的写操作"}, 403)
        return True

    # ---------- 异常兜底 ----------
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception:
            self._fail()

    def _fail(self) -> None:
        print("\n[服务端错误] " + "=" * 50, file=sys.stderr, flush=True)
        traceback.print_exc()
        print("=" * 68, file=sys.stderr, flush=True)
        try:
            self._json({"error": "服务端内部错误，详见控制台窗口"}, 500)
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        """读请求体。畸形输入一律当成空对象，交给各路由自行返回 400，
        避免抛异常变成 500。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        # 限制请求体大小：本工具的请求都很小，超大的必然是异常或恶意
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _query(self) -> dict:
        from urllib.parse import urlparse, parse_qs
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802
        route = self.path.split("?")[0]
        q = self._query()
        if route in ("/", "/index.html"):
            try:
                html = (WEB_DIR / "index.html").read_bytes()
            except OSError:
                html = "<h1>web/index.html 缺失</h1>".encode("utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if route == "/__oc__/state":
            return self._json(build_state(q.get("drive"), q.get("custom"),
                                          q.get("scope", "all")))
        if route == "/__oc__/scan":
            return self._json(build_scan(q.get("drive"), q.get("custom"),
                                         q.get("scope", "all")))
        if route == "/__oc__/history":
            return self._json(load_history())
        if route == "/__oc__/stats":
            return self._json(history_stats())
        if route == "/__oc__/steam":
            return self._json(discover_steam())
        if route == "/__oc__/games":
            # 全平台游戏库（Steam + Epic + GOG），steam 负载原样透传在 .steam 里
            return self._json(discover_games())
        if route == "/__oc__/partition":
            p = q.get("path") or ""
            if not p:
                return self._json({"error": "缺少 path 参数"}, 400)
            return self._json(analyze_dir_partition(p))
        if route == "/__oc__/subdirs":
            # 大清理项逐层拆子项：查 WizTree 常驻表，任意深度都是 O(1)
            return self._json(list_subdirs(q.get("drive") or "",
                                           q.get("path") or "",
                                           min(400, int(q.get("limit") or 80))))
        if route == "/__oc__/wiztree":
            # 新用户引导要的信息全在这里：找到没有、是不是本目录自带、怎么装
            return self._json(wiztree_status())
        if route == "/__oc__/drives":
            return self._json({"drives": list_drives(), "is_admin": is_admin()})
        if route == "/__oc__/fullscan":
            # 旧接口名保留为别名，指向新的占用树
            letter = (q.get("drive") or "C").rstrip(":").upper()
            return self._json(scan_disk_overview(letter))
        if route == "/__oc__/overview":
            letter = (q.get("drive") or "C").rstrip(":").upper()
            depth = int(q.get("depth") or DISK_TREE_MAX_DEPTH)
            return self._json(scan_disk_overview(letter, max(1, min(depth, 6))))
        if route == "/__oc__/dirlevel":
            # 整棵树已在 overview 里一次扫完，这里不再单独扫一层。
            # 保留该路由但明确返回提示，避免旧前端静默失败。
            return self._json({"error": "整棵树已在 /__oc__/overview 中返回，"
                                        "无需逐层请求", "children": []}, 410)
        if route == "/__oc__/identify":
            name = q.get("name") or ""
            path = q.get("path") or name
            kind = q.get("kind") or ("dir" if os.path.isdir(path) else "file")
            return self._json(identify(name, path, kind))
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):  # noqa: N802
        if self._deny_csrf():
            return
        route = self.path.split("?")[0]
        if route == "/__oc__/history":
            _atomic_write(HISTORY_PATH, [])
            return self._json({"ok": True})
        if route == "/__oc__/custom-rules":
            save_custom_rules([])
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self._deny_csrf():
            return
        route = self.path.split("?")[0]
        body = self._body()

        # ---- 自动获取 WizTree 便携版（可选加速，不装到系统里）----
        if route == "/__oc__/wiztree/install":
            if getattr(self.server, "wiz_busy", False):
                return self._json({"error": "正在下载中，请稍候"}, 409)
            self.server.wiz_busy = True
            try:
                r = acquire_wiztree()
            finally:
                self.server.wiz_busy = False
            return self._json({**r, "status": wiztree_status()},
                              200 if r.get("ok") else 500)

        # ---- 执行清理 ----
        if route == "/__oc__/clean":
            drive = (body.get("drive") or "C").rstrip(":").upper()
            custom_path = body.get("custom_path") or None
            ids = set(body.get("ids") or [])
            only = body.get("only_paths") or {}
            targets = build_targets(drive, custom_path)
            selected = [t for t in targets if t["id"] in ids]
            if not selected:
                return self._json({"error": "没有选择任何清理项"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "trigger": f"手动清理（{drive}:）",
                         "results": [], "freed": 0, "failed": 0,
                         "targets": len(selected)}
                for t in selected:
                    try:
                        r = clean_target(t, only.get(t["id"]), drive)
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        r = _error_result(t, drive, exc)
                    entry["results"].append(r)
                    entry["freed"] += r["freed"]
                    entry["failed"] += r["failed"]
                entry["freed_h"] = human(entry["freed"])
                append_history(entry)
                return self._json({"entry": entry, "disk": get_disk_info(drive),
                                   "stats": history_stats()})
            finally:
                self.server.busy = False

        # ---- 定时设置 ----
        if route == "/__oc__/schedule":
            s = schedule_state()
            for k in ("enabled", "interval_hours", "preset", "drive"):
                if k in body:
                    s[k] = body[k]
            if s.get("enabled"):
                hrs = max(0.25, float(s.get("interval_hours") or 24))
                s["interval_hours"] = hrs
                s["next_run"] = time.time() + hrs * 3600
            else:
                s["next_run"] = 0
            _atomic_write(SCHEDULE_PATH, s)
            return self._json(s)

        # ---- 自定义换算单位 ----
        if route == "/__oc__/units":
            cfg = load_convert()
            units = list(cfg.get("units", []))
            uid = (body.get("id") or "").strip()
            if not uid:
                return self._json({"error": "单位 id 不能为空"}, 400)
            if not any(u.get("id") == uid for u in units):
                units.append({
                    "id": uid,
                    "label": body.get("label") or uid,
                    "plural": body.get("plural") or body.get("label") or uid,
                    "bytes_per_unit": body.get("bytes_per_unit") or 0,
                    "note": body.get("note") or "",
                    "category": body.get("category") or "自定义",
                })
            cfg["units"] = units
            _atomic_write(CONVERT_PATH, cfg)
            _cache.pop(str(CONVERT_PATH), None)
            return self._json({"units": resolve_units(units), "stats": history_stats()})

        if route == "/__oc__/units/delete":
            cfg = load_convert()
            uid = body.get("id")
            cfg["units"] = [u for u in cfg.get("units", []) if u.get("id") != uid]
            _atomic_write(CONVERT_PATH, cfg)
            _cache.pop(str(CONVERT_PATH), None)
            return self._json({"units": resolve_units(cfg["units"]),
                               "stats": history_stats()})

        # ---- 自定义规则（勾选固定项 → 命名保存 → 可套用/自动清理）----
        if route == "/__oc__/custom-rules":
            rules = load_custom_rules()
            action = body.get("action")

            if action == "add" or action == "update":
                name = (body.get("name") or "").strip()
                ids = [str(i) for i in (body.get("ids") or [])]
                drive = (body.get("drive") or "C").rstrip(":").upper()
                if not name:
                    return self._json({"error": "请给规则起一个名字"}, 400)
                if not ids:
                    return self._json({"error": "请先勾选至少一个清理项，再保存为规则"}, 400)
                # 校验：勾选的必须是当前真实存在的目标 id
                valid = {t["id"] for t in build_targets(drive, body.get("custom_path") or None)}
                bad = [i for i in ids if i not in valid]
                if bad:
                    return self._json({"error": f"有 {len(bad)} 个清理项已不适用于 {drive}: 盘，"
                                                f"请重新勾选"}, 400)
                existing = next((r for r in rules if r["name"] == name), None)
                if existing:
                    existing["ids"] = ids
                    existing["drive"] = drive
                    existing["auto"] = bool(body.get("auto"))
                    rule = existing
                else:
                    rule = {"id": f"rule-{int(time.time() * 1000) % 10**10}",
                            "name": name, "ids": ids, "drive": drive,
                            "auto": bool(body.get("auto")),
                            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    rules.append(rule)
                save_custom_rules(rules)
                return self._json({"rules": rules, "saved": rule})

            if action == "delete":
                rules = [r for r in rules if r.get("id") != body.get("id")]
                save_custom_rules(rules)
                return self._json({"rules": rules})

            if action == "rename":
                new_name = (body.get("name") or "").strip()
                if not new_name:
                    return self._json({"error": "规则名不能为空"}, 400)
                for r in rules:
                    if r.get("id") == body.get("id"):
                        r["name"] = new_name
                save_custom_rules(rules)
                return self._json({"rules": rules})

            return self._json({"rules": rules})

        # ---- 执行一条自定义规则（勾选固定项的立即清理）----
        if route == "/__oc__/run-rule":
            rid = body.get("id")
            rule = next((r for r in load_custom_rules() if r["id"] == rid), None)
            if not rule:
                return self._json({"error": "规则不存在"}, 404)
            drive = (rule.get("drive") or "C").rstrip(":").upper()
            custom_path = body.get("custom_path") or None
            targets = build_targets(drive, custom_path)
            by_id = {t["id"]: t for t in targets}
            selected = [by_id[i] for i in rule["ids"] if i in by_id]
            if not selected:
                return self._json({"error": "规则里的清理项在当前盘上都不再适用，"
                                            "请重新勾选后覆盖保存"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "trigger": f"规则「{rule['name']}」（{drive}:）",
                         "rule": rule["name"],
                         "results": [], "freed": 0, "failed": 0,
                         "targets": len(selected)}
                for t in selected:
                    try:
                        r = clean_target(t, None, drive)
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        r = _error_result(t, drive, exc)
                    entry["results"].append(r)
                    entry["freed"] += r["freed"]
                    entry["failed"] += r["failed"]
                entry["freed_h"] = human(entry["freed"])
                append_history(entry)
                return self._json({"entry": entry, "disk": get_disk_info(drive),
                                   "stats": history_stats(), "rule": rule["name"]})
            finally:
                self.server.busy = False

        # ---- 在资源管理器中打开 ----
        if route == "/__oc__/reveal":
            path = expand(str(body.get("path") or ""))
            if os.path.exists(path) and IS_WINDOWS:
                subprocess.Popen(["explorer.exe", os.path.normpath(path)],
                                 creationflags=CREATE_NO_WINDOW)
                return self._json({"ok": True})
            return self._json({"error": "路径不存在"}, 404)

        return self._json({"error": "not found"}, 404)


def pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def main() -> None:
    if IS_WINDOWS:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 清掉上次被强杀时遗留的 WizTree 导出文件（正常路径在 finally 里已删）
    stale = cleanup_stale_wiztree_exports()
    if stale:
        print(f"已清理 {stale} 个上次遗留的 WizTree 导出文件")

    ap = argparse.ArgumentParser(description="O-Cleaner 磁盘清理助手")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--drive", default=None, help="命令行扫描时指定盘符，如 D")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--fullscan-only", action="store_true",
                    help="命令行扫描磁盘占用树并打印，不开服务")
    ap.add_argument("--depth", type=int, default=0,
                    help="配合 --fullscan-only 指定展开层数（默认 5）")
    ap.add_argument("--lint", action="store_true",
                    help="只跑规则 linter（发布/贡献规则前用），不开服务")
    args = ap.parse_args()

    dcfg = load_drives_cfg()

    if args.lint:
        lint = BASE_DIR / "tools" / "lint_rules.py"
        if not lint.exists():
            print("找不到 tools/lint_rules.py")
            return
        import subprocess as _sp
        sys.exit(_sp.call([sys.executable, "-X", "utf8", str(lint), "--strict"]))

    if args.fullscan_only:
        letter = (args.drive or "C").rstrip(":").upper()
        depth = int(args.depth or DISK_TREE_MAX_DEPTH)
        print(f"O-Cleaner 磁盘占用树 —— {letter}: 盘"
              f"（逐层展开到 {depth} 层，请稍候）")
        r = scan_disk_overview(letter, max(1, min(depth, 6)))
        if r.get("error"):
            print("  失败：" + r["error"])
            return
        nodes = r["nodes"]
        by_parent: dict[str, list[dict]] = {}
        for n in nodes:
            by_parent.setdefault(n["parent"], []).append(n)

        # 根节点的键是规范化后的盘符根（"e:\\"），另外深度 1 的节点
        # 其 parent 也是这个键 —— 用「深度 1 的 parent」反推最稳，
        # 不要自己拼字符串（大小写、尾斜杠、normpath 都可能不一致）。
        root_key = next((n["parent"] for n in nodes if n.get("depth") == 1), "")

        def dump(parent_key: str, indent: str = "  ") -> None:
            kids = sorted(by_parent.get(parent_key, []), key=lambda x: -x["size"])
            for k in kids:
                kind = "目录" if k["type"] == "dir" else "文件"
                if k["cleanable"]:
                    advice = "[可清理]"
                elif k["recognized"]:
                    advice = "[建议保留]"
                else:
                    advice = "[未识别]  "
                print(f"{indent}{human(k['size']):>10}  {k['name'][:32]:<34}"
                      f"{kind}  {advice}  {(k['title'] or '')[:20]}")
                dump(os.path.normpath(k["path"]).lower(), indent + "  ")

        print(f"  扫描时间 {r['scan_time']}    节点 {r['node_count']} 个，"
              f"已识别 {r['explained']} 个，子项门槛 {r['min_child_h']}")
        print(f"  顶层合计 {r['scanned_total_h']}")
        print("-" * 104)
        print("        大小  名称 / 类型 / 建议 / 识别结果")
        print("-" * 104)
        dump(root_key)
        print("-" * 104)
        cl = [n for n in nodes if n["cleanable"]]
        print(f"  可清理合计 {human(sum(n['size'] for n in cl))}（{len(cl)} 个节点）")
        return

    if args.scan_only:
        letter = (args.drive or "C").rstrip(":").upper()
        print(f"O-Cleaner 扫描报告 —— {letter}: 盘")
        print(f"{'项目':<42}{'大小':>12}{'文件数':>10}  路径")
        print("-" * 100)
        total = 0
        for t in build_targets(letter):
            info = measure_target(t, letter)
            total += info.get("size", 0)
            print(f"{expand(t.get('label', t['id']), letter):<42}"
                  f"{human(info.get('size', 0)):>12}{info.get('files', 0):>10}  "
                  f"{expand(t['path'], letter)}")
        print("-" * 100)
        print(f"{'合计':<42}{human(total):>12}")
        return

    port = pick_port(args.port or int(dcfg.get("app", {}).get("port", 8770)))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.busy = False  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    print()
    print("  O-Cleaner 已启动")
    print(f"  界面地址: {url}")
    print(f"  配置文件: {CONFIG_DIR}")
    print(f"  管理员权限: {'是' if is_admin() else '否（系统目录的项目会提示权限不足）'}")
    print("  关闭此窗口即停止服务。")
    print()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
