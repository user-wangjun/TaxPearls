"""New/existing upload selection, source-bound prefills and stable client history."""
from io import BytesIO
import json
import os
from unittest.mock import patch

from openpyxl import load_workbook
from pdfplumber.page import Page
from tests.test_enterprise_materials import EnterpriseMaterialFixture
from tests.test_materials import COMPANY, FIXTURES, accounts
from webapp import app as module, material_batches


class EnterpriseScopeTests(EnterpriseMaterialFixture):
    def scoped_upload(self, company=None, client=None, raw=None, mode=None, filename='企业.xlsx'):
        data = {'company_mode': mode or ('existing' if client else 'new'), 'company': json.dumps(company or {})}
        if client:
            data['client_id'] = client['id']
        response = self.client.post('/api/enterprise/materials', data=data, files={'files': (filename, raw or accounts())})
        self.assertIn(response.status_code, (200, 202), response.text)
        return response.json()

    def test_blank_new_prefills_without_creating_client_until_confirmation(self):
        item = self.scoped_upload()
        self.assertEqual(item['company']['name'], COMPANY['name'])
        self.assertEqual(item['scope_context']['prefill_origins']['name'], 'material_candidate')
        self.assertEqual(self.counts()['clients'], 0)
        self.assertEqual(self.counts()['audits'], 0)
        result = self.confirm(item)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.counts()['clients'], 1)
        self.assertEqual(self.counts()['audits'], 1)

    def test_user_values_retained_and_missing_identity_never_invented(self):
        item = self.scoped_upload({'taxpayer_id': 'OTHER', 'region': '广东省'})
        self.assertEqual(item['company']['taxpayer_id'], 'OTHER')
        self.assertEqual(item['documents'][0]['company']['taxpayer_id'], COMPANY['taxpayer_id'])
        self.assertEqual(item['scope_context']['prefill_origins']['taxpayer_id'], 'user')
        self.assertFalse(item['analysis']['can_confirm'])
        self.assertEqual(self.confirm(item).status_code, 409)
        raw = accounts({**COMPANY, 'name': '', 'taxpayer_id': '', 'industry': ''})
        missing = self.scoped_upload(raw=raw)
        self.assertEqual(missing['company']['taxpayer_id'], '')
        self.assertFalse(missing['analysis']['can_confirm'])

    def test_existing_client_prefill_and_tax_identity_checked_during_analysis(self):
        archive = self.store.upsert_client(self.admin, '档案企业', COMPANY['taxpayer_id'])
        item = self.scoped_upload(client=archive)
        self.assertEqual(item['company']['name'], '档案企业')
        self.assertEqual(item['scope_context']['prefill_origins']['name'], 'client_archive')
        self.assertEqual(item['client_id'], archive['id'])
        self.assertTrue(item['analysis']['can_confirm'])
        self.assertEqual(self.confirm(item).status_code, 200)
        wrong = self.scoped_upload(client=archive, raw=accounts({**COMPANY, 'taxpayer_id': 'FOREIGN'}))
        self.assertFalse(wrong['analysis']['can_confirm'])
        self.assertIn('identity_conflict', [v['code'] for v in wrong['analysis']['feedback']['blocking']])
        bypass = self.client.post(self.url(wrong, '/analyze'), json={'expected_revision': wrong['revision'], 'company': {'taxpayer_id': 'FOREIGN'}})
        self.assertEqual(bypass.status_code, 200, bypass.text)
        self.assertIn('client_identity_conflict', [v['code'] for v in bypass.json()['analysis']['feedback']['blocking']])
        self.assertEqual(self.confirm(bypass.json()).status_code, 409)

    def test_new_duplicate_reuses_archive_without_renaming_or_reassigning(self):
        archive = self.store.upsert_client(self.admin, '不能覆盖的档案名称', COMPANY['taxpayer_id'], self.accountant['id'])
        item = self.scoped_upload()
        self.assertEqual(item['existing_match']['client_id'], archive['id'])
        response = self.confirm(item)
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.store.get_client(archive['id'])
        self.assertEqual((saved['name'], saved['accountant_id']), ('不能覆盖的档案名称', self.accountant['id']))
        self.assertEqual(self.counts()['clients'], 1)
        self.assertEqual(self.store.get_audit(response.json()['audit_id'])['client_id'], archive['id'])

    def test_legacy_tax_format_reuses_identity_without_rewriting_archive(self):
        archive = self.store.upsert_client(self.admin, '旧格式档案', ' test-upload ', self.accountant['id'])
        item = self.scoped_upload()
        self.assertEqual(item['existing_match']['client_id'], archive['id'])
        response = self.confirm(item)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.counts()['clients'], 1)
        self.assertEqual(self.store.get_audit(response.json()['audit_id'])['client_id'], archive['id'])
        self.assertEqual(self.store.get_client(archive['id'])['taxpayer_id'], ' test-upload ')

    def test_ambiguous_legacy_identity_blocks_new_confirmation(self):
        self.store.upsert_client(self.admin, '旧档案甲', ' test-upload ')
        self.store.upsert_client(self.admin, '旧档案乙', 'test-upload')
        item = self.scoped_upload()
        self.assertEqual(self.confirm(item).status_code, 409)
        self.assertEqual(self.counts()['audits'], 0)

    def test_confirmed_history_prefills_context_not_old_period_and_keeps_old_snapshot(self):
        first = self.scoped_upload({'industry': '已核对行业', 'region': '广东省', 'taxpayer_type': '本期身份', 'business_scope': '本期业务'})
        response = self.confirm(first)
        self.assertEqual(response.status_code, 200, response.text)
        audit_id = response.json()['audit_id']
        archive = self.store.list_clients(self.admin)[0]
        frozen = material_batches.confirmed(self.store, self.admin, audit_id)
        profile = self.client.get('/api/enterprise/client-profile/' + archive['id'])
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()['company']['region'], '广东省')
        self.assertEqual(profile.json()['company']['period_start'], '')
        self.assertEqual(profile.json()['analysis_count'], 1)
        second = self.scoped_upload(client=archive, raw=accounts({**COMPANY, 'period': '2026-02'}))
        self.assertEqual(second['company']['period_start'], '2026-02-01')
        self.assertEqual(second['scope_context']['prefill_origins']['region'], 'confirmed_history')
        response2 = self.confirm(second)
        self.assertEqual(response2.status_code, 200, response2.text)
        self.assertEqual(self.counts()['clients'], 1)
        self.assertEqual(self.counts()['audits'], 2)
        self.assertEqual(material_batches.confirmed(self.store, self.admin, audit_id), frozen)
        graphs = [self.client.get('/api/knowledge/graph', params={'audit_id': ident}).json() for ident in (audit_id, response2.json()['audit_id'])]
        companies = [next(n for n in graph['nodes'] if n['kind'] == 'company') for graph in graphs]
        self.assertEqual(companies[0]['id'], companies[1]['id'])
        self.assertNotEqual(companies[0]['period'], companies[1]['period'])

    def test_foreign_unassigned_and_teacher_denied_before_parsing(self):
        foreign = self.store.upsert_client(self.foreign, '外部企业', 'OTHER')
        own = self.store.upsert_client(self.admin, COMPANY['name'], COMPANY['taxpayer_id'])
        for actor, archive in ((self.admin, foreign), (self.accountant, own), (self.teacher, own)):
            self.login(actor)
            self.assertIn(self.client.get('/api/enterprise/client-profile/' + archive['id']).status_code, (403, 404))
            with patch('src.materials.preview') as parser:
                denied = self.client.post('/api/enterprise/materials', data={'company_mode': 'existing', 'client_id': archive['id']}, files={'files': ('企业.xlsx', accounts())})
                self.assertIn(denied.status_code, (403, 404))
                parser.assert_not_called()

    def test_queue_keeps_declared_scope_encrypted_and_supplement_never_overwrites(self):
        worker = module.app.state.material_worker
        with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '1'}), patch.object(worker, 'start'):
            item = self.scoped_upload({'name': '用户填入的企业名称', 'region': '广东省'})
            self.assertEqual(item['analysis_revision'], None)
            with self.store.connect() as db:
                raw = db.execute('SELECT input_cipher FROM material_jobs').fetchone()[0]
                self.assertNotIn('用户填入'.encode(), raw)
            self.assertTrue(worker.run_once())
            item = self.client.get(self.url(item)).json()
            self.assertEqual(item['company']['name'], '用户填入的企业名称')
            self.assertEqual(item['scope_context']['prefill_origins']['name'], 'user')
            response = self.client.post(self.url(item, '/supplement'), data={'expected_revision': str(item['revision'])}, files={'files': ('补传.xlsx', accounts())})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(worker.run_once())
            current = self.client.get(self.url(item)).json()
            self.assertEqual(current['company']['name'], '用户填入的企业名称')
            self.assertEqual(current['company']['region'], '广东省')

    def test_explicit_source_metadata_prefills_and_differences_are_visible(self):
        book = load_workbook(BytesIO(accounts()))
        for pair in [('地区', '广东省'), ('纳税人身份', '原文身份'), ('业务构成', '原文业务')]:
            book['企业信息'].append(pair)
        out = BytesIO()
        book.save(out)
        book.close()
        item = self.scoped_upload({'region': '用户填入地区'}, raw=out.getvalue())
        self.assertEqual(item['company']['taxpayer_type'], '原文身份')
        self.assertEqual(item['company']['region'], '用户填入地区')
        self.assertEqual(item['documents'][0]['company']['region'], '广东省')
        self.assertIn('scope_info_difference', [v['code'] for v in item['analysis']['feedback']['suggested']])

    def test_pdf_scope_labels_are_optional_and_conflicts_are_not_overwritten(self):
        original = Page.extract_text
        raw = (FIXTURES / 'materials-text.pdf').read_bytes()
        for conflict in (False, True):
            suffix = '\n地区：广东省\n纳税人身份：原文身份\n业务构成：原文业务'
            if conflict:
                suffix += '\n地区：另一地区'
            with patch.object(Page, 'extract_text', lambda page, **kwargs: original(page, **kwargs) + suffix):
                item = self.scoped_upload(raw=raw, filename='企业.pdf')
            if conflict:
                self.assertFalse(item['analysis']['can_confirm'])
                self.assertIn('不一致的地区', item['documents'][0]['error'])
            else:
                self.assertFalse(item['documents'][0]['error'])
                self.assertEqual(item['company']['region'], '广东省')
                self.assertEqual(item['company']['taxpayer_type'], '原文身份')

    def test_invalid_mode_fields_and_stale_scope_edit_rejected(self):
        for data in ({'company_mode': 'existing'}, {'company_mode': 'bogus'}, {'company': '[]'},
                     {'company': json.dumps({'fake_qualification': 'true'})}, {'company': '{bad'}):
            response = self.client.post('/api/enterprise/materials', data=data, files={'files': ('企业.xlsx', accounts())})
            self.assertEqual(response.status_code, 422, response.text)
        item = self.scoped_upload()
        edit = self.client.post(self.url(item, '/analyze'), json={'expected_revision': item['revision'], 'company': {'region': '广东省'}})
        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual(edit.json()['scope_context']['prefill_origins']['region'], 'user')
        self.assertEqual(self.confirm(item).status_code, 409)

    def test_multiple_enterprises_never_select_first_material(self):
        response = self.client.post('/api/enterprise/materials', data={'company_mode': 'new'}, files=[
            ('files', ('甲.xlsx', accounts())), ('files', ('乙.xlsx', accounts({**COMPANY, 'name': '另一企业', 'taxpayer_id': 'OTHER'})))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['company']['taxpayer_id'], '')
        self.assertFalse(response.json()['analysis']['can_confirm'])
