from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from src import engine, materials
from src.ai_extraction import AIExtractor, Extraction, ExtractionError, call_model
from src.settings import AISettings
from webapp import app as app_module
from webapp.storage import Store
from test_materials import COMPANY, FIXTURES, workbook

ROOT = Path(__file__).resolve().parents[1]
CATALOG = {key: value for rule in engine.load_rules(ROOT / "rules") for key, value in rule.inputs.items()}
SETTINGS = AISettings(enabled=True, api_key="synthetic-secret", vision=False)


def row(value="10", unit="万元", quote="销售额 10", **kw):
    return {"name": "增值税.销售额", "raw_value": value, "unit": unit, "page": 1,
            "quote": quote, "detail": "销售额本期不含税合计", "uncertain": False, **kw}


def answer(rows, company=COMPANY):
    return Extraction(company=company, rows=rows, warnings=[])


def document(text="单位：万元\n销售额 10", pages=None):
    # Constructed AI-contract input with a current local-parser record. This
    # is not retained-byte or real-material acceptance evidence.
    from src import material_provenance
    return {"id": "0", "name": "合成.pdf", "kind": "pdf", "company": deepcopy(COMPANY), "rows": [],
            "pages": pages or [{"page": 1, "text": text}], "page_count": len(pages) if pages else 1,
            "accounts": [], "declarations": {}, "warnings": [], "fingerprint": "test", "error": "",
            "pdf_parser_version": material_provenance.PDF_VERSION,
            "extraction": {"method": "local", "local": {"status": "succeeded",
                           "program": material_provenance.program('local')}}}


def envelope(content, finish="stop"):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode()


class AIContractTests(unittest.TestCase):
    def test_withheld_ai_raw_values_are_not_reported_as_absent_or_user_values(self):
        for raw, expected in [('0', '0'), ('-2.5', '-2.5'), (None, '缺失，人工补录')]:
            with self.subTest(raw=raw):
                quote = '销售额 ' + (raw if raw is not None else '空白')
                doc = document('单位：元\n' + quote)
                response = answer([row(raw, '元', quote, uncertain=True)])
                AIExtractor(SETTINGS, CATALOG, lambda *_: response).enrich(doc, b'')
                self.assertEqual(doc['rows'][0]['value'], '')
                original = deepcopy(doc['rows'])
                edited = {'0': {'reviewed': True, 'rows': [{
                    'name': '增值税.销售额', 'value': '1', 'page': 1,
                    'detail': '人工核对构造原件；单位元',
                }]}}
                dataset = materials.build_dataset([doc], edited, COMPANY, set(CATALOG))
                self.assertEqual(dataset.get('增值税.销售额'), 1)
                self.assertIn('识别候选原值：' + expected, dataset.detail_of('增值税.销售额'))
                self.assertNotIn('识别候选原值：1', dataset.detail_of('增值税.销售额'))
                self.assertEqual(doc['rows'], original)

    def test_extraction_provenance_is_per_file_and_contains_no_credentials(self):
        extractor = AIExtractor(SETTINGS, CATALOG, lambda *_: answer([row()]))
        first, second = document(), document()
        extractor.enrich(first, b"")
        extractor.enrich(second, b"")
        for doc in (first, second):
            meta = doc['extraction']
            self.assertEqual(meta['calls'], 1)
            self.assertEqual(meta['status'], 'succeeded')
            self.assertEqual(meta['model'], SETTINGS.effective_model)
            self.assertIsNone(meta['resolved_model_version'])
            self.assertEqual(len(meta['prompt_sha256']), 64)
            self.assertEqual(len(meta['schema_sha256']), 64)
            self.assertEqual(len(meta['catalog_sha256']), 64)
            self.assertEqual(len(meta['program']['sources']['periods.py']), 64)
            self.assertEqual(meta['attempts'][0]['pages'], [1])
            self.assertEqual(meta['attempts'][0]['status'], 'response_received')
            self.assertNotIn(SETTINGS.api_key, json.dumps(meta))
            self.assertNotIn('api_key', json.dumps(meta))
        self.assertEqual(second['extraction']['batch_calls_after'], 2)
        self.assertEqual(first['extraction']['batch_calls_after'], 1)

    def test_failure_provenance_survives_fallback_and_contains_safe_error_code(self):
        extractor = AIExtractor(SETTINGS, CATALOG, Mock(side_effect=ExtractionError('synthetic-secret', code='timeout')))
        doc = materials.preview([('text.pdf', (FIXTURES/'materials-text.pdf').read_bytes())], set(CATALOG), extractor)[0]
        meta = doc['extraction']
        self.assertEqual(meta['method'], 'ai_failed')
        self.assertEqual(meta['status'], 'failed')
        self.assertEqual(meta['failure_code'], 'timeout')
        self.assertEqual(meta['calls'], 1)
        self.assertEqual(meta['attempts'][0]['status'], 'failed')
        self.assertEqual(meta['local']['status'], 'succeeded')
        self.assertNotIn('synthetic-secret', json.dumps(meta))
        self.assertTrue(doc['rows'])

    def test_partial_response_and_preflight_failure_are_not_successful_extraction(self):
        transport = Mock(side_effect=[answer([row()]), ExtractionError('timeout', code='timeout')])
        doc = document(pages=[{'page': i, 'text': '销售额 10'} for i in range(1, 5)])
        with self.assertRaises(ExtractionError):
            AIExtractor(SETTINGS, CATALOG, transport).enrich(doc, b'')
        self.assertEqual(doc['rows'], [])
        self.assertEqual(doc['extraction']['calls'], 2)
        self.assertEqual(doc['extraction']['status'], 'failed')
        self.assertEqual([a['status'] for a in doc['extraction']['attempts']], ['response_received', 'failed'])
        blocked = document()
        with self.assertRaises(ExtractionError):
            AIExtractor(replace(SETTINGS, enabled=False), CATALOG, transport).enrich(blocked, b'')
        self.assertEqual(blocked['extraction']['calls'], 0)
        self.assertEqual(blocked['extraction']['attempts'], [])
        self.assertEqual(blocked['extraction']['failure_code'], 'configuration')

    def test_contract_fingerprints_change_with_prompt_and_catalog_not_secret(self):
        first = document()
        AIExtractor(SETTINGS, CATALOG, lambda *_: answer([])).enrich(first, b'')
        changed = document()
        with patch('src.ai_extraction.SYSTEM', 'different contract'):
            AIExtractor(replace(SETTINGS, api_key='different-secret'), {**CATALOG, 'new': 'definition'}, lambda *_: answer([])).enrich(changed, b'')
        for key in ('prompt_sha256', 'catalog_sha256'):
            self.assertNotEqual(first['extraction'][key], changed['extraction'][key])
        self.assertEqual(first['extraction']['schema_sha256'], changed['extraction']['schema_sha256'])

    def test_alias_config_and_no_secret_in_public_status(self):
        self.assertEqual(SETTINGS.effective_model, "deepseek-flash")
        self.assertIn("deepseek-flash（配置名 ds-v4.1f）", SETTINGS.public_status()['message'])
        self.assertNotIn("页面图片", SETTINGS.public_status()['message'])
        self.assertIn("文字及页面图片", replace(SETTINGS, vision=True).public_status()['message'])
        proxy = replace(SETTINGS, base_url="https://gateway.example/v1")
        self.assertEqual(proxy.effective_model, "ds-v4.1f")
        self.assertNotIn(SETTINGS.api_key, json.dumps(SETTINGS.public_status()))
        self.assertNotIn(SETTINGS.api_key, repr(SETTINGS))
        self.assertTrue(replace(SETTINGS, api_key="").problem())
        for url in ("http://external.example", "https://user:secret@example.com", "https://example.com?key=secret", "https://example.com/chat/completions"):
            self.assertTrue(replace(SETTINGS, base_url=url).problem())

    def test_real_http_protocol_without_external_model(self):
        observed = {}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.update(path=self.path, authorization=self.headers["Authorization"],
                                payload=json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = envelope(answer([row()]).model_dump_json())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = call_model(replace(SETTINGS, base_url=f"http://127.0.0.1:{server.server_port}/v1"), [{"role":"user","content":"synthetic"}], 5)
            self.assertEqual(result.rows[0].raw_value, "10")
            self.assertEqual(observed["path"], "/v1/chat/completions")
            self.assertEqual(observed["authorization"], "Bearer synthetic-secret")
            self.assertEqual(observed["payload"]["model"], "ds-v4.1f")
            self.assertEqual(observed["payload"]["response_format"], {"type":"json_object"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bad_json_truncation_timeout_and_upstream_errors_are_safe(self):
        cases = [envelope("{}"), envelope("```json\n{}\n```"), envelope(answer([row()]).model_dump_json(), "length"), b'{}']
        for data in cases:
            opener = Mock()
            opener.open.return_value = BytesIO(data)
            with patch("src.ai_transport.build_opener", return_value=opener), self.assertRaises(ExtractionError):
                call_model(SETTINGS, [], 5)
        for error in (TimeoutError("synthetic-secret"), HTTPError("https://example.com", 429, "synthetic-secret", {}, BytesIO(b"private data"))):
            opener = Mock()
            opener.open.side_effect = error
            with patch("src.ai_transport.build_opener", return_value=opener):
                with self.assertRaises(ExtractionError) as caught:
                    call_model(SETTINGS, [], 5)
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertNotIn("private data", str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_conversion_reference_validation_and_missing_values(self):
        doc = document("单位：万元\n销售额 10\n进项税额 0\n应纳税额 6")
        response = answer([row(), row("0", "元", "进项税额 0", name="增值税.进项税额"),
                           row(None, "元", "应纳税额 6", name="增值税.应纳税额"),
                           row("99", "元", "销售额 10", uncertain=True),
                           row("999", "元", "不存在的原文 999"), row(name="虚构指标")])
        AIExtractor(SETTINGS, CATALOG, lambda *_: response).enrich(doc, b"")
        self.assertEqual([r["value"] for r in doc["rows"]], ["100000", "0", "", "", ""])
        self.assertEqual(doc["extraction"]["method"], "ai")
        self.assertTrue(doc["rows"][-1]["ai_issues"])
        self.assertTrue(any("未知指标" in w for w in doc["warnings"]))

    def test_scan_rasterization_and_review_required(self):
        captured = []
        def transport(settings, messages, timeout):
            captured.extend(messages[1]["content"])
            return answer([row("100000", "元", "增值税.销售额：100000")])
        docs = materials.preview([("scan.pdf", (FIXTURES/"materials-scanned.pdf").read_bytes())], set(CATALOG),
                                 AIExtractor(replace(SETTINGS, vision=True), CATALOG, transport))
        doc = docs[0]
        self.assertFalse(doc["error"], doc)
        self.assertEqual(doc["extraction"]["method"], "ai", doc)
        self.assertTrue(any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in captured))
        with self.assertRaisesRegex(materials.InputError, "核对"):
            materials.build_dataset(docs, {"0":{"id":"0"}}, {}, set(CATALOG))
        dataset = materials.build_dataset(docs, {"0":{"id":"0", "reviewed":True, "company": COMPANY}}, {}, set(CATALOG))
        self.assertEqual(dataset.get("增值税.销售额"), 100000)
        self.assertIn("AI 提取模型 deepseek-flash", dataset.source_of("增值税.销售额"))
        self.assertIn("100000 元", dataset.detail_of("增值税.销售额"))

    def test_current_and_historical_columns_are_requested_and_kept_separate(self):
        quote = '营业收入 本期 120 上年同期 100'
        captured = []
        response = answer([
            row('120', '元', quote, name='利润表.营业收入', detail='2026年1月本期金额'),
            row('100', '元', quote, name='历史.上年同期收入', detail='2025-01-01至2025-01-31，上年同期金额'),
        ])
        def transport(settings, messages, timeout):
            captured.append(json.loads(messages[1]['content'][0]['text'].split('\n', 1)[1]))
            return response
        doc = document('单位：元\n' + quote)
        AIExtractor(SETTINGS, CATALOG, transport).enrich(doc, b'')
        self.assertEqual(set(captured[0]['historical_metrics']), {'历史.上年同期收入', '历史.上年同期成本'})
        self.assertEqual(captured[0]['historical_metrics']['历史.上年同期收入'], CATALOG['历史.上年同期收入'])
        self.assertEqual(captured[0]['review_scope'], {'period': COMPANY['period']})
        data = materials.build_dataset([doc], {'0': {'reviewed': True}}, {}, set(CATALOG))
        self.assertEqual(data.get('利润表.营业收入'), 120)
        self.assertEqual(data.get('历史.上年同期收入'), 100)
        self.assertIn('2025-01-01至2025-01-31', data.detail_of('历史.上年同期收入'))
        # A missing history response is never filled from the current column.
        response.rows = response.rows[:1]
        AIExtractor(SETTINGS, CATALOG, transport).enrich(doc, b'')
        self.assertEqual([r['name'] for r in doc['rows']], ['利润表.营业收入'])

    def test_scan_disagreed_number_blank_and_negative_remain_reviewable(self):
        doc = document('', pages=[{'page': 1, 'text': ''}])
        doc['company'] = dict.fromkeys(COMPANY, '')
        response = answer([
            row('1234.56', '元', '经营活动产生的现金流量净额 1,234.65', name='现金流量表.经营净额'),
            row('-25.50', '元', '筹资活动产生的现金流量净额 -25.50', name='现金流量表.筹资净额'),
            row(None, '不明', '汇率变动对现金及现金等价物的影响', name='现金流量表.汇率影响'),
        ], {**COMPANY, 'period': '2026年6月30日'})
        AIExtractor(replace(SETTINGS, vision=True), CATALOG, lambda *_: response).enrich(
            doc, (FIXTURES / 'materials-scanned.pdf').read_bytes(), review_period='2026H1')
        self.assertEqual([r['value'] for r in doc['rows']], ['', '-25.50', ''])
        self.assertIn('原文引用中未找到该数值', doc['rows'][0]['ai_issues'])
        self.assertTrue(all(not r['ai_evidence_verified'] and not r['ai_text_verified'] for r in doc['rows']))
        self.assertEqual(doc['company']['period'], '')
        self.assertEqual(doc['extraction']['period_observations'], [{
            'value': '2026年6月30日', 'as_of': '2026-06-30', 'pages': [1],
            'origin': 'model', 'adopted_as_period': False}])
        self.assertTrue(any('未作为业务期间采用' in warning for warning in doc['warnings']))

    def test_single_report_date_cannot_overwrite_valid_business_period(self):
        for value in ('2026-01-31', '2026 年 1 月 31 日', '2026/1/31', '2026.1.31'):
            with self.subTest(value=value):
                doc = document()
                AIExtractor(SETTINGS, CATALOG, lambda *_: answer([], {**COMPANY, 'period': value})).enrich(doc, b'')
                self.assertEqual(doc['company']['period'], COMPANY['period'])
                self.assertEqual(doc['extraction']['period_observations'][0]['as_of'], '2026-01-31')

    def test_image_inferred_business_period_is_preserved_but_not_source_fact(self):
        doc = document('', pages=[{'page': 1, 'text': ''}])
        doc['company'] = dict.fromkeys(COMPANY, '')
        response = answer([row('120', '元', '营业收入 120', name='利润表.营业收入')],
                          {**COMPANY, 'period': '2026-01-01至2026-06-30'})
        AIExtractor(replace(SETTINGS, vision=True), CATALOG, lambda *_: response).enrich(
            doc, (FIXTURES / 'materials-scanned.pdf').read_bytes(), review_period='2026H1')
        self.assertEqual(doc['company']['period'], '')
        self.assertEqual(doc['rows'][0]['value'], '120')
        self.assertEqual(doc['extraction']['period_observations'][0], {
            'value': '2026-01-01至2026-06-30', 'pages': [1], 'origin': 'model',
            'adopted_as_period': False, 'reason': 'image_period_unverified'})
        self.assertTrue(any('无可验证文字依据' in w for w in doc['warnings']))

    def test_scan_report_date_cannot_prove_history_or_a_different_current_endpoint(self):
        evidence = {'table': '合并利润表', 'column': '本期金额',
                    'period': '2026-01-01至2026-06-30', 'period_quote': '2026年6月30日',
                    'unit_quote': '金额单位：人民币元'}
        valid = row('120', '元', '营业收入 120 100', name='利润表.营业收入', evidence=evidence)
        history = row('100', '元', '营业收入 120 100', name='历史.上年同期收入',
            evidence={**evidence, 'column': '上期金额', 'period': '2025-01-01至2025-06-30'})
        wrong_date = row('30', '元', '营业成本 30 20', name='利润表.营业成本',
            evidence={**evidence, 'period_quote': '2025年6月30日'})
        doc = document('', pages=[{'page': 1, 'text': ''}])
        AIExtractor(replace(SETTINGS, vision=True), CATALOG,
                    lambda *_: answer([valid, history, wrong_date], {**COMPANY, 'period': None})).enrich(
            doc, (FIXTURES / 'materials-scanned.pdf').read_bytes(), review_period='2026H1')
        self.assertEqual([r['value'] for r in doc['rows']], ['120', '', ''])
        self.assertIn('单个报表日期不能证明上期金额属于上年同期', doc['rows'][1]['ai_issues'])
        self.assertTrue(all(not r['ai_evidence_verified'] for r in doc['rows']))

    def test_invoice_tax_cannot_become_declaration_or_ledger_value(self):
        source = "纸质增值税普通发票，发票号码 TEST-1，税额 13000.00 元"
        doc = document("", pages=[{"page": 1, "text": ""}])
        result = answer([row("13000.00", "元", "税额 13000.00", name="增值税.销项税额",
                             detail=source),
                         row("13000.00", "元", "税额 13000.00", name="账面.销项税额",
                             detail=source)])
        AIExtractor(replace(SETTINGS, vision=True), CATALOG, lambda *_: result).enrich(
            doc, (FIXTURES / "materials-scanned.pdf").read_bytes())
        self.assertEqual([item["value"] for item in doc["rows"]], ["", ""])
        self.assertTrue(all("不能直接作为" in item["ai_issues"][-1] for item in doc["rows"]))

    def test_conflicting_candidates_are_flagged_and_cannot_be_committed(self):
        doc = document("单位：元\n销售额 本期 100000 累计 200000")
        response = answer([row("100000", "元", "销售额 本期 100000 累计 200000"),
                           row("200000", "元", "销售额 本期 100000 累计 200000")])
        AIExtractor(SETTINGS, CATALOG, lambda *_: response).enrich(doc, b"")
        self.assertEqual(doc["extraction"]["needs_attention"], 2)
        self.assertTrue(all("不同候选值" in item["ai_issues"][-1] for item in doc["rows"]))
        with self.assertRaisesRegex(materials.InputError, "冲突"):
            materials.build_dataset([doc], {"0": {"id": "0", "reviewed": True}}, {}, set(CATALOG))
        # Decimal-equivalent representations are not a conflict.
        response.rows[1].raw_value = "100000.00"
        AIExtractor(SETTINGS, CATALOG, lambda *_: response).enrich(doc, b"")
        self.assertFalse(any('同一指标存在不同' in issue for r in doc['rows'] for issue in r['ai_issues']))
        self.assertEqual(doc["extraction"]["needs_attention"], 2)  # Structured source proof is still missing.

    def test_unknown_unit_retains_missing_value_after_review(self):
        doc = document("销售额 100000")
        AIExtractor(SETTINGS, CATALOG, lambda *_: answer([
            row("100000", "不明", "销售额 100000", uncertain=True)
        ])).enrich(doc, b"")
        self.assertEqual(doc["rows"][0]["value"], "")
        self.assertIn("原始单位不明", doc["rows"][0]["ai_issues"])
        with self.assertRaisesRegex(materials.InputError, "没有可执行"):
            materials.build_dataset([doc], {"0": {"id": "0", "reviewed": True}}, {}, set(CATALOG))

    def test_failure_preserves_local_candidates_without_claiming_ai(self):
        def fail(*_):
            raise ExtractionError("模拟超时")
        doc = materials.preview([("text.pdf", (FIXTURES/"materials-text.pdf").read_bytes())], set(CATALOG), AIExtractor(SETTINGS, CATALOG, fail))[0]
        self.assertFalse(doc["error"])
        self.assertEqual(doc["extraction"]["method"], "ai_failed")
        self.assertTrue(doc["rows"])
        self.assertIn("模拟超时", doc["warnings"][0])

    def test_unmapped_excel_and_standard_errors_are_not_overwritten(self):
        raw = workbook("导出明细", [["单位", "万元"], ["销售额", 10]])
        extractor = AIExtractor(SETTINGS, CATALOG, lambda *_: answer([row(quote="A2=销售额 | B2=10")]))
        docs = materials.preview([("raw.xlsx", raw)], set(CATALOG), extractor)
        self.assertFalse(docs[0]["error"], docs)
        data = materials.build_dataset(docs, {"0":{"id":"0","reviewed":True}}, {}, set(CATALOG))
        self.assertEqual(data.get("增值税.销售额"), 100000)
        self.assertIn("工作表：导出明细", data.source_of("增值税.销售额"))
        guarded = Mock(side_effect=AssertionError('本地财务公式或原始输入错误不得调用模型'))
        local_only = AIExtractor(SETTINGS, CATALOG, guarded)
        raw = workbook("利润表", [["项目", "本期金额"], ["营业收入", "=1+2"]], COMPANY)
        doc = materials.preview([("formula.xlsx", raw)], set(CATALOG), local_only)[0]
        self.assertEqual(doc['error'], '')
        field = doc['import_mapping']['formula_cells'][0]
        self.assertEqual(field['formula'], '=1+2')
        self.assertIsNone(field['value'])
        self.assertIsNone(field['cached_value'])
        self.assertFalse(any(r['name'] == '利润表.营业收入' for r in doc['rows']))
        raw = workbook("利润表", [["项目", "本期金额"], ["营业收入", True]], COMPANY)
        doc = materials.preview([("bad.xlsx", raw)], set(CATALOG), local_only)[0]
        self.assertIn("标准工作表校验失败", doc["error"])
        guarded.assert_not_called()

    def test_limits_and_conflicting_identity_discard_partial_response(self):
        doc = document(pages=[{"page":i,"text":"销售额 10"} for i in range(1,5)])
        calls=[]
        def transport(*_):
            calls.append(1)
            return answer([row()])
        with self.assertRaises(ExtractionError):
            AIExtractor(replace(SETTINGS, max_calls=1), CATALOG, transport).enrich(doc,b"")
        self.assertEqual(doc["rows"], [])
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(ExtractionError, "不一致"):
            AIExtractor(SETTINGS, CATALOG, lambda *_: answer([], {**COMPANY,"taxpayer_id":"OTHER"})).enrich(document(), b"")


class AIWebTests(unittest.TestCase):
    def test_background_job_owner_schema_status_and_audit(self):
        with tempfile.TemporaryDirectory() as td:
            old = app_module.store
            app_module.store = Store(Path(td)/"ai.db")
            try:
                app_module.store.create_user("aiadmin", "ai-test-pass-2026", "AI教学测试", "teacher", "default")
                with TestClient(app_module.app) as client, patch("webapp.material_upload.AISettings.from_env", return_value=replace(SETTINGS,vision=True)), patch("src.ai_extraction.call_model", return_value=answer([row("100000","元","增值税.销售额：100000")])):
                    client.post("/api/login",json={"username":"aiadmin","password":"ai-test-pass-2026"})
                    status = client.get("/api/materials/config")
                    self.assertTrue(status.json()["ready"])
                    self.assertNotIn(SETTINGS.api_key, status.text)
                    started = client.post("/api/materials/preview", data={"extraction":"ai"},files={"files":("scan.pdf",(FIXTURES/"materials-scanned.pdf").read_bytes())})
                    self.assertEqual(started.status_code, 202, started.text)
                    url = "/api/materials/jobs/" + started.json()["job_id"]
                    job = client.get(url).json()
                    self.assertEqual(job["state"], "done", job)
                    draft = job["result"]
                    result=client.post("/api/materials/audit",json={"token":draft["token"],"mode":"separate","selections":[{"id":"0","reviewed":True,"company":COMPANY}]})
                    self.assertEqual(len(result.json()["results"]),1,result.text)
                    client.post("/api/logout")
                    self.assertEqual(client.get(url).status_code,401)
                    app_module.store.create_user("otherai", "ai-test-pass-2026", "其他教师", "teacher", "default")
                    client.post("/api/login",json={"username":"otherai","password":"ai-test-pass-2026"})
                    self.assertEqual(client.get(url).status_code,404)
            finally:
                app_module.store = old


if __name__ == "__main__":
    unittest.main()
