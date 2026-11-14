"""给未挂规则的知识点生成「建议挂哪些规则」的候选表，供人工确认后再落库。

为什么不让脚本直接写库：
知识点 ↔ 规则的对应关系是**考纲业务判断**，不是技术判断。脚本只能把「有证据支持的猜测」
摆出来让人看一眼，拍板必须是人。所以本脚本默认**只读**，落库要显式 --apply --confirmed。

匹配方式（显式术语词典，不玩子串相似度）
词典 TERM_RULES 里每个术语都标了来源：
  - "confirmed"：来自库里**已人工挂过规则**的 19 个知识点，把它们的知识点名去掉泛词后
    当术语，规则沿用它们已挂的那几条（有人工先例）。
  - "rule-name"：来自规则名里的领域词，但库里没有人工先例（弱证据，默认只标「待定」）。
排除表 EXCLUDE_TERMS 用来挡误伤：这套规则只覆盖增值税/企业所得税/账表勾稽，
像「土地增值税法律制度」里的「增值税」是**另一个税种**，不能配到增值税规则上。

产物是 Markdown 表，人改完「决定」列再回喂给 --apply。

用法：
    # 只看建议（只读）
    python scripts/suggest_kp_rules.py --out 知识点规则候选表.md
    # 人改完后落库
    python scripts/suggest_kp_rules.py --confirmed 知识点规则候选表.md --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT.parent / "TaxPearls" / "instance" / "taxpearls.db"

# 术语 → (规则列表, 证据来源)
# "confirmed" 的行全部能在库里找到人工先例：见函数 explain() 会把先例知识点名一并打印。
TERM_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    # —— 有人工先例 ——
    "存货":        (("R-013", "R-014", "R-019"), "confirmed"),
    "营业收入":     (("R-001", "R-007", "R-015", "R-022"), "confirmed"),
    "收入":        (("R-001", "R-007", "R-015", "R-022"), "confirmed"),
    "营业成本":     (("R-008", "R-020", "R-021"), "confirmed"),
    "成本":        (("R-008", "R-020", "R-021"), "confirmed"),
    "利润表":      (("R-007", "R-010"), "confirmed"),
    "本年利润":     (("R-011",), "confirmed"),
    "留存收益":     (("R-011",), "confirmed"),
    "未分配利润":    (("R-011",), "confirmed"),
    "利润":        (("R-007", "R-010", "R-011"), "confirmed"),
    "资产负债":     (("R-009",), "confirmed"),
    "现金流":      (("R-012",), "confirmed"),
    "现金流量":     (("R-012",), "confirmed"),
    "货币资金":     (("R-015",), "confirmed"),
    "职工薪酬":     (("R-018",), "confirmed"),
    "应付职工薪酬":   (("R-018",), "confirmed"),
    "工资":        (("R-018",), "confirmed"),
    "所得税费用":    (("R-010",), "confirmed"),
    "期间费用":     (("R-023",), "confirmed"),
    "费用":        (("R-023",), "confirmed"),
    "税金及附加":    (("R-003",), "confirmed"),
    "增值税":      (("R-001", "R-002", "R-005", "R-006", "R-016", "R-017"), "confirmed"),
    "个人所得税":    (("R-004", "R-018"), "confirmed"),
    "个税":        (("R-004", "R-018"), "confirmed"),
    "社会保险":     (("R-004",), "confirmed"),
    "社保":        (("R-004",), "confirmed"),
    "会计科目":     (("R-009",), "confirmed"),
    # —— 只有规则名作证据，无人工先例（默认待定）——
    "毛利率":      (("R-020", "R-021"), "rule-name"),
    "毛利率分析":    (("R-020", "R-021"), "rule-name"),
    "销项":        (("R-005", "R-016"), "rule-name"),
    "进项":        (("R-006", "R-014", "R-017"), "rule-name"),
    "开票":        (("R-016", "R-017", "R-019"), "rule-name"),
    "发票":        (("R-016", "R-017", "R-019"), "rule-name"),
    "税负率":      (("R-003",), "rule-name"),
    "回款":        (("R-015",), "rule-name"),
    "亏损":        (("R-024",), "rule-name"),
}

# 命中这些词就判定「这套规则不覆盖」，直接不给建议
EXCLUDE_TERMS = (
    "土地增值税", "消费税", "关税", "印花税", "契税", "房产税", "城镇土地使用税",
    "环境保护税", "资源税", "车船税", "船舶吨税", "烟叶税", "城市维护建设税",
    "支付结算", "劳动合同", "会计法律制度", "税收征管", "政府会计", "票据", "银行卡",
    "网上支付", "汇兑", "委托收款", "国内信用证", "预付卡",
)


def load(db_path: str):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    kps = [dict(r) for r in db.execute(
        "SELECT id, name, subject, parent_id FROM knowledge_point ORDER BY name")]
    rules = rule_rows(db)
    links: dict[str, set[str]] = {}
    for r in db.execute(
            "SELECT knowledge_point_id, target_id FROM knowledge_point_link"
            " WHERE target_type='rule' AND active=1"):
        links.setdefault(r["knowledge_point_id"], set()).add(r["target_id"])
    return db, kps, rules, links


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def rule_rows(db: sqlite3.Connection) -> list[dict]:
    """优先用运行时真正在用的 24 条 YAML；库里的 TaxRule 是图谱快照（同 rule_code 多版本行）。"""
    import yaml  # 规则就是 YAML，直接读源，免得跟运行时函数签名耦合
    found = []
    for path in sorted((ROOT / "rules").glob("R-0*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if doc.get("id"):
            found.append({"id": str(doc["id"]), "name": str(doc.get("name", ""))})
    if found:
        return found
    if table_exists(db, "TaxRule"):
        seen: dict[str, dict] = {}
        for r in db.execute("SELECT rule_code, name FROM TaxRule ORDER BY rule_code"):
            seen.setdefault(r["rule_code"], {"id": r["rule_code"], "name": r["name"]})
        return [seen[k] for k in sorted(seen)]
    return []


def build_rows(db_path: str):
    db, kps, rules, links = load(db_path)
    db.close()

    by_id = {k["id"]: k for k in kps}
    rule_name = {r["id"]: r["name"] for r in rules}
    # 术语 → 哪些已确认知识点用过它（用于把证据写进表给人看）
    precedent: dict[str, list[str]] = {}
    for kp in kps:
        if not links.get(kp["id"]):
            continue
        for term in TERM_RULES:
            if term in kp["name"]:
                precedent.setdefault(term, []).append(kp["name"])

    rows = []
    for kp in kps:
        if links.get(kp["id"]):
            continue
        name = kp["name"]
        parent = by_id.get(kp["parent_id"] or "", {})
        parent_name = parent.get("name", "")

        if any(bad in name for bad in EXCLUDE_TERMS):
            rows.append({
                "id": kp["id"], "name": name, "subject": kp["subject"] or "",
                "parent": parent_name, "rules": "", "decide": "n",
                "reason": "税种/领域不在现有 24 条规则覆盖范围内，无建议",
            })
            continue

        hits = [t for t in TERM_RULES if t in name]
        if not hits:
            rows.append({
                "id": kp["id"], "name": name, "subject": kp["subject"] or "",
                "parent": parent_name, "rules": "", "decide": "n",
                "reason": "名称里没有可识别的领域术语，需人工判断",
            })
            continue

        # 长术语优先（「营业成本」比「成本」更具体）
        hits.sort(key=len, reverse=True)
        picks: list[str] = []
        for term in hits:
            for rid in TERM_RULES[term][0]:
                if rid not in picks:
                    picks.append(rid)
        sources = {TERM_RULES[t][1] for t in hits}
        main = hits[0]
        if TERM_RULES[main][1] == "confirmed":
            who = "、".join(precedent.get(main, [])[:2]) or "（无）"
            reason = f"术语「{main}」，人工先例：{who}"
            decide = "y"
        else:
            names = "、".join(rule_name.get(r, r) for r in TERM_RULES[main][0][:2])
            reason = f"术语「{main}」仅来自规则名（{names}），无人工先例"
            decide = "?"
        if len(sources) > 1:
            reason += "；另命中 " + "、".join(h for h in hits[1:3])
        rows.append({
            "id": kp["id"], "name": name, "subject": kp["subject"] or "",
            "parent": parent_name, "rules": ",".join(picks[:4]),
            "decide": decide, "reason": reason,
        })

    rows.sort(key=lambda r: (r["decide"] != "y", r["decide"] != "?", r["name"]))
    return rows, len(kps), sum(1 for _ in [k for k in kps if links.get(k["id"])])


def render(rows: list[dict], total: int, linked: int, db_path: str) -> str:
    yes = sum(1 for r in rows if r["decide"] == "y")
    maybe = sum(1 for r in rows if r["decide"] == "?")
    none = len(rows) - yes - maybe
    head = (
        "# 知识点 ↔ 规则 候选映射表（待人工确认）\n\n"
        f"> 来源库：`{db_path}`\n"
        f"> 知识点 {total} 条，其中已挂规则 {linked} 条；本表覆盖其余 {len(rows)} 条。\n"
        f"> 有人工先例、建议采纳 **{yes}** 条；只有规则名作证据、待定 **{maybe}** 条；"
        f"判定规则不覆盖 **{none}** 条。\n\n"
        "> ⚠️ 这是**候选**不是结论。知识点该配哪条规则是考纲业务判断，脚本只能缩小人工范围，"
        "拍板必须你自己来。特别是「收入 / 成本 / 费用 / 利润」这种章节级知识点，"
        "配上去只是「这个章节下有相关规则」，不等于这个知识点真的能出题。\n\n"
        "## 怎么改\n\n"
        "- `决定` 列：`y` = 落库，`n` = 跳过，`?` = 待定（**默认不落库**）。\n"
        "- `建议规则` 列可以直接增删改，多个规则用英文逗号分隔。\n"
        "- 按**知识点名**匹配落库；库里存在重名（如「所有者权益」「总论」），重名的会一起挂。\n"
        "- 落库前先备份：`sqlite3 <库> \".backup <库>.bak\"`（本脚本不替你备份）。\n"
        "- 改完后落库：\n"
        "  `python scripts/suggest_kp_rules.py --confirmed 本文件.md --apply`\n\n"
        "| 决定 | 知识点 | 科目 | 所属章节 | 建议规则 | 依据 |\n"
        "|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| {r['decide']} | {r['name']} | {r['subject']} | {r['parent']} | "
        f"{r['rules']} | {r['reason']} |\n" for r in rows)
    return head + body + legend()


def legend() -> str:
    """规则编号对照，省得人改表时来回翻 rules/*.yaml。"""
    try:
        rows = rule_rows(None)  # type: ignore[arg-type]
    except Exception:
        return ""
    if not rows:
        return ""
    out = "\n## 规则编号对照\n\n| 编号 | 规则名 |\n|---|---|\n"
    out += "".join(f"| {r['id']} | {r['name']} |\n" for r in rows)
    out += ("\n> 覆盖口径：这 24 条只覆盖**增值税 / 企业所得税勾稽 / 账表勾稽**三类。"
            "经济法里土地增值税、消费税、关税、印花税、契税、房产税、城镇土地使用税、"
            "环境保护税、资源税、车船税等税种章节，以及支付结算、劳动合同、政府会计、"
            "税收征管，本系统目前**没有任何规则**，对应知识点给不出建议是预期结果，"
            "不是脚本漏了。\n")
    return out


def parse_confirmed(path: str) -> list[tuple[str, str]]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| 决定"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        decide, name, _s, _p, rules, _r = cells[:6]
        if decide.lower() == "y" and rules:
            out.append((name, rules))
    return out


def apply_links(db_path: str, confirmed: list[tuple[str, str]]) -> int:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    written = 0
    try:
        db.execute("BEGIN IMMEDIATE")
        for name, rules in confirmed:
            # 库里有重名知识点（如「所有者权益」「总论」各两条，分属不同科目/章节），
            # 这里按名字全写，重名的一起挂，不静默只写第一条。
            rows = db.execute(
                "SELECT id FROM knowledge_point WHERE name=?", (name,)).fetchall()
            if not rows:
                print(f"  跳过（知识点不存在）：{name}")
                continue
            for row in rows:
                for rid in [r.strip() for r in rules.split(",") if r.strip()]:
                    exists = db.execute(
                        "SELECT 1 FROM knowledge_point_link WHERE knowledge_point_id=?"
                        " AND target_type='rule' AND target_id=? AND active=1",
                        (row["id"], rid)).fetchone()
                    if exists:
                        continue
                    db.execute(
                        "INSERT INTO knowledge_point_link (id, knowledge_point_id, target_type,"
                        " target_id, created_by, created_at, active) VALUES (?,?,'rule',?,?,"
                        " datetime('now'),1)",
                        (uuid.uuid4().hex, row["id"], rid, "script:suggest_kp_rules"))
                    written += 1
        db.commit()
    finally:
        db.close()
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", help="写出候选表（Markdown）")
    ap.add_argument("--confirmed", help="人工改过的候选表")
    ap.add_argument("--apply", action="store_true", help="落库，必须与 --confirmed 一起用")
    args = ap.parse_args()

    if args.apply:
        if not args.confirmed:
            print("ERROR: --apply 必须配 --confirmed（先让人改过表再落库）")
            return 2
        rows = parse_confirmed(args.confirmed)
        if not rows:
            print("ERROR: 没有读到任何「决定=y」的行")
            return 2
        n = apply_links(args.db, rows)
        print(f"已写入 {n} 条知识点↔规则关联到 {args.db}")
        return 0

    rows, total, linked = build_rows(args.db)
    text = render(rows, total, linked, args.db)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        yes = sum(1 for r in rows if r["decide"] == "y")
        maybe = sum(1 for r in rows if r["decide"] == "?")
        print(f"候选表已写出：{args.out}")
        print(f"  待处理 {len(rows)} 条：建议采纳 {yes}、待定 {maybe}、判定不覆盖 "
              f"{len(rows) - yes - maybe}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
