"""Lossless source arrangement, candidate-only import and failure atomicity."""
from copy import deepcopy
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src import policy_corpus as corpus
from webapp import policy_graph as pg


class PolicyCorpusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        pg.migrate(self.db)
        self.addCleanup(self.db.close)

    def manifest(self, text, *, key="TEST", classification="增值税", amending=False):
        raw = b"<html>fictional original only for an engineering test</html>"
        raw_path, text_path = self.root / (key + ".html"), self.root / (key + ".txt")
        raw_path.write_bytes(raw)
        text_path.write_bytes(text.encode("utf-8"))
        item = {"id": key, "title": "明确虚构的工程样本", "classification": classification,
                "raw_path": raw_path.name, "text_path": text_path.name,
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "source_url": "https://example.test/fictional", "fetched_at": "2026-10-15T00:00:00+08:00"}
        if amending:
            item["document_kind"] = "amending_decision"
        value = {"documents": [item], "graph_preparation": {"format": corpus.FORMAT, "documents": {key: {"issuer": "虚构测试机关"}}}}
        path = self.root / "manifest.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path, value

    def write(self, value):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_full_character_coverage_and_chapter_parents_keep_exceptions(self):
        text = "沿革及目录\n第一章 总则\n第二章 管理\n第一章 总则\n第一条 事实。\n但有例外时另行核对。\n第二章 管理\n第二条无空格也应识别。\n"
        path, _ = self.manifest(text)
        plan = corpus.prepare(path)
        clauses = plan["bundle"]["Clause"]
        self.assertEqual("".join(c["content"] for c in clauses), text)
        by_label = {c["clause_label"]: c for c in clauses}
        self.assertIn("但有例外", by_label["第一条"]["content"])
        self.assertEqual(by_label["第一条"]["parent_clause_id"], by_label["第一章 总则"]["clause_id"])
        self.assertEqual(by_label["第二条"]["parent_clause_id"], by_label["第二章 管理"]["clause_id"])
        for c in clauses:
            location = json.loads(c["source_location"])
            self.assertEqual(text[location["start_char"]:location["end_char_exclusive"]], c["content"])

    def test_amendments_do_not_become_current_article_nodes(self):
        text = '修订决定\n一、将第一条修改为：\n第一条 这里是引文。\n二、删除第二条。\n'
        path, _ = self.manifest(text, amending=True)
        clauses = corpus.prepare(path)["bundle"]["Clause"]
        self.assertEqual([c["clause_label"] for c in clauses], ["修订决定与版本说明", "一、", "二、"])
        self.assertEqual("".join(c["content"] for c in clauses), text)
        self.assertTrue(all(c["extraction_status"] == "needs_review" for c in clauses))

    def test_first_chapter_and_repeated_sections_have_correct_parent(self):
        text = "第一章 总则\n第一节 范围\n第一条 测试。\n第二章 监督\n第一节 范围\n第二条 测试。\n"
        path, _ = self.manifest(text)
        clauses = corpus.prepare(path)["bundle"]["Clause"]
        labels = {c["clause_label"]: c for c in clauses}
        self.assertEqual(labels["第一章 总则/第一节 范围"]["parent_clause_id"], labels["第一章 总则"]["clause_id"])
        self.assertEqual(labels["第一条"]["parent_clause_id"], labels["第一章 总则/第一节 范围"]["clause_id"])
        self.assertEqual(labels["第二条"]["parent_clause_id"], labels["第二章 监督/第一节 范围"]["clause_id"])
        self.assertEqual("".join(c["content"] for c in clauses), text)

    def test_declared_embedded_asset_must_be_retained(self):
        _, value = self.manifest("第一条 测试。\n")
        value["documents"][0]["embedded_assets"] = [{"source_url": "https://example.test/rates.bmp"}]
        with self.assertRaisesRegex(ValueError, "内嵌资源"):
            corpus.prepare(self.write(value))

    def test_real_rate_image_transcription_matches_both_official_html_tables(self):
        root = Path(__file__).resolve().parents[1] / "docs/法规原文"
        manifest = json.loads((root / "来源清单.json").read_text(encoding="utf-8"))
        by_id = {d["id"]: d for d in manifest["documents"]}
        image = by_id["PIT-LAW-2018"]["attachments"][0]
        self.assertTrue((root / image["path"]).read_bytes().startswith(b"BM"))
        rows = [line.split(" | ") for line in (root / image["text_path"]).read_text(encoding="utf-8").splitlines()
                if line[:1].isdigit()]

        class Tables(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows, self.row, self.cell = [], None, None

            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    self.row = []
                elif tag in {"td", "th"}:
                    self.cell = []

            def handle_data(self, data):
                if self.cell is not None:
                    self.cell.append(data)

            def handle_endtag(self, tag):
                if tag in {"td", "th"} and self.row is not None and self.cell is not None:
                    self.row.append("".join("".join(self.cell).split()))
                    self.cell = None
                elif tag == "tr" and self.row is not None:
                    if self.row and self.row[0].isdigit():
                        self.rows.append(self.row)
                    self.row = None

        source = by_id["PIT-AMENDMENT-2018"]
        parser = Tables()
        parser.feed((root / source["raw_path"]).read_bytes().decode(source["encoding"]))
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows, parser.rows)
        plan = corpus.prepare(root / "来源清单.json", ["PIT-LAW-2018"])
        added = [c for c in plan["bundle"]["Clause"] if c["clause_label"].startswith("附件")]
        self.assertTrue(added)
        self.assertIn("按月换算", added[0]["content"])
        self.assertNotIn("pdf_pages", json.loads(added[0]["source_location"]))

    def test_import_summary_separates_current_plan_from_retained_versions(self):
        from scripts.prepare_policy_corpus import import_local
        path, value = self.manifest("第一条 测试。\n")
        db = self.root / "versions.sqlite"
        original = corpus.prepare(path)
        import_local(original, {}, db, self.root / "retained")
        value["graph_preparation"]["documents"]["TEST"]["issuer"] = "虚构新版本机关"
        updated = corpus.prepare(self.write(value))
        result = import_local(updated, {}, db, self.root / "retained")
        self.assertEqual(result["current_plan_counts"]["Policy"], 1)
        self.assertEqual(result["counts"]["Policy"], 2)
        self.assertEqual(result["counts"]["Clause"], 2)

    def test_version_dates_have_retained_amending_decision_evidence(self):
        root = Path(__file__).resolve().parents[1] / "docs/法规原文"
        manifest = json.loads((root / "来源清单.json").read_text(encoding="utf-8"))
        expected = {"EIT-LAW-2018": "2018-12-29", "TAX-COLLECTION-LAW-2015": "2015-04-24",
                    "TAX-COLLECTION-REG-2016": "2016-02-06", "SOCIAL-INSURANCE-LAW-2018": "2018-12-29"}
        for item in manifest["documents"]:
            if item["id"] not in expected:
                continue
            self.assertEqual(item["version_effective_from"], expected[item["id"]])
            evidence = item["supporting_sources"][0]
            corpus.checked_file(root, evidence["raw_path"], evidence["raw_sha256"])
            _, data = corpus.checked_file(root, evidence["text_path"], evidence["text_sha256"])
            normalized = "".join(data.decode("utf-8").split())
            self.assertIn("自公布之日起施行", normalized)
            self.assertIn(evidence["promulgation_date_in_source"], normalized)

    def test_missing_or_duplicate_article_sequence_is_rejected(self):
        for text in ["第一条 A\n第三条 B\n", "第一条 A\n第一条 B\n"]:
            with self.subTest(text=text):
                path, _ = self.manifest(text)
                with self.assertRaisesRegex(ValueError, "条号"):
                    corpus.prepare(path)

    def test_repeated_announcement_sections_stay_one_unresolved_body(self):
        text = "一、通知\n二、内容\n一、另一文件\n"
        path, _ = self.manifest(text)
        clauses = corpus.prepare(path)["bundle"]["Clause"]
        self.assertEqual(len(clauses), 1)
        self.assertEqual(clauses[0]["extraction_status"], "needs_review")
        self.assertEqual(clauses[0]["content"], text)

    def test_alias_deduplicates_policy_without_losing_source_record(self):
        path, value = self.manifest("第一条 测试。\n")
        alias = {**value["documents"][0], "id": "ALIAS", "same_document_as": "TEST"}
        value["documents"].append(alias)
        plan = corpus.prepare(self.write(value))
        self.assertEqual(len(plan["bundle"]["Policy"]), 1)
        self.assertEqual(plan["source_aliases"][0]["document_id"], "ALIAS")
        self.assertEqual(corpus.prepare(path, ["ALIAS"])["bundle"], corpus.prepare(path, ["TEST"])["bundle"])

    def test_classification_unknown_dates_and_exclusive_end_are_preserved(self):
        _, value = self.manifest("第一条 测试。\n", classification="会计规范")
        value["documents"][0]["version_effective_to_exclusive"] = "2026-01-01"
        policy = corpus.prepare(self.write(value))["bundle"]["Policy"][0]
        self.assertEqual(policy["tax_type"], "ACCOUNTING")
        self.assertEqual(policy["effective_to"], "2025-12-31")
        self.assertIsNone(policy["publish_date"])
        self.assertIsNone(policy["effective_from"])
        self.assertEqual(policy["validity_status"], "unverified")

    def test_source_hash_path_escape_and_unknown_ids_fail_before_import(self):
        path, value = self.manifest("第一条 测试。\n")
        for update in [{"raw_path": "../outside.html"}, {"text_sha256": "0" * 64}]:
            changed = deepcopy(value)
            changed["documents"][0].update(update)
            with self.assertRaises(ValueError):
                corpus.prepare(self.write(changed))
        self.write(value)
        for selected in [[], ["UNKNOWN"], ["TEST", "TEST"]]:
            with self.assertRaises(ValueError):
                corpus.prepare(path, selected)

    def test_attachment_text_is_lossless_and_does_not_become_approved_conditions(self):
        _, value = self.manifest("第一条 测试。\n")
        raw, text = b"%PDF-fictional", "【PDF第1页】\n一、定义\n例外情况\n【PDF第2页】\n二、范围\n"
        (self.root / "a.pdf").write_bytes(raw)
        (self.root / "a.txt").write_bytes(text.encode("utf-8"))
        value["documents"][0]["attachments"] = [{"path": "a.pdf", "sha256": corpus.sha(raw),
            "text_path": "a.txt", "text_sha256": corpus.sha(text.encode())}]
        plan = corpus.prepare(self.write(value))
        attached = [c for c in plan["bundle"]["Clause"] if c["clause_label"].startswith("附件1/")]
        self.assertEqual("".join(c["content"] for c in attached), text)
        self.assertTrue(all(c["extraction_status"] == "needs_review" for c in attached))
        self.assertIn("pdf_pages", json.loads(attached[-1]["source_location"]))
        self.assertNotIn("Condition", plan["bundle"])

    def test_subset_and_full_import_keep_stable_ids_order_and_retry(self):
        path, value = self.manifest("第一条 测试。\n")
        value["documents"].append({**value["documents"][0], "id": "SECOND"})
        path = self.write(value)
        full, subset = corpus.prepare(path), corpus.prepare(path, ["SECOND"])
        second = subset["bundle"]["Clause"][0]
        self.assertEqual(second, full["bundle"]["Clause"][1])
        corpus.import_plan(self.db, subset, self.root / "retained")
        corpus.import_plan(self.db, full, self.root / "retained")
        corpus.import_plan(self.db, full, self.root / "retained")
        self.assertEqual(self.db.execute("SELECT count(*) FROM Clause").fetchone()[0], 2)

    def test_late_batch_failure_rolls_back_nodes_and_only_new_retained_files(self):
        path, _ = self.manifest("第一条 测试。\n第二条 测试。\n")
        plan = corpus.prepare(path)
        retained = self.root / "retained"
        retained.mkdir()
        existing = retained / "user-file.txt"
        existing.write_text("preserve", encoding="utf-8")
        original = pg.import_bundle
        def fail_clause(db, rows):
            if "Clause" in rows:
                raise ValueError("模拟后续批次失败")
            return original(db, rows)
        with patch.object(pg, "import_bundle", side_effect=fail_clause):
            with self.assertRaisesRegex(ValueError, "后续批次"):
                corpus.import_plan(self.db, plan, retained)
        self.assertEqual(self.db.execute("SELECT count(*) FROM Policy").fetchone()[0], 0)
        self.assertEqual(list(retained.iterdir()), [existing])
        self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")

    def test_existing_corpus_entrypoint_uses_prepared_schema_and_status(self):
        path, _ = self.manifest("第一条 测试。\n第二条 测试。\n")
        result = pg.import_corpus(self.db, path, ["TEST"], self.root / "retained")
        self.assertEqual(result["Policy"], 1)
        self.assertEqual(result["Clause"], 2)
        policy = dict(self.db.execute("SELECT * FROM Policy").fetchone())
        self.assertEqual(policy["tax_type"], "VAT")
        self.assertEqual(policy["validity_status"], "unverified")
        self.assertEqual(self.db.execute("SELECT count(*) FROM TaxRule").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM kg_clause_fts").fetchone()[0], 2)

    def test_extraction_or_metadata_change_creates_distinct_version(self):
        path, value = self.manifest("第一条 测试。\r\n")
        before = corpus.prepare(path)["bundle"]["Policy"][0]["policy_id"]
        value["graph_preparation"]["documents"]["TEST"]["issuer"] = "另一个虚构测试机关"
        after = corpus.prepare(self.write(value))["bundle"]["Policy"][0]["policy_id"]
        self.assertNotEqual(before, after)
        plan = corpus.prepare(path)
        (self.root / "TEST.txt").write_bytes(b"changed after preparing")
        with self.assertRaisesRegex(ValueError, "哈希"):
            corpus.import_plan(self.db, plan, self.root / "retained")
        self.assertEqual(self.db.execute("SELECT count(*) FROM Policy").fetchone()[0], 0)

    def test_real_corpus_keeps_full_sources_contexts_and_uncertain_dates(self):
        root = Path(__file__).resolve().parents[1] / "docs/法规原文"
        plan = corpus.prepare(root / "来源清单.json")
        clauses = {c["clause_id"]: c for c in plan["bundle"]["Clause"]}
        for source in plan["source_coverage"]:
            text = (root / source["text_path"]).read_bytes().decode("utf-8-sig")
            actual = "".join(clauses[cid]["content"] for cid in source["clause_ids"])
            self.assertEqual(actual, text, source["text_path"])
        policies = {s["document_id"]: s["policy_id"] for s in plan["documents"]}
        for doc, label, excerpt in [("VAT-LAW-2024", "第三十八条", "同时废止"),
                                   ("INVOICE-DETAILS-2024", "第二十七条", "红字发票"),
                                   ("SOCIAL-INSURANCE-LAW-2018", "第五十八条", "社会保险登记")]:
            self.assertIn(excerpt, clauses[policies[doc] + ":" + label]["content"])
        edge = plan["bundle"]["Relationship"][0]
        self.assertEqual((edge["relation_type"], edge["review_status"]), ("REPEALS", "candidate"))
        self.assertEqual(edge["evidence_clause_id"], policies["VAT-LAW-2024"] + ":第三十八条")
        self.assertEqual(len(plan["source_aliases"]), 1)
        self.assertTrue(all(p["validity_status"] == "unverified" for p in plan["bundle"]["Policy"]))
        self.assertFalse(any(table in plan["bundle"] for table in ["TaxRule", "Condition", "Exception"]))


if __name__ == "__main__":
    unittest.main()
