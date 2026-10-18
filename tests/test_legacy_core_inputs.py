"""Legacy XLS follows the real encrypted upload/reopen/export API path."""
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import unittest

from tests import test_core_inputs as core_fixture
from tests.test_legacy_workbooks import biff8, compound, record

SAMPLES = Path(__file__).resolve().parents[1] / 'samples' / '合成测试数据'


class LegacyCoreInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core_fixture.CoreAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client

    def test_biff8_upload_reopen_download_and_standard_json_preserve_scopes(self):
        response = self.client.post('/api/enterprise/input-tasks',json={
            'company_name':'模拟星澜商贸有限公司','tax_no':'91310000SIM20260901',
            'start_date':'2026-09-01','end_date':'2026-09-30','applicable_forms':[]})
        self.assertEqual(response.status_code,200,response.text)
        task=response.json()
        name='02-增值税申报表-一般纳税人.xls'
        raw=(SAMPLES/name).read_bytes()
        response=self.client.post('/api/enterprise/input-tasks/'+task['id']+'/files',
            data={'expected_revision':str(task['revision']),'material_type':'vat_declaration'},
            files={'files':(name,raw,'application/vnd.ms-excel')})
        self.assertEqual(response.status_code,200,response.text)
        uploaded=response.json()
        url='/api/enterprise/input-tasks/'+task['id']
        reopened=self.client.get(url).json()
        self.assertEqual(reopened['revision'],uploaded['revision'])
        doc=reopened['documents'][0]
        self.assertEqual(doc['sha256'],sha256(raw).hexdigest())
        main=[r for r in doc['records'] if r['form_name']=='主表']
        self.assertEqual(len(main),136)
        self.assertEqual({r['column_scope']:Decimal(r['declared_value']) for r in main if r['item_key']=='1'},
            {'一般项目本月数':Decimal('1200000'),'一般项目本年累计':Decimal('1200000'),
             '即征即退项目本月数':Decimal('0'),'即征即退项目本年累计':Decimal('0')})
        original=self.client.get(url+'/originals/'+doc['id'])
        self.assertEqual(original.status_code,200)
        self.assertEqual(original.content,raw)
        exported=self.client.get(url+'/data',params={'expected_revision':reopened['revision']})
        self.assertEqual(exported.status_code,200,exported.text)
        self.assertEqual(exported.json()['schema_version'],'taxpearls.material-data.v1')
        self.assertEqual(len(exported.json()['records']),len(doc['records']))
        self.assertEqual(reopened['processing']['status'],'needs_completion')

    def test_september_account_uploads_reconcile_in_three_format_combinations(self):
        # Source confirmation here is simulated test input, not user acceptance.
        for balance_ext, vat_ext in [('.xlsx', '.xlsx'), ('.xls', '.xls'), ('.pdf', '.xlsx')]:
            with self.subTest(balance=balance_ext, vat=vat_ext):
                response = self.client.post('/api/enterprise/input-tasks', json={
                    'company_name': '模拟星澜商贸有限公司', 'tax_no': '91310000SIM20260901',
                    'start_date': '2026-09-01', 'end_date': '2026-09-30',
                    'applicable_forms': ['附列资料一', '附列资料二']})
                self.assertEqual(response.status_code, 200, response.text)
                task = response.json()
                url = '/api/enterprise/input-tasks/' + task['id']
                originals = {}
                for kind, name in [
                    ('account_balance', '01-科目余额表-财务软件版式' + balance_ext),
                    ('vat_declaration', '02-增值税申报表-一般纳税人' + vat_ext),
                    ('invoice_detail', '03_销项发票_每页一张.pdf'),
                    ('invoice_detail', '04_进项发票_每页一张.pdf'),
                ]:
                    raw = (SAMPLES / name).read_bytes()
                    originals[name] = raw
                    response = self.client.post(url + '/files',
                        data={'expected_revision': str(task['revision']), 'material_type': kind, 'ocr': '0'},
                        files={'files': (name, raw)})
                    self.assertEqual(response.status_code, 200, response.text)
                    task = response.json()
                self.assertEqual(task['processing']['status'], 'needs_completion')
                self.assertIn('invoice_coverage_gap', {i['code'] for i in task['processing']['issues']})
                options = {}
                for doc in task['documents']:
                    change = {'source_reviewed': True}
                    if doc['material_type'] == 'invoice_detail':
                        change.update(confirmed_metadata={'period_start': '2026-09-01', 'period_end': '2026-09-30'},
                            invoice_statuses={r['id']: {'red_letter_status': '蓝字', 'void_status': '正常'}
                                              for r in doc['records']})
                    options[doc['id']] = change
                response = self.client.post(url + '/prepare', json={
                    'expected_revision': task['revision'], 'source_options': options})
                self.assertEqual(response.status_code, 200, response.text)
                task = self.client.get(url).json()
                self.assertEqual(task['processing']['status'], 'ready_for_analysis', task['processing']['issues'])
                self.assertEqual(len(task['processing']['records']), 201)
                summaries = {g['direction']: g for g in task['processing']['invoice_summaries']}
                for direction, count, amount, tax in [('output', 12, '1200000', '156000'), ('input', 10, '700000', '91000')]:
                    group = summaries[direction]
                    self.assertEqual((group['count'], Decimal(group['amount_excluding_tax']), Decimal(group['tax_amount'])),
                                     (count, Decimal(amount), Decimal(tax)))
                for doc in task['documents']:
                    original = self.client.get(url + '/originals/' + doc['id'])
                    self.assertEqual(original.status_code, 200)
                    self.assertEqual(original.content, originals[task['source_options'][doc['id']]['original_name']])
                response = self.client.get(url + '/data', params={'expected_revision': task['revision']})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['schema_version'], 'taxpearls.material-data.v1')
                self.assertEqual(len(response.json()['records']), 201)

    def test_unsupported_formula_rejection_has_no_partial_revision(self):
        task=self.fixture.create()
        url='/api/enterprise/input-tasks/'+task['id']
        raw=compound(biff8(extra=record(6,bytes(25))))
        response=self.client.post(url+'/files',
            data={'expected_revision':str(task['revision']),'material_type':'vat_declaration'},
            files={'files':('含公式.xls',raw,'application/vnd.ms-excel')})
        self.assertEqual(response.status_code,422,response.text)
        self.assertIn('公式',response.json()['detail'])
        reopened=self.client.get(url).json()
        self.assertEqual(reopened['revision'],task['revision'])
        self.assertFalse(reopened['files'])
        self.assertFalse(reopened['documents'])


if __name__ == '__main__':
    unittest.main()
