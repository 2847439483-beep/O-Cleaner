# -*- coding: utf-8 -*-
"""验证 linter 真的有检出能力（而不是空转通过）。

两组用例：
  知识库组 —— 临时往 config/filetypes.json 注入若干类缺陷，跑 linter 确认被抓到，然后还原。
  资源包组 —— 在系统临时目录里造一个假 packs/，进程内调用 lint_packs() 确认抓到缺陷。
              刻意不往仓库的 packs/ 里写文件：那样会被宿主的删除保护拦下，
              留下测试残渣反而污染仓库。用临时目录既不碰仓库也不依赖删文件。

只修改 config/filetypes.json，运行后必定还原（finally）。
"""
from __future__ import annotations
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CFG = HERE / "config" / "filetypes.json"
BAK = HERE / "config" / ".filetypes.linttest.bak"
sys.path.insert(0, str(HERE / "tools"))

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


# ----------------------------------------------------------------------
#  资源包组的用例。基准包刻意做成完全合法，每个用例只改坏一处。
# ----------------------------------------------------------------------
PACK_BASE = {
    "id": "probe", "name": "测试包", "version": "1.0.0", "type": "custom",
    "description": "仅用于 linter 自检的包",
    "entries": [{
        "id": "ut-entry", "title": "测试条目", "category": "自定义目录",
        "risk": "低", "cleanable": True,
        "match": {"any": ["\\foo\\barcache"]},
        "what": "这是一个用于自检的缓存目录。",
        "effect": "删除没有影响。", "rebuild": "会重建。", "keep": "可直接删除。",
    }],
}


def _set_entry(key, val):
    def go(p):
        p["entries"][0][key] = val
    return go


PACK_CASES = [
    ("资源包 id 非法", lambda p: p.__setitem__("id", "bad id!")),
    ("资源包缺 name", lambda p: p.pop("name", None)),
    ("资源包缺 version", lambda p: p.pop("version", None)),
    ("资源包 entries 为空", lambda p: p.__setitem__("entries", [])),
    ("条目缺 title", lambda p: p["entries"][0].pop("title", None)),
    ("条目缺 match", lambda p: p["entries"][0].pop("match", None)),
    ("match 既无 any 也无 all", _set_entry("match", {"some": ["x"]})),
    ("match.any 为空数组", _set_entry("match", {"any": []})),
    ("条目 id 重复",
     lambda p: p["entries"].append(dict(p["entries"][0]))),
    ("条件 risk 非法", _set_entry("risk", "极高")),
    ("cleanable 写成字符串", _set_entry("cleanable", "true")),
    ("only 非法", _set_entry("only", "folder")),
    ("正则关键词编译失败", _set_entry("match", {"any": ["re:[unclosed"]})),
    ("解释字段为空白字符串", _set_entry("what", "   ")),
    ("category 为空字符串", _set_entry("category", "")),
    ("单段泛化关键词（警告级）", _set_entry("match", {"any": ["\\temp"]})),
    ("ASCII 关键词过短（警告级）", _set_entry("match", {"any": ["\\ab"]})),
    ("id 与文件名不一致（警告级）", None),      # 由 pack_case_file 换文件名完成
    ("未知 type（警告级）", lambda p: p.__setitem__("type", "novel")),
    ("清单漏登同名包", "__orphan__"),
]


def run_pack_lint(pack: dict, file_name: str = "probe.json",
                  listed: bool = True) -> tuple[list[str], list[str]]:
    """在临时目录里跑一次 lint_packs()。返回 (错误列表, 警告列表)。

    不写仓库文件，因此不会触发宿主的文件删除保护，也就没有测试残渣。
    """
    import lint_rules as L
    tmp = Path(tempfile.mkdtemp(prefix="oc_lint_pack_"))
    saved = L.PACKS
    try:
        (tmp / file_name).write_text(
            json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        names = [file_name] if listed else []
        (tmp / "index.json").write_text(
            json.dumps({"packs": names}, ensure_ascii=False), encoding="utf-8")
        L.PACKS = tmp
        L.ERRORS.clear()
        L.WARNINGS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            L.lint_packs()
        return list(L.ERRORS), list(L.WARNINGS)
    finally:
        L.PACKS = saved
        L.ERRORS.clear()
        L.WARNINGS.clear()
        shutil.rmtree(tmp, ignore_errors=True)


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

        # --------------------------------------------------------------
        #  资源包组
        # --------------------------------------------------------------
        print("\n  —— 资源包检查的检出能力 ——")
        try:
            errs, warns = run_pack_lint(PACK_BASE)
        except Exception as e:                        # noqa: BLE001
            print(f"  [跳过] 资源包组：无法调用 lint_packs（{e}）")
            errs = warns = None
        if errs is not None:
            if not errs and not warns:
                PASS.append("干净资源包应通过")
                print("  [OK]   干净资源包通过 lint_packs")
            else:
                FAIL.append("干净资源包应通过")
                print(f"  [严重] 干净资源包没通过：{errs} {warns}")

            for name, mutate in PACK_CASES:
                pack = json.loads(json.dumps(PACK_BASE))
                fname, listed = "probe.json", True
                if mutate == "__orphan__":
                    listed = False           # 文件在磁盘上但没进 index.json
                elif mutate is None:
                    fname = "other-name.json"   # id 与文件名不一致
                else:
                    mutate(pack)
                errs, warns = run_pack_lint(pack, fname, listed)
                # CI 用的是 --strict：警告同样会让门禁失败，所以警告也算「检出」。
                caught = bool(errs) or bool(warns)
                (PASS if caught else FAIL).append("资源包：" + name)
                print(("  [OK]   " if caught else "  [严重] ") + f"能检出：{name}")
                if not caught:
                    print("        （lint_packs 通过了，说明这一类缺陷没被覆盖）")
    finally:
        shutil.move(str(BAK), str(CFG))

    print(f"\n{'=' * 52}\n  通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("  未覆盖：" + "、".join(FAIL))
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
