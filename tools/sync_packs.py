#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_packs.py —— 主仓库 packs/ 与公开分发仓 O-Cleaner-packs 的同步工具

背景：主仓库是私有的，raw 链接对外不可达；「从 GitHub 检查更新」指向独立的
公开仓库 Oerian/O-Cleaner-packs。两个仓库存着同一份 packs 数据，
**改包必须两边同步** —— 这个脚本把「同步」从手工记忆变成一条命令。

用法:
    python tools/sync_packs.py check    # 只读：比对两边差异（sha256 逐文件）
    python tools/sync_packs.py push     # 把本地有差异的文件推到公开仓

依赖:
    check 只用标准库（urllib 拉 raw）；
    push 调用 GitHub Contents API，走本机的 gh CLI（需要已 `gh auth login`，
    与主仓库的 SSH 推送通道互不影响）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKS_DIR = HERE.parent / "packs"

REPO = "Oerian/O-Cleaner-packs"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
API_BASE = f"https://api.github.com/repos/{REPO}/contents"
GH = r"C:\Program Files\GitHub CLI\gh.exe"

# 同步范围：清单 + 包文件 + 格式说明。主仓 packs/ 里的其它文件（如有）不推。
SYNC_FILES = ["index.json", "windows-system.json", "apps-common.json",
              "games-platform.json", "games-titles.json", "explanations.json",
              "README.md"]


def _local_bytes(name: str) -> bytes:
    return (PACKS_DIR / name).read_bytes()


def _remote_bytes(name: str) -> bytes | None:
    """拉公开仓文件；404 返回 None（公开仓还没有这个文件）。"""
    url = f"{RAW_BASE}/{name}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except urllib.error.URLError as e:
        print(f"  [网络] 拉取 {name} 失败：{e.reason}", file=sys.stderr)
        print("  api.github.com 与 raw.githubusercontent.com 都可能劣化，稍后重试。",
              file=sys.stderr)
        raise


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def check() -> int:
    """比对两边。返回差异数（0 = 完全一致）。"""
    diffs = 0
    for name in SYNC_FILES:
        local = _local_bytes(name)
        try:
            remote = _remote_bytes(name)
        except Exception:  # noqa: BLE001 —— 网络问题直接终止，别报成「有差异」
            return -1
        if remote is None:
            print(f"  [缺失] {name}：公开仓还没有这个文件")
            diffs += 1
        elif _sha(local) != _sha(remote):
            print(f"  [不同] {name}：本地 {len(local)} B / 公开仓 {len(remote)} B")
            diffs += 1
        else:
            print(f"  [一致] {name}")
    print()
    if diffs == 0:
        print("两边完全一致。")
    elif diffs > 0:
        print(f"共 {diffs} 个文件有差异 —— 改过包之后请运行 "
              f"`python tools/sync_packs.py push` 同步到公开仓。")
    return diffs


def _gh_api_json(endpoint: str, body: dict, retries: int = 3) -> dict:
    """经 gh CLI 调 Contents API（PUT）。gh 不在 PATH 里，用全路径。
    api.github.com 有时段性劣化（TLS 握手超时），同一个文件重试几次。"""
    last = ""
    for i in range(retries):
        proc = subprocess.run(
            [GH, "api", "--method", "PUT", f"repos/{REPO}/contents/{endpoint}",
             "--input", "-"],
            input=json.dumps(body), capture_output=True, text=True,
            encoding="utf-8", timeout=120)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        last = (proc.stderr or proc.stdout).strip()[:300]
        # 409 是内容冲突（sha 不对），重试没有意义
        if "409" in last:
            break
        print(f"    重试 {i + 1}/{retries}（{last[-80:]}）")
    raise RuntimeError(last)


def _api_file(name: str) -> dict | None:
    """经 API GET 取文件现状（sha + base64 内容）。API 无 CDN 缓存，
    是 PUT 时 sha 的唯一可靠来源 —— raw 有 CDN 缓存，用它算 sha 会 409。"""
    proc = subprocess.run(
        [GH, "api", f"repos/{REPO}/contents/{name}?ref={BRANCH}"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        if "404" in err:
            return None
        raise RuntimeError(err[:300])
    return json.loads(proc.stdout)


def push() -> int:
    """把有差异的文件推到公开仓。返回推送的文件数。"""
    pushed = 0
    for name in SYNC_FILES:
        local = _local_bytes(name)
        try:
            cur = _api_file(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  [网络] 无法读取 {name} 的现状（{exc}），中止推送以免覆盖错内容。")
            return pushed
        body = {
            "message": f"同步自主仓库 packs/：{name}",
            "content": base64.b64encode(local).decode(),
        }
        if cur is None:
            print(f"  [新增] {name}（公开仓还没有）")
        else:
            remote = base64.b64decode(cur.get("content") or "")
            if remote == local:
                print(f"  [跳过] {name}（已一致）")
                continue
            body["sha"] = cur["sha"]
            print(f"  [更新] {name}：公开仓 {len(remote)} B → 本地 {len(local)} B")
        try:
            _gh_api_json(name, body)
            pushed += 1
            print(f"  [推送] {name}（{len(local)} B）")
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {name}：{exc}")
            print("  可能原因：gh 未登录 / api.github.com 被阻断 / 令牌无 repo 权限。")
            return pushed
    print()
    print(f"推送完成：{pushed} 个文件。check 走 raw，有 CDN 缓存，"
          f"几分钟内才会显示一致。")
    return pushed


def main() -> None:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "check":
        sys.exit(0 if check() == 0 else 1)
    if cmd == "push":
        sys.exit(0 if push() >= 0 else 1)
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
