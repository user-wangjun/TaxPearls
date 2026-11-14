"""Source-change quarantine and offline replay, without approving a real policy."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import audit_guangdong_vat as audit
from scripts import repair_guangdong_corpus as repair
from scripts import complete_guangdong_vat_coverage as coverage


class GuangdongVATAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.package = Path(self.tmp.name)
        self.body = '本公告自2026年1月1日起施行。纯合成工程测试。'
        self.raw = ('<div id="zoomcon">' + self.body + '</div>').encode()
        self.url = 'https://example.gov.cn/source.html'
        doc = {'document_id': 'synthetic', 'source_url': self.url, 'title': '合成增值税测试',
               'tax_types': ['增值税'], 'raw_path': 'source.html', 'raw_sha256': repair.sha(self.raw),
               'text_path': 'source.txt', 'text_sha256': repair.sha(self.body.encode()),
               'attachment_urls': [], 'body_image_urls': [], 'content_gaps': [],
               'date_coverage': {'effective_from': '2026-01-01', 'effective_to': None}}
        (self.package / 'source.html').write_bytes(self.raw)
        (self.package / 'source.txt').write_bytes(self.body.encode())
        repair.write_json(self.package / '来源清单.json', {'documents': [doc]})
        repair.write_json(self.package / '附件清单.json', {'assets': []})
        repair.write_json(self.package / '效力核对结果.json', {'documents': [
            {'document_id': 'synthetic', 'cleanup_verdict': None, 'matches': []}]})
        self.doc = doc

    def test_exact_tax_membership_and_no_implicit_legal_approval(self):
        with patch.object(repair, 'fetch', return_value=self.raw):
            report = audit.audit(self.package, online=True)
        self.assertEqual(report['candidate_import_counts'], {'passed': 1})
        self.assertEqual(report['formal_approvals'], 0)
        self.assertEqual(report['documents'][0]['legal_review'], 'pending_human_review')
        self.assertEqual(audit.vat_documents({'documents': [{'tax_types': ['土地增值税']}]}), [])
        with patch.object(repair, 'fetch', side_effect=AssertionError('offline replay must not fetch')):
            self.assertEqual(audit.audit(self.package), report)

    def test_changed_source_and_failed_fetch_stay_out_of_candidate_delivery(self):
        with patch.object(repair, 'fetch', return_value=self.raw.replace('工程'.encode(), '变更'.encode())):
            changed = audit.audit(self.package, online=True)
        self.assertEqual(changed['candidate_import_counts'], {'blocked': 1})
        with patch.object(repair, 'fetch', side_effect=OSError('synthetic source unavailable')):
            missing = audit.audit(self.package, online=True)
        self.assertEqual(missing['source_errors'], 1)
        self.assertEqual(missing['candidate_import_counts'], {'blocked': 1})
        self.assertEqual((self.package / 'source.html').read_bytes(), self.raw)

    def test_capture_tamper_is_rejected(self):
        with patch.object(repair, 'fetch', return_value=self.raw):
            audit.audit(self.package, online=True)
        path = self.package / '增值税来源复核快照.zip'
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read('capture.json'))
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('capture.json', json.dumps(metadata))
            archive.writestr(metadata['sources'][0]['capture_path'], b'tampered')
        with self.assertRaisesRegex(ValueError, '快照哈希'):
            audit.audit(self.package)

    def test_reviewed_identity_is_bound_to_source_and_unique_target(self):
        proof = {'source_url': self.url, 'text_path': 'source.txt',
                 'text_sha256': repair.sha(self.body.encode()), 'quote': self.body,
                 'start_char': 0, 'end_char_exclusive': len(self.body)}
        row = {'source_url': self.url, 'source_text_sha256': proof['text_sha256'],
               'title': '国家税务总局关于合成测试的通知', 'document_no_quoted': '国税发〔2005〕41号',
               'quote': self.body, 'start_char': 0, 'end_char_exclusive': len(self.body),
               'status': 'unresolved', 'matched_policy_ids': []}
        review = {'quoted_number': row['document_no_quoted'], 'canonical_document_no': '国税发〔2005〕141号',
                  'conclusion': 'same_intended_document_source_number_anomaly', 'evidence': [proof]}
        target = {'policy_id': 'synthetic-target', 'title': row['title'], 'document_no': review['canonical_document_no']}
        resolved = coverage.resolve_reviewed_reference(self.package, row, [target], [review])
        self.assertEqual(resolved['matched_policy_ids'], ['synthetic-target'])
        self.assertEqual(resolved['document_no_quoted'], row['document_no_quoted'])
        self.assertEqual(resolved['legal_review'], 'pending_human_review')
        other_source = {**row, 'source_url': 'https://example.gov.cn/other.html'}
        self.assertEqual(coverage.resolve_reviewed_reference(self.package, other_source, [target], [review]), other_source)
        with self.assertRaisesRegex(ValueError, 'one retained target'):
            coverage.resolve_reviewed_reference(self.package, row, [target, {**target, 'policy_id': 'duplicate'}], [review])
        (self.package / 'source.txt').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            coverage.resolve_reviewed_reference(self.package, row, [target], [review])

    def test_title_correction_with_unchanged_number_does_not_change_another_citation(self):
        from src.public_policy_package import citation_view
        short = '关于加强货物运输业税收征收管理的通知'
        full = '国家税务总局关于加强货物运输业税收征收管理有关问题的通知'
        number = '国税发明电〔2003〕55号'
        quote = f'《{short}》（{number}）'
        other = f'《{short}》（国税发〔2003〕121号）'
        content = quote + '；' + other
        row = {'source_url': self.url, 'title': short, 'canonical_title': full,
               'document_no_quoted': number, 'canonical_document_no': number,
               'source_text_sha256': 'synthetic-sha', 'start_char': 0, 'end_char_exclusive': len(quote),
               'quote': quote, 'matched_policy_ids': []}
        clause = {'content': content, 'source_location': {'text_sha256': 'synthetic-sha', 'start_char': 0}}
        view = citation_view(clause, {'source_url': self.url}, resolutions=[row])
        self.assertIn(f'《{full}》（{number}）', view['text'])
        self.assertIn(other, view['text'])
        self.assertEqual(view['original_text'], content)
