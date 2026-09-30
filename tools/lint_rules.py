# -*- coding: utf-8 -*-
"""
规则 linter —— 发布前的安全闸门。

一条错误的识别规则会真的让用户删掉不该删的东西，而用户不会看免责声明。
所以在接受任何规则贡献之前，必须先过这一关。

检查内容：
  结构层  ID 唯一、必填字段、类型正确、枚举合法、关键词非空且不重复
  语义层  分类是否在已知集合内、cleanable 与 risk 是否自相矛盾
  冲突层  关键词在规则之间撞车（A 的词被 B 抢走）
  路由层  用知识库自己的每个关键词去跑真实识别，验证「归属规则」能否赢
  一致性  项目文档与配置是否漂移（如 README 里的文件清单）

用法:
    python -X utf8 tools/lint_rules.py
    python -X utf8 tools/lint_rules.py --strict     # 警告也视为失败（CI 用）
    python -X utf8 tools/lint_rules.py --json       # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
HERE = TOOLS_DIR.parent
sys.path.insert(0, str(HERE))

CONFIG = HERE / "config"
FILETYPES = CONFIG / "filetypes.json"
TARGETS = CONFIG / "targets.json"
DRIVES = CONFIG / "drives.json"

# 已知分类集合。出现新分类多半是拼写错误；确实要新增时把它加到这里。
KNOWN_CATEGORIES = {
    "游戏", "模拟器", "个人数据", "下载", "聊天软件", "云盘", "创作软件", "创作数据",
    "软件", "工具", "音乐", "视频", "音频", "文档", "开发", "AI 模型", "虚拟机",
    "系统", "缓存", "诊断", "压缩包", "镜像", "安装包", "备份", "回收站", "浏览器",
    "其它",
}
KNOWN_RISKS = {"无", "低", "中", "高"}
KNOWN_CONFIDENCE = {"high", "medium", "low"}

# 过于宽泛的关键词：会匹配到大量无关目录或文件
GENERIC_KEYWORDS = {
    "data", "file", "files", "my", "new", "old", "test", "temp", "app", "apps",
    "doc", "docs", "img", "image", "images", "user", "users", "系统", "我的",
    "新建", "工具", "文件", "数据", "备份", "下载",
}

# 「已复核、有意为之」的豁免清单。
# 目的：让 --strict 能通过，同时不逼迫作者为了过 CI 而削弱规则的表达力。
# 每一项都必须写明理由和复核日期，并给出联系方式外的责任人（PR 作者）。
# 注意：豁免只影响本文件里的规则检查，不会让引擎行为发生变化。
ACCEPTED: dict[str, dict[str, str]] = {
    "keyword:temp": {
        "reason": "temp/tmp 是最常见的临时目录命名，虽然有误匹配风险，"
                  "但引擎对 ≤4 字符的英文关键词要求整词边界匹配，已足够安全。"
                  "已有 selftest 覆盖 temp 相关用例。",
        "reviewed": "2026-09-16",
    },
    "keyword:下载": {
        "reason": "中文无词边界，但「下载」作为目录名指向明确，"
                  "且它只把项目标为「可清理」而非直接删除，用户仍需确认。",
        "reviewed": "2026-09-16",
    },
    "keyword:备份": {
        "reason": "备份目录标记为「不可清理」，误匹配的后果是少提示而非多删除，"
                  "属于安全方向的偏差。",
        "reviewed": "2026-09-16",
    },
    "keyword:工具": {
        "reason": "「工具」目录在中文环境常见，且 software-dir 为「不可清理」，"
                  "误匹配只会导致保守判断。",
        "reviewed": "2026-09-16",
    },
    "high-risk-cleanable:recycle-bin": {
        "reason": "回收站在语义上确实可清理（它就是待删文件），风险高的原因是不可恢复。"
                  "引擎的 recycle 预设与主列表都把它单独列出，并要求用户展开确认条目，"
                  "风险已被 UI 与原语共同约束。",
        "reviewed": "2026-09-16",
    },
}


def accepted(key: str) -> bool:
    if key in ACCEPTED:
        USED_ACCEPTED.add(key)
        return True
    return False


def accepted_reason(key: str) -> str:
    a = ACCEPTED.get(key, {})
    return f"已复核豁免（{a.get('reviewed', '?')}）：{a.get('reason', '')}"


def lint_accepted_list() -> None:
    """豁免清单自身也要能被检查：已不再触发的条目应当删掉。

    否则这份清单会慢慢变成「掩盖新问题」的垃圾场 —— 那才是真正的风险。
    """
    print("\n[附] 豁免清单检查")
    stale = sorted(set(ACCEPTED) - USED_ACCEPTED)
    for k in stale:
        warn(f"豁免 {k!r} 已不再触发（规则或被删、或已修正），请从 ACCEPTED 里移除")
    print(f"  登记 {len(ACCEPTED)} 条，本次生效 {len(USED_ACCEPTED)} 条，"
          f"失效 {len(stale)} 条")

# 关键词长度下限（英文），低于此值必须整词匹配才安全（引擎里已实现）
SHORT_KEYWORD_LEN = 4

REQUIRED_FIELDS = ("id", "match", "kind", "title", "category", "risk",
                   "cleanable", "confidence", "what", "advice")
ADVICE_MIN_LEN = 12
WHAT_MIN_LEN = 15

ERRORS: list[str] = []
WARNINGS: list[str] = []
NOTES: list[str] = []
USED_ACCEPTED: set[str] = set()


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def note(msg: str) -> None:
    NOTES.append(msg)


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        err(f"缺文件：{path.relative_to(HERE)}")
    except json.JSONDecodeError as e:
        err(f"JSON 解析失败 {path.relative_to(HERE)}：{e}")
    return None


# ======================================================================
#  1. 结构层
# ======================================================================
def lint_structure(all_rules: list[dict]) -> None:
    print("\n[1/6] 结构检查")
    seen_ids: dict[str, str] = {}
    for src, rules in (("folder", [r for r in all_rules if r.get("_src") == "folder"]),
                       ("file", [r for r in all_rules if r.get("_src") == "file"])):
        for i, r in enumerate(rules):
            where = f"{src}_rules[{i}] id={r.get('id', '?')}"
            for f in REQUIRED_FIELDS:
                if f not in r:
                    err(f"{where} 缺必填字段 {f}")
            rid = r.get("id")
            if rid:
                if rid in seen_ids:
                    err(f"{where} id 与 {seen_ids[rid]} 重复")
                seen_ids[rid] = where
            if not isinstance(r.get("cleanable"), bool):
                err(f"{where} cleanable 必须是布尔值（不要写字符串）")
            if r.get("kind") not in ("exact", "substring", "extension"):
                err(f"{where} kind 非法：{r.get('kind')}（应为 exact/substring/extension）")
            if r.get("risk") not in KNOWN_RISKS:
                err(f"{where} risk 非法：{r.get('risk')}（应为 {'/'.join(sorted(KNOWN_RISKS))}）")
            if r.get("confidence") not in KNOWN_CONFIDENCE:
                err(f"{where} confidence 非法：{r.get('confidence')}")
            m = r.get("match")
            if not isinstance(m, list) or not m:
                err(f"{where} match 必须是非空数组")
            else:
                if len(set(m)) != len(m):
                    dup = [k for k in set(m) if list(m).count(k) > 1]
                    warn(f"{where} match 内有重复关键词：{dup}")
                if r.get("kind") == "extension":
                    bad = [k for k in m if not str(k).startswith(".")]
                    if bad:
                        err(f"{where} kind=extension 的关键词必须以点开头：{bad}")
                if r.get("kind") == "exact" and any(
                        all(ord(c) < 128 for c in str(k)) and len(str(k)) < 3 for k in m):
                    warn(f"{where} kind=exact 出现过短关键词，有误匹配风险")
                if r.get("kind") == "substring":
                    empt = [k for k in m if not str(k).strip()]
                    if empt:
                        err(f"{where} match 含空关键词")
            if r.get("category") not in KNOWN_CATEGORIES:
                warn(f"{where} 分类 {r.get('category')!r} 不在已知集合内（拼写错误？）")
            if len(str(r.get("what") or "")) < WHAT_MIN_LEN:
                err(f"{where} what 过短（至少 {WHAT_MIN_LEN} 字，需说明这是什么）")
            if len(str(r.get("advice") or "")) < ADVICE_MIN_LEN:
                err(f"{where} advice 过短（至少 {ADVICE_MIN_LEN} 字，需说明怎么处理）")

    # 关键词在规则内自撞（同一规则里 A 是 B 的子串是允许的，跨规则才是问题）
    note(f"共检查 {len(all_rules)} 条规则、{len(seen_ids)} 个唯一 id")
    print(f"  {'通过' if not ERRORS else '发现问题'}：错误 {len(ERRORS)}，警告 {len(WARNINGS)}")


# ======================================================================
#  2. 语义自相矛盾
# ======================================================================
def lint_semantics(all_rules: list[dict]) -> None:
    print("\n[2/6] 语义一致性检查")
    n0 = len(ERRORS)
    for r in all_rules:
        rid = r.get("id")
        # 高风险 + 可清理 = 危险组合，必须显式写明保留条件
        if r.get("risk") == "高" and r.get("cleanable"):
            key = f"high-risk-cleanable:{rid}"
            if accepted(key):
                note(f"[豁免] {rid} 高风险可清理 —— {accepted_reason(key)}")
            elif not r.get("tip"):
                err(f"{rid} 标为『高风险 + 可清理』却没有 tip 说明例外情况，"
                    f"这对用户太危险")
            else:
                warn(f"{rid} 标为『高风险 + 可清理』，请确认这是有意为之"
                     f"（引擎会把它列入清理建议）。若确认无误，请在 "
                     f"lint_rules.py 的 ACCEPTED 里登记豁免并写明理由")
        # 不可清理却声称能自动重建，逻辑上不一致
        if not r.get("cleanable"):
            rb = str(r.get("what", "")) + str(r.get("advice", ""))
            if "可以清理" in rb and "不要" not in rb:
                warn(f"{rid} cleanable=false 但文案里写着『可以清理』，容易误导")
        # 建议清理的规则必须给出去向说明，否则用户不知道该不该信
        if r.get("cleanable") and not r.get("advice"):
            err(f"{rid} cleanable=true 但没有 advice")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  3. 关键词跨规则冲突
# ======================================================================
def lint_keyword_collisions(all_rules: list[dict]) -> None:
    print("\n[3/6] 关键词冲突检查")
    n0 = len(ERRORS)
    k2r: dict[str, list[dict]] = {}
    for r in all_rules:
        for k in (r.get("match") or []):
            k2r.setdefault(str(k).lower(), []).append(r)

    for k, owners in sorted(k2r.items()):
        if len(owners) == 1:
            continue
        ids = {o.get("id") for o in owners}
        if len(ids) == 1:
            continue                      # 同一规则内重复，已在结构层处理
        # 归属不同规则：只有当它们对用户的建议相反时才危险
        cleanable = {bool(o.get("cleanable")) for o in owners}
        detail = "、".join(f"{o.get('id')}(可清理={o.get('cleanable')})" for o in owners)
        if len(cleanable) > 1:
            err(f"关键词 {k!r} 被多个建议相反的规则共用：{detail}")
        else:
            warn(f"关键词 {k!r} 被多条规则共用：{detail}")

    # 宽泛关键词
    for k in sorted(k2r):
        if k not in GENERIC_KEYWORDS:
            continue
        key = f"keyword:{k}"
        if accepted(key):
            note(f"[豁免] 关键词 {k!r} 已知宽泛 —— {accepted_reason(key)}")
        else:
            warn(f"关键词 {k!r} 过于宽泛，容易误匹配（归属 "
                 f"{'、'.join(o.get('id') or '?' for o in k2r[k])}）。"
                 f"若确认必要，请在 lint_rules.py 的 ACCEPTED 里登记豁免并写明理由")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  4. 路由验证 —— 用真实识别引擎验证归属
# ======================================================================
def lint_routing(all_rules: list[dict]) -> None:
    """对每条规则的每个关键词，构造样本跑真实 identify()，验证归属规则能赢。"""
    print("\n[4/6] 路由验证（用真实引擎跑知识库自己的关键词）")
    n0 = len(ERRORS)
    try:
        import app
    except Exception as e:                # noqa: BLE001
        err(f"无法导入 app.py，跳过路由验证：{e}")
        return

    checked = 0
    ctx_skipped = 0
    stolen_dangerous: list[str] = []
    stolen_benign: list[str] = []
    # 这些规则的匹配依赖「真实文件系统上下文」，用不存在的合成路径测不出结果：
    # 浏览器目录必须真的含 User Data / Default / Cache 结构才算数，
    # 否则 D:\Chrome 这种合成路径必然不匹配，只会产生误报。
    CONTEXT_RULES = {"browser-data", "browser-profile"}
    for r in all_rules:
        is_dir = r.get("_src") == "folder"
        for k in (r.get("match") or []):
            k = str(k)
            if r.get("kind") == "extension":
                sample = "sample" + k
            elif is_dir:
                sample = k
            else:
                # 文件规则：关键词可能是文件名或扩展名
                sample = k if not k.startswith(".") else "sample" + k
            if r.get("id") in CONTEXT_RULES and is_dir:
                ctx_skipped += 1
                continue
            got = app.identify(sample, "D:\\" + sample, "dir" if is_dir else "file")
            checked += 1
            if got.get("rule_id") == r.get("id"):
                continue
            # 归属失败：判断危险程度
            if r.get("cleanable") and not got.get("cleanable"):
                stolen_benign.append(
                    f"{r.get('id')} 的关键词 {k!r} 被 {got.get('rule_id') or '未识别'} 抢走"
                    f"（本该可清理，结果变成不建议清理）")
            elif r.get("cleanable") and got.get("cleanable"):
                stolen_dangerous.append(
                    f"{r.get('id')} 的关键词 {k!r} 被 {got.get('rule_id')} 抢走"
                    f"（两条都是可清理，实际归类为 {got.get('title')}）")
            else:
                stolen_benign.append(
                    f"{r.get('id')} 的关键词 {k!r} 被 {got.get('rule_id') or '未识别'} 抢走"
                    f"（都不建议清理，影响较小）")

    # 只有「两条都建议清理但归属不同」才会让用户删错东西，算错误；
    # 其余归属失败只影响解释准确性，算警告。
    for m in stolen_dangerous:
        err(m)
    for m in stolen_benign:
        warn(m)
    print(f"  验证了 {checked} 个关键词样本（另有 {ctx_skipped} 个上下文依赖型规则跳过），"
          f"归属失败 {len(stolen_dangerous)} 危险 / {len(stolen_benign)} 轻微")
    note(f"路由验证样本数 {checked}")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  5. 与项目文档/配置的一致性
# ======================================================================
def lint_consistency() -> None:
    print("\n[5/6] 文档与配置一致性")
    n0 = len(ERRORS)
    readme = (HERE / "README.md")
    if not readme.exists():
        warn("缺 README.md")
    else:
        txt = readme.read_text(encoding="utf-8")
        for f in ("app.py", "config\\targets.json", "config\\filetypes.json",
                  "web\\index.html", "tools\\selftest.py", "tools\\lint_rules.py"):
            if Path(f).name not in txt:
                warn(f"README 未提到 {f}")

    # targets.json 检查
    t = load(TARGETS)
    if t:
        ids = [x.get("id") for x in t.get("targets", [])]
        if len(ids) != len(set(ids)):
            err("targets.json 存在重复目标 id")
        for x in t.get("targets", []):
            for f in ("id", "label", "path", "category", "risk", "kind",
                      "short", "what", "rebuild", "impact", "keep_if"):
                if not x.get(f):
                    err(f"targets.json 目标 {x.get('id')} 缺字段 {f}")
            if x.get("kind") not in ("tree", "files", "recyclebin"):
                err(f"targets.json 目标 {x.get('id')} kind 非法：{x.get('kind')}")
            if x.get("kind") == "files" and not x.get("glob"):
                err(f"targets.json 目标 {x.get('id')} kind=files 但没给 glob")
        for p in t.get("presets", []):
            sel = p.get("select", {})
            if "ids" not in sel and "risk" not in sel:
                err(f"targets.json 预设 {p.get('id')} 既没给 ids 也没给 risk")
            for want in sel.get("ids", []) or []:
                if want not in ids:
                    err(f"targets.json 预设 {p.get('id')} 引用了不存在的目标 {want}")

    d = load(DRIVES)
    if d is not None and not d.get("app", {}).get("name"):
        err("drives.json 缺 app.name")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  6. 危险删除面 —— 明确列出「这条规则会让工具去删什么」
# ======================================================================
def lint_delete_surface(all_rules: list[dict]) -> None:
    print("\n[6/6] 可清理规则清单（人工复核用）")
    cl = [r for r in all_rules if r.get("cleanable")]
    print(f"  共 {len(cl)} 条规则会把项目列入清理建议：")
    for r in sorted(cl, key=lambda x: (x.get("risk") != "高", x.get("id") or "")):
        mark = "!!!" if r.get("risk") == "高" else (" ! " if r.get("risk") == "中" else "   ")
        note(f"{mark} {r.get('id'):<24} 风险{r.get('risk')}  {r.get('title')}")
    high = [r for r in cl if r.get("risk") == "高"]
    note(f"其中高风险 {len(high)} 条")
    print(f"  高风险项 {len(high)} 条（已在上面标 !!!，请逐条人工确认）")


def main() -> int:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="O-Cleaner 规则 linter")
    ap.add_argument("--strict", action="store_true", help="警告也视为失败")
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = ap.parse_args()

    print("O-Cleaner 规则 linter")
    print("=" * 68)

    kb = load(FILETYPES)
    if not kb:
        print("\n无法加载知识库，终止。")
        return 1

    all_rules = []
    for r in kb.get("folder_rules", []):
        all_rules.append({**r, "_src": "folder"})
    for r in kb.get("file_rules", []):
        all_rules.append({**r, "_src": "file"})

    lint_structure(all_rules)
    lint_semantics(all_rules)
    lint_keyword_collisions(all_rules)
    lint_routing(all_rules)
    lint_consistency()
    lint_delete_surface(all_rules)
    lint_accepted_list()

    print("\n" + "=" * 68)
    if ERRORS:
        print(f"错误 {len(ERRORS)} 项：")
        for m in ERRORS:
            print("  [错误] " + m)
    if WARNINGS:
        print(f"\n警告 {len(WARNINGS)} 项：")
        for m in WARNINGS:
            print("  [警告] " + m)
    if not ERRORS and not WARNINGS:
        print("全部检查通过。")

    ok = not ERRORS and not (args.strict and WARNINGS)
    print("=" * 68)
    print(f"结论：{'通过' if ok else '不通过'}"
          f"（错误 {len(ERRORS)}，警告 {len(WARNINGS)}）"
          + ("  [--strict]" if args.strict else ""))

    if args.json:
        print(json.dumps({"ok": ok, "errors": ERRORS, "warnings": WARNINGS,
                          "notes": NOTES}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
