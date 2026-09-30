# -*- coding: utf-8 -*-
"""验证 linter 真的有检出能力（而不是空转通过）。

临时往知识库注入若干类缺陷，运行 linter，确认每一类都被抓到，然后还原。
只修改 config/filetypes.json，运行后必定还原（finally）。
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CFG = HERE / "config" / "filetypes.json"
BAK = HERE / "config" / ".filetypes.linttest.bak"

def idx(rules: list, rule_id: str) -> int:
    """按 id 定位规则下标。不要硬编码顺序 —— 顺序会变，测试会静默失效。"""
    for i, r in enumerate(rules):
        if r.get("id") == rule_id:
            return i
    raise KeyError(rule_id)


# 用例：(名称, 变异函数)。全部按 id 定位，避免依赖列表顺序。
CASES = [
    ("必填字段缺失",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "steam-library")].pop("advice", None)),
    ("cleanable 写成字符串",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "download-dir")].__setitem__("cleanable", "true")),
    ("kind 非法",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "cache-temp")].__setitem__("kind", "fuzzy")),
    ("risk 非法",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "logs-dumps")].__setitem__("risk", "极高")),
    ("id 重复",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "music-app")].__setitem__(
         "id", "cache-temp")),
    ("扩展名关键词缺前导点",
     lambda k: k["file_rules"][idx(k["file_rules"], "installer-pkg")].__setitem__("match", ["exe"])),
    ("what 过短",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "cloud-disk")].__setitem__("what", "短")),
    ("高风险可清理但无 tip（且未豁免）",
     lambda k: (k["folder_rules"][idx(k["folder_rules"], "node-modules")].__setitem__("risk", "高"),
                k["folder_rules"][idx(k["folder_rules"], "node-modules")].__setitem__("cleanable", True),
                k["folder_rules"][idx(k["folder_rules"], "node-modules")].pop("tip", None))),
    # 真正的撞车：两条 substring 规则共用一个关键词，且建议相反
    ("关键词撞车且建议相反",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "steam-library")].__setitem__(
         "match", ["steamlibrary", "steamapps", "cache"])),
    ("分类拼写错误（警告级）",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "gopro-exports")].__setitem__(
         "category", "遊戏")),
    ("match 为空数组",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "emulator")].__setitem__("match", [])),
    ("advice 过短",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "backup-dir")].__setitem__("advice", "删")),
    ("match 含空关键词",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "drivers")].__setitem__(
         "match", ["driver", "驱动", ""])),
    ("confidence 非法",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "video-media")].__setitem__(
         "confidence", "very-high")),
    ("递归引用自身导致死循环防护（match 全为空串）",
     lambda k: k["file_rules"][idx(k["file_rules"], "archive")].__setitem__("match", ["  "])),
]

PASS, FAIL = [], []


def run_lint(strict: bool = True) -> tuple[int, str]:
    cmd = [sys.executable, "-X", "utf8", str(HERE / "tools" / "lint_rules.py")]
    if strict:
        cmd.append("--strict")     # CI 用的就是这个模式，警告必须算失败
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    if sys.stdout.reconfigure:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    shutil.copy2(CFG, BAK)
    base = json.loads(CFG.read_text(encoding="utf-8-sig"))
    try:
        code, out = run_lint()
        if code == 0:
            PASS.append("干净知识库应通过")
            print("  [OK]   干净知识库通过 linter")
        else:
            FAIL.append("干净知识库应通过")
            print("  [严重] 干净知识库竟然没通过：")
            print("\n".join(out.splitlines()[-12:]))

        for name, mutate in CASES:
            kb = json.loads(json.dumps(base))          # 每次从干净副本出发
            try:
                mutate(kb)
            except Exception as e:                     # noqa: BLE001
                print(f"  [跳过] {name}：注入失败 {e}")
                continue
            CFG.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
            code, out = run_lint()
            caught = code != 0
            (PASS if caught else FAIL).append(name)
            print(("  [OK]   " if caught else "  [严重] ") + f"能检出：{name}")
            if not caught:
                print("        （linter 通过了，说明这一类缺陷没被覆盖）")

        CFG.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
        code, out = run_lint()
        restored = code == 0
        (PASS if restored else FAIL).append("还原后应恢复通过")
        print(("  [OK]   " if restored else "  [严重] ") + "还原后重新通过")
    finally:
        shutil.move(str(BAK), str(CFG))

    print(f"\n{'=' * 52}\n  通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("  未覆盖：" + "、".join(FAIL))
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
