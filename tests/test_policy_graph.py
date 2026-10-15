"""Policy schema, review gates and three-valued preview. All thresholds here are fictional."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from src import engine, tax_conditions as conditions
from webapp import app as app_module, policy_graph as pg
from webapp.storage import Store
from webapp.knowledge import extract_policy_candidates


ROOT = Path(__file__).resolve().parents[1]


def demonstration(original):
    """Fictional internal screen; the source provides context, never a statutory threshold."""
    return {
        "Policy": [{"policy_id": "P1", "title": "虚构工程测试背景", "issuer": "测试数据",
                    "source_url": "https://example.test/fictional", "effective_from": "2026-01-01",
                    "validity_status": "in_force", "raw_file_path": str(original),
                    "file_sha256": hashlib.sha256(original.read_bytes()).hexdigest()}],
        "Clause": [{"clause_id": "CL1", "policy_id": "P1", "clause_label": "示例第三条", "sort_index": 3,
                    "content": "仅用于技术测试，不是税法。", "source_location": "原件第1行", "extraction_status": "verified"}],
        "TaxRule": [{"rule_id": "T1", "rule_code": "TEST-1", "name": "虚构销售金额筛查",
                     "rule_type": "risk_screen", "valid_from": "2026-01-01",
                     "condition_logic_json": {"and": [{"ref": "C1"}]}, "outcome_text": "金额待核查"}],
        "Entity": [{"entity_id": "E1", "entity_type": "taxpayer_type", "name": "演示纳税人类型"}],
        "Metric": [{"metric_id": "M1", "metric_code": "test_sales_v1", "logical_code": "test_sales",
                    "name": "演示销售额", "value_type": "decimal", "unit": "元", "source_schema": "VATDeclaration",
                    "source_field": "declared_value", "source_filter_json": {
                        "form_type": "main", "form_name": "演示申报主表", "item_key": "1", "column_scope": "本期数"},
                    "period_basis": "tax_period"}],
        "Condition": [{"condition_id": cid, "rule_id": "T1", "left_metric_id": "M1", "operator": ">",
                       "right_kind": "literal", "right_literal": value, "unit": "元", "condition_source": "internal_screening"}
                      for cid, value in [("C1", "100000"), ("C2", "999999999")]],
        "Exception": [{"exception_id": "X1", "rule_id": "T1", "name": "超出样例范围转复核",
                       "trigger_logic_json": {"ref": "C2"}, "effect": "needs_review"}],
        "Relationship": [
            {"relationship_id": "L1", "from_type": "Clause", "from_id": "CL1", "relation_type": "PROVIDES_CONTEXT",
             "to_type": "TaxRule", "to_id": "T1", "evidence_clause_id": "CL1"},
            {"relationship_id": "L2", "from_type": "TaxRule", "from_id": "T1", "relation_type": "APPLIES_TO",
             "to_type": "Entity", "to_id": "E1"},
        ],
    }


class PolicyGraphTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.original = Path(self.directory.name) / "fictional.txt"
        self.original.write_text("仅用于技术测试，不是税法。", encoding="utf-8")
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        pg.migrate(self.db)
        self.bundle = demonstration(self.original)

    def install(self, approve=False):
        pg.import_bundle(self.db, self.bundle)
        if approve:
            pg.approve_rule(self.db, "T1", "human-reviewer")

    def preview(self, value="100001", **overrides):
        args = {"start": "2026-02-01", "end": "2026-02-28", "region": "CN", "entities": ["E1"]}
        args.update(overrides)
        facts = {} if value is None else {"M1": {"state": "known", "value": value, "unit": "元"}}
        return pg.evaluate_rule(self.db, "T1", facts, **args)

    def test_additive_migration_from_supplied_sql_preserves_existing_data(self):
        other = sqlite3.connect(":memory:")
        other.row_factory = sqlite3.Row
        self.addCleanup(other.close)
        other.executescript((ROOT / "webapp/policy_schema.sql").read_text(encoding="utf-8-sig"))
        other.execute("CREATE TABLE sentinel (value TEXT)")
        other.execute("INSERT INTO sentinel VALUES ('keep')")
        other.execute("INSERT INTO TaxRule (rule_id,name,rule_type,outcome_text) VALUES ('old','old','risk_screen','old')")
        pg.migrate(other)
        pg.migrate(other)
        self.assertEqual(pg.get(other, "TaxRule", "old")["version"], "1")
        self.assertEqual(other.execute("SELECT value FROM sentinel").fetchone()[0], "keep")
        self.assertEqual(len(pg.IDS), 8)

    def test_graph_contains_seven_node_types_and_typed_relationships(self):
        self.install()
        graph = pg.graph(self.db)
        self.assertEqual({n["schema_type"] for n in graph["nodes"]}, set(pg.KINDS))
        ids = {n["id"] for n in graph["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in graph["edges"]))
        self.assertTrue(any(e.get("schema_type") == "Relationship" for e in graph["edges"]))
        self.assertFalse(any(n["kind"] in {"company", "risk"} for n in graph["nodes"]))

    def test_candidate_cannot_execute_and_approval_records_reviewer(self):
        self.install()
        self.assertEqual(self.preview()["truth"], "UNKNOWN")
        approved = pg.approve_rule(self.db, "T1", "human-reviewer")
        self.assertEqual(approved["reviewed_by"], "human-reviewer")
        self.assertTrue(approved["reviewed_at"])
        self.assertEqual(self.preview()["truth"], "TRUE")

    def test_three_valued_decimal_boundaries_and_exceptions(self):
        self.install(approve=True)
        for value, expected in [("100000", "FALSE"), ("100000.000000000000000001", "TRUE"),
                                ("99999.9", "FALSE"), (None, "UNKNOWN"), ("NaN", "UNKNOWN"),
                                ("not-number", "UNKNOWN"), ("1000000000", "UNKNOWN")]:
            with self.subTest(value=value):
                self.assertEqual(self.preview(value)["truth"], expected)

    def test_unknown_exception_does_not_pass_and_exclusion_is_distinct(self):
        self.bundle["Metric"].append({"metric_id": "M2", "metric_code": "exception_flag", "name": "例外标记",
                                      "value_type": "boolean", "source_schema": "VATDeclaration", "source_field": "item_label",
                                      "period_basis": "tax_period", "source_filter_json": self.bundle["Metric"][0]["source_filter_json"]})
        self.bundle["Condition"][1].update(left_metric_id="M2", unit=None, operator="=", right_literal="true")
        self.install(approve=True)
        self.assertEqual(self.preview()["truth"], "UNKNOWN")
        # The exclusion is a separate disposition; it does not fabricate a FALSE predicate.
        self.db.execute("UPDATE Exception SET effect='exclude' WHERE exception_id='X1'")
        facts = {"M1": {"state": "known", "value": "200000", "unit": "元"},
                 "M2": {"state": "known", "value": True, "unit": None}}
        result = pg.evaluate_rule(self.db, "T1", facts, start="2026-02-01", end="2026-02-28", region="CN", entities=["E1"])
        self.assertEqual((result["truth"], result["disposition"]), ("TRUE", "excluded"))

    def test_time_region_subject_and_source_review_gates(self):
        self.install(approve=True)
        for args in [{"start": "2025-12-01"}, {"entities": []}]:
            self.assertEqual(self.preview(**args)["truth"], "UNKNOWN")
        self.db.execute("UPDATE TaxRule SET applicable_region='CN-33' WHERE rule_id='T1'")
        self.assertEqual(self.preview()["truth"], "UNKNOWN")
        self.db.execute("UPDATE TaxRule SET applicable_region='CN' WHERE rule_id='T1'")
        self.db.execute("UPDATE Policy SET validity_status='repealed' WHERE policy_id='P1'")
        self.assertEqual(self.preview()["truth"], "UNKNOWN")
        self.db.execute("UPDATE Policy SET effective_to='2026-03-31' WHERE policy_id='P1'")
        self.assertEqual(self.preview()["truth"], "TRUE")

    def test_import_is_atomic_for_dangling_relationship_and_bad_logic(self):
        for mutate in [lambda b: b["Relationship"][0].update(to_id="missing"),
                       lambda b: b["TaxRule"][0].update(condition_logic_json={"ref": "foreign-condition"}),
                       lambda b: b["Condition"][0].update(right_literal="garbage"),
                       lambda b: b["Relationship"][0].update(relation_type="invented")]:
            bundle = deepcopy(self.bundle)
            mutate(bundle)
            with self.assertRaises(ValueError):
                pg.import_bundle(self.db, bundle)
            self.assertEqual(self.db.execute("SELECT count(*) FROM Policy").fetchone()[0], 0)

    def test_empty_and_foreign_rule_logic_rejected(self):
        for logic in [{}, {"and": []}, {"or": []}, {"not": []}, {"ref": "C3"}, {"and": [{"ref": "C1"}], "or": []}]:
            bundle = deepcopy(self.bundle)
            bundle["TaxRule"][0]["condition_logic_json"] = logic
            with self.assertRaises(ValueError):
                pg.import_bundle(self.db, bundle)

    def test_import_never_accepts_approval_and_content_cannot_be_overwritten(self):
        for table in ["TaxRule", "Relationship"]:
            bundle = deepcopy(self.bundle)
            bundle[table][0]["review_status"] = "approved"
            with self.assertRaises(ValueError):
                pg.import_bundle(self.db, bundle)
        self.install()
        with self.assertRaises(ValueError):
            pg.import_bundle(self.db, {"Metric": [{**self.bundle["Metric"][0], "unit": "万元"}]})
        self.assertEqual(pg.get(self.db, "Metric", "M1")["unit"], "元")
        pg.approve_rule(self.db, "T1", "human")
        with self.assertRaises(ValueError):
            pg.import_bundle(self.db, {"Condition": [{**self.bundle["Condition"][0], "condition_id": "extra"}]})

    def test_approval_blocks_pending_mapping_source_or_forged_derivation(self):
        variants = [lambda b: b["Condition"][0].update(condition_source="pending"),
                    lambda b: b["Metric"][0].update(source_schema=None),
                    lambda b: b["Clause"][0].update(extraction_status="parsed"),
                    lambda b: b["Policy"][0].update(validity_status="unverified"),
                    lambda b: b["Relationship"][0].update(relation_type="DERIVES_RULE"),
                    lambda b: b["Metric"][0].update(calculation_code="__import__('os').system('echo bad')")]
        for mutate in variants:
            with self.subTest(mutate=mutate):
                db = sqlite3.connect(":memory:")
                db.row_factory = sqlite3.Row
                try:
                    pg.migrate(db)
                    bundle = deepcopy(self.bundle)
                    mutate(bundle)
                    pg.import_bundle(db, bundle)
                    with self.assertRaises(ValueError):
                        pg.approve_rule(db, "T1", "human")
                    self.assertEqual(pg.get(db, "TaxRule", "T1")["review_status"], "candidate")
                finally:
                    db.close()

    def test_statutory_condition_needs_own_source_and_verified_original(self):
        self.bundle["TaxRule"][0]["rule_type"] = "policy_rule"
        self.bundle["Relationship"][0]["relation_type"] = "DERIVES_RULE"
        for item in self.bundle["Condition"]:
            item.update(condition_source="statutory", source_clause_id="CL1")
        self.install()
        self.original.write_text("原件已经改变", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "哈希"):
            pg.approve_rule(self.db, "T1", "human")

    def test_snapshot_is_frozen_and_same_version_legacy_edits_get_distinct_records(self):
        self.install(approve=True)
        old = self.preview()
        frozen = pg.canonical(old["snapshot"])
        self.bundle["Metric"][0].update(metric_id="M2", metric_code="test_sales_v2", version="2", supersedes_id="M1")
        pg.import_bundle(self.db, {"Metric": self.bundle["Metric"]})
        self.assertEqual(pg.canonical(old["snapshot"]), frozen)
        self.assertEqual(old["snapshot_hash"], pg.fingerprint(old["snapshot"]))
        base = engine.load_rules(ROOT / "rules")[0]
        first = pg.sync_rules(self.db, [base])
        second = pg.sync_rules(self.db, [replace(base, suggestion="新说明，同版本号")])
        self.assertNotEqual(set(first), set(second))
        self.assertEqual(json.loads(pg.get(self.db, "TaxRule", next(iter(first)))["execution_json"])["suggestion"], base.suggestion)
        self.assertEqual(pg.graph(self.db, second)["nodes"][-1].get("review_status", "candidate"), "candidate")
        self.assertTrue(any(n["id"] == base.id for n in pg.graph(self.db, second)["nodes"]))
        with self.assertRaises(ValueError):
            pg.approve_rule(self.db, next(iter(second)), "human")

    def test_all_and_or_not_truth_tables(self):
        for a in ["TRUE", "FALSE", "UNKNOWN"]:
            for b in ["TRUE", "FALSE", "UNKNOWN"]:
                values = {"a": a, "b": b}
                expected_and = "FALSE" if "FALSE" in values.values() else "UNKNOWN" if "UNKNOWN" in values.values() else "TRUE"
                expected_or = "TRUE" if "TRUE" in values.values() else "UNKNOWN" if "UNKNOWN" in values.values() else "FALSE"
                self.assertEqual(conditions.logic_value({"and": [{"ref": "a"}, {"ref": "b"}]}, values), expected_and)
                self.assertEqual(conditions.logic_value({"or": [{"ref": "a"}, {"ref": "b"}]}, values), expected_or)
            self.assertEqual(conditions.logic_value({"not": {"ref": "a"}}, {"a": a}),
                             {"TRUE": "FALSE", "FALSE": "TRUE", "UNKNOWN": "UNKNOWN"}[a])

    def test_in_membership_and_unit_mismatch(self):
        definitions = {"m": {"value_type": "text", "unit": None}}
        item = {"left_metric_id": "m", "right_kind": "literal", "right_literal": '["A","B"]', "unit": None, "operator": "IN"}
        self.assertEqual(conditions.compare(item, definitions, {"m": {"state": "known", "value": "A", "unit": None}}), "TRUE")
        item["operator"] = "NOT IN"
        self.assertEqual(conditions.compare(item, definitions, {"m": {"state": "known", "value": "A", "unit": None}}), "FALSE")
        self.assertEqual(conditions.compare(item, definitions, {"m": {"state": "known", "value": "A", "unit": "元"}}), "UNKNOWN")

    def test_existing_core_record_mapping_and_preview_connect(self):
        self.install(approve=True)
        metric = pg.get(self.db, "Metric", "M1")
        task = {"tenant_id": "org-a", "company_id": "company-a", "id": "task-a",
                "start_date": "2026-02-01", "end_date": "2026-02-28"}
        record = {**task, "task_id": "task-a", "id": "record-a", "material_type": "vat_declaration",
                  "form_type": "main", "form_name": "演示申报主表", "item_key": "1", "column_scope": "本期数",
                  "period_start": task["start_date"], "period_end": task["end_date"], "in_scope": True,
                  "declared_value": "200000.01", "value_states": {"declared_value": "present"}, "unit": "元"}
        processing = {"status": "ready_for_analysis", "issues": [], "records": [record]}
        fact = conditions.resolve_metric(metric, processing, task)
        self.assertEqual((fact["state"], fact["value"], fact["source_record_ids"]), ("known", "200000.01", ["record-a"]))
        result = pg.evaluate_rule(self.db, "T1", {"M1": fact}, start=task["start_date"], end=task["end_date"], region="CN", entities=["E1"])
        self.assertEqual(result["truth"], "TRUE")
        for changes in [{"tenant_id": "org-b"}, {"unit": "万元"}, {"period_start": "2026-01-01"},
                        {"column_scope": "累计数"}, {"declared_value": None}, {"form_name": None}]:
            data = {**processing, "records": [{**record, **changes}]}
            self.assertEqual(conditions.resolve_metric(metric, data, task)["state"], "unknown")
        self.assertEqual(conditions.resolve_metric(metric, {**processing, "records": []}, task)["state"], "unknown")
        self.assertEqual(conditions.resolve_metric(metric, {**processing, "records": [record, record]}, task)["state"], "unknown")

    def test_corpus_import_retains_original_and_stays_unverified(self):
        root = Path(self.directory.name)
        raw = b"<p>fictional source</p>"
        text = "第一条 测试原文。\n第二条 测试例外。\n".encode()
        (root / "source.html").write_bytes(raw)
        (root / "source.txt").write_bytes(text)
        manifest = {"documents": [{"id": "TEST", "title": "虚构来源", "source_url": "https://example.test/source",
                                  "raw_path": "source.html", "text_path": "source.txt", "raw_sha256": hashlib.sha256(raw).hexdigest(),
                                  "text_sha256": hashlib.sha256(text).hexdigest()}]}
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = pg.import_corpus(self.db, path, ["TEST"], root / "retained")
        self.assertEqual(result, {"Policy": 1, "Clause": 2})
        policy = dict(self.db.execute("SELECT * FROM Policy").fetchone())
        self.assertEqual(Path(policy["raw_file_path"]).read_bytes(), raw)
        self.assertEqual(policy["validity_status"], "unverified")
        self.assertEqual(self.db.execute("SELECT count(*) FROM TaxRule").fetchone()[0], 0)
        pg.import_corpus(self.db, path, ["TEST"], root / "retained")
        reviewed = pg.verify_source(self.db, policy["policy_id"], issuer="纯合成测试机关", validity_status="in_force",
                                    effective_from="2026-01-01", effective_to=None,
                                    clause_ids=[policy["policy_id"] + ":第一条"])
        self.assertEqual(reviewed["validity_status"], "in_force")
        self.assertEqual(pg.get(self.db, "Clause", policy["policy_id"] + ":第一条")["extraction_status"], "verified")
        self.assertEqual(pg.get(self.db, "Clause", policy["policy_id"] + ":第二条")["extraction_status"], "parsed")
        with self.assertRaises(ValueError):
            pg.verify_source(self.db, policy["policy_id"], issuer="纯合成测试机关", validity_status="repealed",
                             effective_from="2026-01-01", effective_to="2026-12-31",
                             clause_ids=[policy["policy_id"] + ":第一条"])
        (root / "source.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "哈希"):
            pg.import_corpus(self.db, path, ["TEST"], root / "retained")

    def test_real_xlsx_parser_outputs_resolve_three_material_metrics(self):
        from tests.test_core_inputs import book, mapper, TAX
        from src import core_materials
        db, field_mapper = mapper()
        self.addCleanup(db.close)
        task = {"id": "task", "company_id": "company", "tenant_id": "org", "tax_no": TAX,
                "start_date": "2026-01-01", "end_date": "2026-01-31", "applicable_forms": []}
        documents = [core_materials.parse(book(kind, **kwargs), "fixture.xlsx", kind, str(i), field_mapper)
                     for i, (kind, kwargs) in enumerate([
                         ("account_balance", {}), ("vat_declaration", {}),
                         ("invoice_detail", {}), ("invoice_detail", {"direction": "进项"})])]
        processing = core_materials.prepare(task, documents)
        self.assertEqual(processing["status"], "ready_for_analysis")
        vat = {**self.bundle["Metric"][0], "source_filter_json": pg.canonical({
            "form_type": "main", "form_name": "增值税纳税申报表主表", "item_key": "1", "column_scope": "本期数"})}
        invoice_row = next(r for r in processing["records"] if r["material_type"] == "invoice_detail")
        invoice = {**vat, "source_schema": "Invoice", "source_field": "amount_excluding_tax", "aggregation": "sum",
                   "period_basis": "issue_date", "source_filter_json": pg.canonical({
                       "direction": "output", "red_letter_status": invoice_row["red_letter_status"],
                       "void_status": invoice_row["void_status"]})}
        balance = {**vat, "source_schema": "AccountBalanceRow", "source_field": "current_debit",
                   "period_basis": "accounting_period", "source_filter_json": '{"account_code":"1001"}'}
        for metric in (vat, invoice, balance):
            fact = conditions.resolve_metric(metric, processing, task)
            self.assertEqual(fact["state"], "known", fact)
            self.assertEqual(fact["value"], "100.0123")
        count = {**invoice, "source_field": "invoice_no", "unit": "张", "aggregation": "count"}
        self.assertEqual(conditions.resolve_metric(count, processing, task)["value"], 1)

    def test_ai_candidates_keep_sources_and_require_manual_review(self):
        self.install()
        bundle = {
            "TaxRule": [{"rule_id": "AI-T1", "name": "纯合成AI候选", "rule_type": "policy_rule",
                         "condition_logic_json": {"ref": "AI-C1"}, "outcome_text": "待核实"}],
            "Metric": [{"metric_id": "AI-M1", "metric_code": "ai-pending", "name": "待配置指标",
                        "value_type": "decimal", "unit": "元"}],
            "Condition": [{"condition_id": "AI-C1", "rule_id": "AI-T1", "left_metric_id": "AI-M1",
                           "operator": ">", "right_kind": "literal", "right_literal": "100000", "unit": "元",
                           "condition_source": "statutory", "source_clause_id": "CL1"}],
        }
        with patch("webapp.knowledge._chat_json", return_value=({"bundle": bundle}, "mocked-model")):
            candidate, model = extract_policy_candidates(pg.get(self.db, "Policy", "P1"), pg.get(self.db, "Clause", "CL1"),
                                                          {key: [] for key in pg.IDS})
        pg.import_bundle(self.db, candidate)
        self.assertEqual(model, "mocked-model")
        self.assertEqual(pg.get(self.db, "TaxRule", "AI-T1")["review_status"], "candidate")
        with self.assertRaises(ValueError):
            pg.approve_rule(self.db, "AI-T1", "human")

    def test_graph_does_not_duplicate_declared_structural_edges(self):
        self.bundle["Relationship"].append({"relationship_id": "L3", "from_type": "Policy", "from_id": "P1",
                                              "relation_type": "HAS_CLAUSE", "to_type": "Clause", "to_id": "CL1"})
        self.install()
        links = [e for e in pg.graph(self.db)["edges"] if e.get("relation_type") == "HAS_CLAUSE"]
        self.assertEqual(len(links), 1)


class PolicyGraphHTTPTests(unittest.TestCase):
    def test_import_and_review_roll_back_if_audit_log_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "fictional.txt"
            original.write_text("纯合成测试。", encoding="utf-8")
            store = Store(Path(directory) / "test.db")
            store.create_user("platform", "policy-test-2026", "平台", "platform_admin", "platform")
            with patch.object(app_module, "store", store), TestClient(app_module.app) as client:
                client.post("/api/login", json={"username": "platform", "password": "policy-test-2026"})
                with patch.object(store, "_log", side_effect=RuntimeError("test log failure")):
                    with self.assertRaises(RuntimeError):
                        client.post("/api/knowledge/candidates", json={"bundle": demonstration(original)})
                with store.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM TaxRule").fetchone()[0], 0)
                self.assertEqual(client.post("/api/knowledge/candidates", json={"bundle": demonstration(original)}).status_code, 200)
                with patch.object(store, "_log", side_effect=RuntimeError("test log failure")):
                    with self.assertRaises(RuntimeError):
                        client.post("/api/knowledge/rules/T1/approve")
                with store.connect() as db:
                    self.assertEqual(pg.get(db, "TaxRule", "T1")["review_status"], "candidate")

    def test_schema_permissions_candidate_approval_preview_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "fictional.txt"
            original.write_text("仅用于技术测试。", encoding="utf-8")
            path = Path(directory) / "test.db"
            store = Store(path)
            store.create_user("platform", "policy-test-2026", "平台", "platform_admin", "platform")
            store.create_user("accountant", "policy-test-2026", "会计", "accountant", "org-a")
            with patch.object(app_module, "store", store), TestClient(app_module.app) as client:
                self.assertEqual(client.get("/api/knowledge/schema").status_code, 401)
                client.post("/api/login", json={"username": "accountant", "password": "policy-test-2026"})
                self.assertEqual(client.post("/api/knowledge/candidates", json={"bundle": demonstration(original)}).status_code, 403)
                self.assertEqual(len(client.get("/api/knowledge/schema").json()["types"]), 8)
                graph = client.get("/api/knowledge/graph").json()
                self.assertTrue(any(n["id"] == "R-001" and n["schema_type"] == "TaxRule" for n in graph["nodes"]))
                client.post("/api/logout")
                client.post("/api/login", json={"username": "platform", "password": "policy-test-2026"})
                bundle = demonstration(original)
                bundle["Policy"][0]["validity_status"] = "unverified"
                bundle["Clause"][0]["extraction_status"] = "parsed"
                imported = client.post("/api/knowledge/candidates", json={"bundle": bundle})
                self.assertEqual(imported.status_code, 200, imported.text)
                body = {"facts": {"M1": {"state": "known", "value": "200000", "unit": "元"}},
                        "start": "2026-02-01", "end": "2026-02-28", "region": "CN", "entities": ["E1"]}
                endpoint = "/api/knowledge/rules/T1/preview"
                self.assertEqual(client.post(endpoint, json=body).json()["truth"], "UNKNOWN")
                self.assertEqual(client.post("/api/knowledge/rules/T1/approve").status_code, 422)
                source_review = client.post("/api/knowledge/policies/P1/verify", json={
                    "issuer": "测试数据", "validity_status": "in_force", "effective_from": "2026-01-01", "clause_ids": ["CL1"]})
                self.assertEqual(source_review.status_code, 200, source_review.text)
                approved = client.post("/api/knowledge/rules/T1/approve")
                self.assertEqual(approved.status_code, 200, approved.text)
                result = client.post(endpoint, json=body)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["truth"], "TRUE")
                self.assertFalse(result.json()["formal_finding"])
                with patch("webapp.app.extract_policy_candidates", return_value=({"TaxRule": [{
                    "rule_id": "ai-test", "name": "AI候选测试", "rule_type": "risk_screen",
                    "condition_logic_json": {"ref": "missing"}, "outcome_text": "候选"}]}, "test-model")):
                    rejected = client.post("/api/knowledge/clauses/CL1/extract")
                    self.assertEqual(rejected.status_code, 422)
                with store.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM TaxRule WHERE rule_id='ai-test'").fetchone()[0], 0)
                reopened = Store(path)
                with reopened.connect() as db:
                    self.assertEqual(pg.get(db, "TaxRule", "T1")["review_status"], "approved")


if __name__ == "__main__":
    unittest.main()
