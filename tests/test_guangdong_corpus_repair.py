"""Source integrity, temporal evidence and offline rebuild regressions."""
import json
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import repair_guangdong_corpus as repair


class ClassificationAndDates(unittest.TestCase):
    def dates(self, text, **kwargs):
        return repair.extract_dates(text, "2022-06-01", "2026-10-20", **kwargs)

    def test_specific_tax_names_are_not_substring_matches(self):
        self.assertEqual(repair.tax_tags("土地增值税清算管理办法"), ["土地增值税"])
        self.assertEqual(repair.tax_tags("水资源税改革试点"), ["水资源税"])
        self.assertEqual(repair.tax_tags("房产税和城镇土地使用税"), ["房产税", "城镇土地使用税"])

    def test_explicit_range_and_unknown_end(self):
        for text in ["本通知自2023年1月1日起执行至2027年12月31日。",
                     "本公告执行期限为2023年1月1日至2027年12月31日。"]:
            result = self.dates(text)
            self.assertEqual((result["effective_from"], result["effective_to"]), ("2023-01-01", "2027-12-31"))
            self.assertTrue(result["effective_to_evidence"])
        result = self.dates("本公告自2023年1月1日起施行。", page_validity="有效")
        self.assertIsNone(result["effective_to"])
        self.assertEqual(result["status_as_of"], "unknown")

    def test_duration_is_a_reviewable_calculation(self):
        result = self.dates("本通知自2019年1月1日起实施，试行一年。")
        self.assertEqual(result["effective_to"], "2019-12-31")
        self.assertEqual(result["status_as_of"], "expired")
        self.assertIn("duration_end_requires_review", result["gaps"])
        relative = self.dates("本公告自制发之日起施行，有效期5年。")
        self.assertEqual(relative["effective_from"], "2022-06-01")
        self.assertEqual(relative["effective_to"], "2027-05-31")

    def test_compilations_do_not_acquire_a_quoted_policy_date(self):
        result = self.dates("本通知自2020年1月1日起执行。", kind="reference")
        self.assertIsNone(result["effective_from"])

    def test_cleanup_notice_can_have_its_own_enactment_but_not_a_quoted_date(self):
        result = self.dates('本公告自2017年4月1日起实施。', kind='cleanup')
        self.assertEqual(result['effective_from'], '2017-04-01')
        result = self.dates('废止《某办法》（该办法自2010年1月1日起执行）。', kind='cleanup')
        self.assertIsNone(result['effective_from'])
        result = self.dates("依据《某办法》自2020年1月1日起施行的规定办理。")
        self.assertIsNone(result["effective_from"])

    def test_chinese_digits(self):
        self.assertEqual(repair.normal_dates("一九八七年一月一日；二〇二一年九月二十九日"), "1987年1月1日；2021年9月29日")

    def test_table_cells_and_all_body_images_are_retained(self):
        text, files, images = repair.html_parts('<div id="zoomcon"><p>附件下载</p><table><tr><td><p>税目</p></td><td><p>税额</p></td></tr></table>' + ''.join(f'<img src="{i}.png">' for i in range(8)) + '</div><a href="a.pdf?download=1">附件</a>')
        self.assertIn("税目\t税额", text)
        self.assertEqual(len(images), 8)
        self.assertEqual(files[0]["link"], "a.pdf?download=1")

    def test_catalog_conflicting_number_and_later_edition_are_not_matched(self):
        catalog = {"catalog_date": "2018-01-01", "kind": "full", "page_url": "page", "attachment_url": "sheet",
                   "raw_path": "sheet.xls", "raw_sha256": "digest", "records": [{"title": "某办法", "document_no": "粤税〔2010〕1号", "row": 3}]}
        d = {"title": "某办法", "publish_date": "2010-01-01", "document_no": "粤税〔2012〕2号"}
        self.assertIsNone(repair.match_cleanup(d, [catalog])["cleanup_verdict"])
        d.update(document_no="粤税〔2010〕1号", publish_date="2020-01-01")
        self.assertIsNone(repair.match_cleanup(d, [catalog])["cleanup_verdict"])
        d["publish_date"] = "2010-01-01"
        self.assertEqual(repair.match_cleanup(d, [catalog])["cleanup_verdict"], "expired")
        catalog["kind"] = "keep"
        self.assertEqual(repair.match_cleanup(d, [catalog])["cleanup_verdict"], "historically_kept")

    def test_short_notice_number_requires_title_and_same_year_number(self):
        catalog = {"catalog_date": "2018-06-15", "kind": "full", "page_url": "page", "attachment_url": "sheet",
                   "raw_path": "sheet.xls", "raw_sha256": "digest", "records": [{"title": "启用新版发票", "document_no": "广东省国家税务局公告2010年第1号", "row": 125}]}
        doc = {"title": "启用新版发票", "publish_date": "2016-05-06", "document_no": "2016年第2号"}
        self.assertIsNone(repair.match_cleanup(doc, [catalog])["cleanup_verdict"])
        catalog["records"][0]["document_no"] = "广东省国家税务局公告2016年第2号"
        catalog["kind"] = "modified"
        self.assertEqual(repair.match_cleanup(doc, [catalog])["cleanup_verdict"], "modified")
        catalog["records"][0].update(title="另一公告", document_no="2016年第2号")
        self.assertIsNone(repair.match_cleanup(doc, [catalog])["cleanup_verdict"])

    def test_incomplete_attachments_cannot_be_priority_review(self):
        item = {"date_coverage": self.dates("本通知自2023年1月1日起执行。"), "text_status": "parsed",
                "document_kind": "policy_candidate", "content_gaps": ["attachment_incomplete:url"], "validity_observed": "有效"}
        self.assertEqual(repair.grade(item, {"cleanup_verdict": None})[0], "待补齐")
        item["content_gaps"] = []
        self.assertEqual(repair.grade(item, {"cleanup_verdict": "modified"})[0], "待复核")


class OfflineIntegrity(unittest.TestCase):
    def test_research_replay_keeps_unknown_dates_and_rejects_changed_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, text = b'official raw', '明确废止旧文件。'
            (root/'source.html').write_bytes(raw)
            (root/'source.txt').write_bytes(text.encode())
            event = {'kind':'full_repeal','effective_on':None,'raw_path':'source.html',
                     'raw_sha256':repair.sha(raw),'text_path':'source.txt',
                     'text_sha256':repair.sha(text.encode()),'start_char':0,
                     'end_char_exclusive':len(text),'quote':text}
            document={'legal_research':{'events':[event]}}
            result=repair.apply_research(root,document,repair.extract_dates('未明确施行时间。',None,'2026-10-21'))
            self.assertEqual(result['status_as_of'],'expired')
            self.assertIsNone(result['effective_from'])
            self.assertIsNone(result['effective_to'])
            event['quote']='不同内容'
            with self.assertRaisesRegex(ValueError,'效力检索证据'):
                repair.apply_research(root,document,result)

    def test_own_enactment_with_tax_period_and_starting_qualifiers(self):
        for text, expected in [('本通知自2005年1月1日（税款所属时期）起执行。', '2005-01-01'),
                               ('本通知自2007年10月1日起开始执行。', '2007-10-01')]:
            result = repair.extract_dates(text, None, '2026-10-21')
            self.assertEqual(result['effective_from'], expected)
            self.assertIsNone(result['effective_to'])
            self.assertTrue(result['effective_from_evidence'])

    def test_doc_piece_table_orders_text_and_rejects_encryption_and_bad_offsets(self):
        word = bytearray(1200)
        struct.pack_into('<HH', word, 0, 0xA5EC, 0xC1)
        struct.pack_into('<H', word, 32, 0)
        struct.pack_into('<H', word, 34, 0)
        struct.pack_into('<H', word, 36, 34)
        struct.pack_into('<II', word, 38 + 33 * 8, 0, 33)
        word[1000:1004] = '正文'.encode('utf-16-le')
        word[900:904] = b'\rABC'
        plc = struct.pack('<III', 0, 2, 6) + struct.pack('<HIH', 0, 1000, 0) + struct.pack('<HIH', 0, 0x40000000 | 1800, 0)
        table = b'\x02' + struct.pack('<I', len(plc)) + plc
        self.assertEqual(repair.doc_piece_text(word, table), '正文\nABC')
        struct.pack_into('<H', word, 10, 0x100)
        with self.assertRaisesRegex(ValueError, 'encrypted'):
            repair.doc_piece_text(word, table)
        struct.pack_into('<H', word, 10, 0)
        with self.assertRaises(ValueError):
            repair.doc_piece_text(word, table[:-1])

    def test_doc_main_story_excludes_footer_fields_and_checks_count(self):
        word = bytearray(1200)
        struct.pack_into('<HH', word, 0, 0xA5EC, 0xC1)
        struct.pack_into('<H', word, 32, 0)
        struct.pack_into('<H', word, 34, 4)
        struct.pack_into('<I', word, 48, 2)
        struct.pack_into('<H', word, 52, 34)
        struct.pack_into('<II', word, 54 + 33 * 8, 0, 21)
        word[1000:1014] = '正文PAGE1'.encode('utf-16-le')
        plc = struct.pack('<II', 0, 7) + struct.pack('<HIH', 0, 1000, 0)
        table = b'\x02' + struct.pack('<I', len(plc)) + plc
        self.assertEqual(repair.doc_piece_text(word, table), '正文')
        struct.pack_into('<I', word, 48, 8)
        with self.assertRaisesRegex(ValueError, '正文字符数量'):
            repair.doc_piece_text(word, table)

    def test_guangdong_pilot_excludes_shenzhen_and_keeps_subject_conditions(self):
        text = '广东省（不含深圳市，下同）。在部分增值税纳税人中开展试点，具体范围由主管部门确定。'
        scope = repair.source_scope(text)
        self.assertEqual(scope['excluded_regions'], ['CN-44-4403'])
        self.assertTrue(scope['conditions'])
        for item in scope['conditions'] + scope['evidence']:
            self.assertEqual(text[item['start_char']:item['end_char_exclusive']], item['quote'])
        self.assertEqual(repair.source_scope('深圳市执行情况另行统计。')['excluded_regions'], [])

    def test_missing_attachment_is_explicit_and_never_fetches(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(repair, "fetch", side_effect=AssertionError("network")) as fetch:
            asset = repair.asset_record(Path(tmp), "https://example.gov.cn/table.xls", "attachment", "税额表", None, False)
            self.assertEqual(asset["status"], "missing")
            fetch.assert_not_called()

    def test_reviewed_whole_repeal_replays_only_explicit_bound_target_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            quote = '本公告自2016年6月1日起施行。《旧办法》（旧文号）同时废止。'
            raw = b'<html>retained official source</html>'
            (package / 'source.html').write_bytes(raw)
            (package / 'source.txt').write_bytes(quote.encode())
            event = {'raw_path': 'source.html', 'raw_sha256': repair.sha(raw),
                     'text_path': 'source.txt', 'text_sha256': repair.sha(quote.encode()),
                     'quote': quote, 'start_char': 0, 'end_char_exclusive': len(quote),
                     'kind': 'full_repeal', 'target_document_id': 'old', 'effective_on': '2016-06-01'}
            document = {'document_id': 'old', 'legal_research': {'events': [event]}}
            def dates():
                return {'effective_from': None, 'effective_to': None, 'status_as_of': 'unknown',
                        'gaps': ['effective_from_unknown', 'effective_to_unknown']}
            self.assertIsNone(repair.apply_research(package, document, dates())['effective_to'])
            document['legal_research']['reviewed_termination'] = {'event_index': 0, 'effective_to': '2016-05-31'}
            result = repair.apply_research(package, document, dates())
            self.assertEqual(result['effective_to'], '2016-05-31')
            self.assertIsNone(result['effective_from'])
            self.assertNotIn('effective_to_unknown', result['gaps'])
            for field, bad, good in [('kind', 'partial_repeal', 'full_repeal'),
                                     ('target_document_id', 'another-policy', 'old'),
                                     ('effective_on', '2018-06-01', '2016-06-01')]:
                event[field] = bad
                with self.assertRaises(ValueError):
                    repair.apply_research(package, document, dates())
                event[field] = good

    def test_multiline_extracted_file_hash_matches_bytes_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            url = "https://example.gov.cn/a.pdf"
            path = package / f"法规原文/附件/{repair.sha(url.encode())[:24]}.pdf"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"%PDF test")
            with patch.object(repair, "extract_asset", return_value=("first\nsecond\n", "extracted_needs_layout_review")):
                item = repair.asset_record(package, url, "attachment", "附件", None, False)
            self.assertEqual(repair.read_verified(package, item["text_path"], item["text_sha256"]), b"first\nsecond\n")

    def test_traversal_and_modified_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            with self.assertRaises(ValueError):
                repair.checked_path(package, "../outside")
            (package / "source").write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                repair.read_verified(package, "source", repair.sha(b"original"))

    def test_offline_rebuild_is_repeatable_and_keeps_raw_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(repair, "fetch", side_effect=AssertionError("network")) as fetch:
            package = Path(tmp)
            (package / "法规原文").mkdir()
            html = ('<div id="zoomcon"><p>本通知自2023年1月1日起执行至2027年12月31日。</p><p>' + '来源正文。' * 20 + '</p></div><a href="a.xls">税额表</a>').encode()
            (package / "法规原文/a.html").write_bytes(html)
            (package / "法规原文/a.txt").write_bytes(b"old")
            doc = {"document_id": "test", "title": "土地增值税规定", "publish_date": "2023-01-01", "document_no": None,
                   "source_url": "https://example.gov.cn/a.html", "raw_path": "法规原文/a.html", "raw_sha256": repair.sha(html),
                   "text_path": "法规原文/a.txt", "text_sha256": repair.sha(b"old"), "validity_observed": "有效"}
            repair.write_json(package / "来源清单.json", {"as_of": "2026-10-20", "documents": [doc]})
            report = repair.repair(package)
            first = {p.name: p.read_bytes() for p in package.iterdir() if p.is_file()}
            repair.repair(package)
            second = {p.name: p.read_bytes() for p in package.iterdir() if p.is_file()}
            self.assertEqual(first, second)
            self.assertEqual((package / "法规原文/a.html").read_bytes(), html)
            self.assertEqual(report["review_queues"], {"待补齐": 1})
            self.assertEqual(report["business_database_writes"], 0)
            rebuilt = json.loads((package / "来源清单.json").read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["documents"][0]["tax_types"], ["土地增值税"])
            self.assertEqual(rebuilt["documents"][0]["validity_status"], "unverified")
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
