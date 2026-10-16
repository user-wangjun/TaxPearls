"""Arrange retained official sources for schema v1, optionally verify in a local candidate DB."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import policy_corpus  # noqa: E402
from src.engine import load_rules  # noqa: E402
from webapp import policy_graph  # noqa: E402


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checked_rule_definitions(manifest):
    """A context edge must still refer to the exact rule we scanned earlier."""
    selected, skipped = [], []
    current = {rule.id: rule for rule in load_rules(ROOT / "rules")}
    for item in manifest.get("rules", []):
        path = (ROOT / item["path"]).resolve()
        if (not path.is_relative_to(ROOT / "rules") or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
                or item["id"] not in current):
            skipped.append(item["id"])
        else:
            selected.append(current[item["id"]])
    return selected, skipped


def import_local(plan, manifest, db_path, originals):
    """Never bootstrap Store or touch enterprise records, services or .env."""
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & {"users", "audits", "clients", "material_batches", "core_tax_reports"}:
            raise ValueError("只接受独立候选知识库；不修改业务数据库。")
        if "Policy" in tables and db.execute("SELECT 1 FROM Policy WHERE validity_status!='unverified' LIMIT 1").fetchone():
            raise ValueError("目标库含已复核政策，不能当作资料整理验收库。")
        policy_graph.migrate(db)
        # migrate runs SQLite DDL before this transaction; all candidate content
        # and YAML snapshots below commit or roll back together.
        rules, skipped = checked_rule_definitions(manifest)
        with db:
            ids = policy_graph.sync_rules(db, rules)
            from src.rule_conversion import compile_rule
            converted={rule.id:compile_rule(rule)['rule_id'] for rule in rules}
            imported = policy_corpus.import_plan(db, plan, originals, rule_ids=converted or {code: key for key, code in ids.items()})
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        counts = {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in policy_graph.IDS}
        keyword_count = db.execute("SELECT count(*) FROM kg_clause_fts").fetchone()[0]
        clause_count = db.execute("SELECT count(*) FROM Clause").fetchone()[0]
        assert keyword_count == clause_count
        assert db.execute("SELECT count(*) FROM Policy WHERE validity_status!='unverified'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM TaxRule WHERE review_status!='candidate'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM Relationship WHERE review_status!='candidate'").fetchone()[0] == 0
        from src.kg_retrieval import keyword_search
        examples = ["纳税义务发生时间", "红字发票", "递延所得税", "工资、薪金所得", "社会保险登记", "未开票"]
        searches = {}
        for topic in examples:
            hits = keyword_search.search(db, topic, keywords=[topic], limit=5)["hits"]
            searches[topic] = [{"clause_id": hit["clause_id"], "policy_id": hit["policy_id"]} for hit in hits]
        return {"database": str(db_path), "counts": counts, "current_plan_counts": imported,
                "parser_version": plan["parser_version"], "fts_clause_count": keyword_count,
                "foreign_keys": "passed", "integrity": "ok", "rule_context_skipped": skipped,
                "keyword_smoke": searches, "external_ai_calls": 0, "formal_approvals": 0}
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "docs/法规原文/来源清单.json")
    parser.add_argument("--document-id", action="append")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/法规原文/图谱整理.json")
    parser.add_argument("--db", type=Path, help="可选：显式指定独立候选库进行真实导入核验")
    parser.add_argument("--originals", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    plan = policy_corpus.prepare(args.manifest, args.document_id)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    # Export source-space IDs. The importer binds rule_code to the content-
    # fingerprinted YAML snapshot only after checking the source file hash.
    clauses = {(c["policy_id"], c["clause_label"]): c["clause_id"] for c in plan["bundle"]["Clause"]}
    plan["rule_context_candidates"] = [
        {"clause_id": clauses[(item["policy_id"], label)], "rule_code": citation["rule_id"],
         "relation_type": "PROVIDES_CONTEXT", "review_status": "candidate"}
        for item in plan["documents"] for citation in item["existing_rule_citations"] for label in citation["article_labels"]]
    result = {"schema_version": 1, "policy_count": len(plan["bundle"]["Policy"]),
              "clause_count": len(plan["bundle"]["Clause"]),
              "rule_context_candidates": len(plan["rule_context_candidates"]),
              "tax_type_counts": dict(Counter(d["tax_type"] for d in plan["documents"])),
              "source_count": len(plan["source_coverage"]), "retained_file_count": len(plan["files"]),
              "all_source_text_covered": all(s["exact_coverage"] for s in plan["source_coverage"]),
              "approval_boundary": plan["boundary"]}
    if args.db:
        result["local_import"] = import_local(plan, manifest, args.db, args.originals or args.db.parent / "originals")
    write_json(args.output, plan)
    if args.report:
        write_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
