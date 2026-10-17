"""The extraction export contains facts and source states, not processing feedback."""
from copy import deepcopy
import unittest

from src import material_export


class MaterialExportTests(unittest.TestCase):
    def payload(self):
        return {
            "task": {"company_name": "测试企业", "tax_no": "001234567890", "start_date": "2026-01-01",
                     "end_date": "2026-01-31", "scope_confirmed": True, "tenant_id": "private"},
            "documents": [{"id": "pdf", "name": "发票.pdf", "sha256": "hash", "material_type": "invoice_detail",
                           "pdf_candidate": True, "source_reviewed": False}],
            "processing": {"status": "needs_completion", "records": [{
                "id": "r1", "material_type": "invoice_detail", "invoice_no": "00123456789012345678",
                "amount_excluding_tax": "100.012300", "tax_amount": None, "unit": "元",
                "issue_date": "2026-01-03", "red_letter_status": "蓝字", "void_status": "正常",
                "source_file_id": "pdf", "source_position": "PDF第3页",
                "value_states": {"tax_amount": "missing", "amount_excluding_tax": "present"},
                "field_sources": {"amount_excluding_tax": {"position": "PDF第3页/金额", "raw_value": "100.012300",
                                                          "explanation": "internal", "origin": "builtin"}},
                "requires_source_review": True, "in_scope": True, "raw_record": {"秘密": "完整原文"},
                "tenant_id": "private", "source_metadata": {"unit": "元"}, "unexpected": "internal"}],
                "issues": [{"code": "pdf_source_review", "message": "发票提取须核对票号、购销方、金额及状态。",
                            "source_file_id": "pdf", "source_position": "PDF第3页"},
                           {"code": "pdf_source_review", "message": "发票提取须核对票号、购销方、金额及状态。",
                            "source_file_id": "pdf", "source_position": "PDF第4页"},
                           {"code": "invoice_status_missing", "message": "票面未证明状态。",
                            "source_file_id": "pdf", "source_position": "PDF第3页"}], "notices": []},
            "events": ["internal"], "versions": ["internal"], "source_options": {"internal": True}}

    def test_facts_and_trace_are_allowlisted_without_mutating_task(self):
        payload = self.payload()
        before = deepcopy(payload)
        result = material_export.build(payload)
        self.assertEqual(result["schema_version"], "taxpearls.material-data.v1")
        row = result["records"][0]
        self.assertEqual(row["fields"]["invoice_no"], "00123456789012345678")
        self.assertEqual(row["fields"]["amount_excluding_tax"], "100.012300")
        self.assertIsNone(row["fields"]["tax_amount"])
        self.assertEqual(row["context"]["unit"], "元")
        self.assertEqual(row["source"]["fields"]["amount_excluding_tax"],
                         {"position": "PDF第3页/金额", "raw_value": "100.012300", "origin": "builtin"})
        self.assertEqual(row["quality"]["field_states"]["tax_amount"], "missing")
        self.assertTrue(row["quality"]["requires_source_review"])
        for key in ["raw_record", "tenant_id", "source_metadata", "unexpected"]:
            self.assertNotIn(key, row)
            self.assertNotIn(key, row["fields"])
        self.assertEqual(set(result), {"schema_version", "task", "records", "sources"})
        self.assertNotIn("tenant_id", result["task"])
        self.assertEqual(payload, before)

    def test_processing_feedback_is_absent_instead_of_renamed(self):
        payload = self.payload()
        payload["processing"]["issues"].append({"code": "invoice_coverage_gap", "message": "整期覆盖未证明。"})
        payload["processing"]["notices"].append({"code": "duplicate_invoice", "message": "同票重复来源已保留。"})
        before = deepcopy(payload)
        result = material_export.build(payload)

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        self.assertFalse({"message", "description", "limitations", "severity", "occurrences",
                          "issues", "notices", "status"} & set(keys(result)))
        self.assertNotIn("quality", result)
        self.assertTrue(result["records"][0]["quality"]["requires_source_review"])
        self.assertEqual(payload, before)

    def test_zero_structured_balances_and_duplicates_stay_explicit(self):
        payload = self.payload()
        row = payload["processing"]["records"][0]
        row.update(material_type="account_balance", opening_balance={"debit": "0", "credit": None},
                   current_debit="0", duplicate_of="original", in_scope=False)
        result = material_export.build(payload)["records"][0]
        self.assertEqual(result["fields"]["opening_balance"], {"debit": "0", "credit": None})
        self.assertEqual(result["fields"]["current_debit"], "0")
        self.assertNotIn("invoice_no", result["fields"])
        self.assertFalse(result["quality"]["in_scope"])
        self.assertEqual(result["quality"]["duplicate_of"], "original")

    def test_source_review_state_is_preserved_without_a_readiness_claim(self):
        payload = self.payload()
        payload["processing"].update(status="ready_for_analysis", issues=[])
        result = material_export.build(payload)
        self.assertNotIn("quality", result)
        self.assertTrue(result["records"][0]["quality"]["requires_source_review"])
        payload["documents"][0]["source_reviewed"] = True
        payload["processing"]["records"][0]["requires_source_review"] = False
        payload["processing"]["records"][0]["tax_amount"] = "13"
        payload["processing"]["records"][0]["value_states"]["tax_amount"] = "present"
        result = material_export.build(payload)
        self.assertFalse(result["records"][0]["quality"]["requires_source_review"])
        payload["task"]["scope_confirmed"] = False
        self.assertEqual(material_export.build(payload), result)

    def test_vat_columns_missing_fields_and_invalid_values_remain_unambiguous(self):
        payload = self.payload()
        row = payload["processing"]["records"][0]
        row.update(material_type="vat_declaration", item_label="销售额", declared_value=None,
                   column_scope="本年累计", value_states={"declared_value": "invalid"})
        result = material_export.build(payload)["records"][0]
        self.assertEqual(result["context"]["column_scope"], "本年累计")
        self.assertIsNone(result["fields"]["item_key"])
        self.assertIsNone(result["fields"]["declared_value"])
        self.assertEqual(result["quality"]["field_states"]["declared_value"], "invalid")
        payload["processing"].update(status="ready_for_analysis", issues=[], records=[])
        result = material_export.build(payload)
        self.assertEqual(result["records"], [])
        self.assertNotIn("quality", result)

    def test_confirmed_values_keep_original_context_and_ocr_evidence(self):
        payload = self.payload()
        row = payload["processing"]["records"][0]
        row.update(unit="万元", confirmed_metadata={"unit": "万元", "direction": "input", "private": "internal"},
                   confirmed_invoice_statuses={"void_status": "正常"})
        row["field_sources"]["amount_excluding_tax"].update(origin="ocr_candidate", quote="合计100.012300")
        result = material_export.build(payload)["records"][0]
        self.assertEqual(result["context"]["unit"], "万元")
        self.assertEqual(result["source"]["context"], {"extracted": {"unit": "元"}, "confirmed": {"unit": "万元"}})
        self.assertEqual(result["source"]["fields"]["amount_excluding_tax"]["quote"], "合计100.012300")
        self.assertEqual(result["source"]["confirmed_fields"], {"void_status": "正常", "direction": "input"})
