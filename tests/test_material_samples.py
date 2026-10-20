"""Verify realistic synthetic sources independently of parser compatibility gaps."""
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import unittest
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook

from src import core_materials
from tests.test_core_inputs import mapper

SAMPLES = Path(__file__).resolve().parents[1] / 'samples' / '合成测试数据'
BALANCE = '01-科目余额表-财务软件版式'
GENERAL = '02-增值税申报表-一般纳税人.xlsx'


def small_form():
    """In-memory header regression only; not a second upload sample account."""
    book = Workbook()
    main = book.active
    main.title = '主表'
    for row in [['增值税纳税申报表（小规模纳税人）'], ['纳税人识别号：91310000TEST00001A'],
                ['税款所属期：2026-09-01 至 2026-09-30'], ['金额单位：元'], [], [],
                ['项目', None, None, '栏次', '本期数', None, '本年累计'],
                [None, None, None, None, '货物及劳务', '服务、不动产和无形资产']]:
        main.append(row)
    main['G8'], main['H8'] = '货物及劳务', '服务、不动产和无形资产'
    for span in ['A7:C8', 'D7:D8', 'E7:F7', 'G7:H7']:
        main.merge_cells(span)
    for key in range(1, 23):
        values = [100000, 50000, 450000, 200000] if key == 1 else [0] * 4
        if key == 4:
            values = ['—', 0, '—', 0]
        if key == 14:
            values = [None] * 4
        main.append(['应税销售额' if key == 1 else f'测试项目{key}', None, None, key, *values])
    deduction = book.create_sheet('扣除项目')
    for row in [['扣除项目申报'], [], ['期初余额', '本期发生额', '本期扣除额', '期末余额'],
                ['1', '2', '3', '4=1+2-3'], [0, 20000, 5000, 15000], [],
                ['服务扣除'], ['期初余额', '本期发生额', '本期扣除额', '期末余额'],
                ['1', '2', '3', '4=1+2-3'], [0, 60000, 10000, 50000]]:
        deduction.append(row)
    surcharge = book.create_sheet('附加税费')
    surcharge.append(['税费种', '栏次', '本期税费额', '税费率（%）'])
    surcharge.append(['城市维护建设税', '1', 315, 50])
    raw = BytesIO()
    book.save(raw)
    book.close()
    return raw.getvalue()


class MaterialSampleTests(unittest.TestCase):
    def book(self, name):
        book = load_workbook(SAMPLES / name, data_only=False)
        self.addCleanup(book.close)
        return book

    def parse(self, name, kind):
        db, mapping = mapper()
        self.addCleanup(db.close)
        return core_materials.parse((SAMPLES / name).read_bytes(), name, kind, 'sample', mapping)

    def parse_small(self):
        db, mapping = mapper()
        self.addCleanup(db.close)
        return core_materials.parse(small_form(), 'header-regression.xlsx', 'vat_declaration', 'small', mapping)

    def test_balance_sources_reconcile_and_keep_parent_child_rows(self):
        sheet = self.book(BALANCE + '.xlsx').active
        rows = list(sheet.iter_rows(min_row=6, max_row=35, values_only=True))
        self.assertEqual(len(rows), 30)
        by_code = {r[0]: r for r in rows}
        self.assertEqual(by_code['1002'][5:9], by_code['100201'][5:9])
        self.assertEqual(by_code['100201'][3:9], (500000, 500000, 1100000, 620000, 1100000, 620000))
        self.assertEqual(by_code['600101'][5:7], (0, 1200000))
        self.assertEqual(by_code['160201'][2], '贷')
        self.assertEqual(by_code['160201'][9:11], ('贷', 210000))
        def signed(value, direction):
            return Decimal(str(value)) * (-1 if direction == '贷' else 1)
        for r in rows:
            self.assertEqual(signed(r[4], r[2]) + Decimal(str(r[5])) - Decimal(str(r[6])), signed(r[10], r[9]), r[0])
        leaves = [r for r in rows if len(r[0]) > 4]
        self.assertEqual(sum(signed(r[4], r[2]) for r in leaves), 0)
        self.assertEqual(sum(signed(r[10], r[9]) for r in leaves), 0)
        for col, index in [('F', 5), ('G', 6), ('H', 7), ('I', 8)]:
            self.assertEqual(sheet[col+'37'].value, sum(r[index] for r in leaves))
        self.assertEqual(sum(r[5] for r in leaves), 4777000)

    def test_balance_pdf_and_excel_have_same_records_and_traceable_values(self):
        documents = [self.parse(BALANCE + extension, 'account_balance') for extension in ['.xlsx', '.pdf']]
        for doc in documents:
            self.assertEqual(doc['issues'], [])
            self.assertEqual(len(doc['records']), 30)
        keys = ['opening_balance', 'current_debit', 'current_credit', 'closing_balance']
        normalized = [{r['account_code']: tuple(Decimal(r[key]) for key in keys) for r in d['records']} for d in documents]
        self.assertEqual(normalized[0], normalized[1])
        bank = next(r for r in documents[0]['records'] if r['account_code'] == '100201')
        self.assertEqual(bank['field_sources']['current_debit']['position'], '科目余额表!F7')
        self.assertEqual(bank['period_start'], '2026-09-01')
        self.assertEqual(bank['period_end'], '2026-09-30')
        self.assertEqual(bank['raw_record']['2'], '借')
        self.assertEqual(bank['raw_record']['9'], '借')
        self.assertTrue(any('第2页' in r['source_position'] for r in documents[1]['records']))

    def test_general_main_preserves_four_project_and_period_scopes(self):
        book = self.book(GENERAL)
        self.assertEqual(book.sheetnames, ['主表', '附列资料一', '附列资料二'])
        doc = self.parse(GENERAL, 'vat_declaration')
        records = [r for r in doc['records'] if r['form_name'] == '主表']
        self.assertEqual(len(records), 34 * 4)
        sales = {r['column_scope']: r['declared_value'] for r in records if r['item_key'] == '1'}
        self.assertEqual(sales, {'一般项目本月数': '1200000', '一般项目本年累计': '1200000',
                                 '即征即退项目本月数': '0', '即征即退项目本年累计': '0'})
        self.assertEqual(book['附列资料一']['D9'].value, 156000)
        self.assertEqual(book['附列资料二']['D8'].value, 700000)
        self.assertEqual(book['附列资料二']['E8'].value, 91000)

    def test_small_source_keeps_business_scopes_blank_zero_and_not_applicable(self):
        book = load_workbook(BytesIO(small_form()))
        self.addCleanup(book.close)
        s = book['主表']
        self.assertIn('E7:F7', {str(r) for r in s.merged_cells.ranges})
        self.assertIn('G7:H7', {str(r) for r in s.merged_cells.ranges})
        self.assertEqual([s.cell(8, c).value for c in range(5, 9)], ['货物及劳务', '服务、不动产和无形资产'] * 2)
        self.assertEqual([s.cell(9, c).value for c in range(5, 9)], [100000, 50000, 450000, 200000])
        self.assertEqual(s['E12'].value, '—')
        self.assertEqual(s['F12'].value, 0)
        self.assertTrue(all(s.cell(22, c).value is None for c in range(5, 9)))

    def test_small_main_parser_keeps_all_four_business_and_period_columns(self):
        doc = self.parse_small()
        main = [r for r in doc['records'] if r['form_name'] == '主表']
        sales = [r for r in main if r['item_key'] == '1']
        self.assertEqual({r['column_scope']:r['declared_value'] for r in sales}, {
            '本期数货物及劳务':'100000', '本期数服务、不动产和无形资产':'50000',
            '本年累计货物及劳务':'450000', '本年累计服务、不动产和无形资产':'200000'})
        self.assertEqual(len(main), 88)
        self.assertEqual({r['declared_value'] for r in main if r['item_key']=='14'}, {None})

    def test_appendix_sales_tax_deductions_and_surcharge_keep_their_sources(self):
        general = self.parse(GENERAL, 'vat_declaration')
        rows = [r for r in general['records'] if r['form_name']=='附列资料一' and r['item_key']=='1']
        self.assertEqual({r['column_scope']:r['declared_value'] for r in rows}, {
            '开具增值税专用发票销售额':'1200000', '开具增值税专用发票销项税额':'156000',
            '合计销售额':'1200000', '合计销项税额':'156000'})
        small = self.parse_small()
        by_position = {r['source_position']:r for r in small['records']}
        self.assertEqual(by_position['扣除项目!D5']['declared_value'], '15000')
        self.assertEqual(by_position['扣除项目!D10']['declared_value'], '50000')
        self.assertEqual(by_position['扣除项目!D5']['field_sources']['item_label']['position'], '扣除项目!A1')
        self.assertEqual(by_position['扣除项目!D10']['field_sources']['item_label']['position'], '扣除项目!A7')
        self.assertEqual(by_position['附加税费!C2']['declared_value'], '315')
        self.assertEqual(by_position['附加税费!D2']['unit'], '%')

    def test_old_excel_samples_match_xlsx_facts_and_source_positions(self):
        from src.material_formats import validate
        for filename,kind in [(BALANCE+'.xlsx','account_balance'),(GENERAL,'vat_declaration')]:
            old_name=filename.replace('.xlsx','.xls')
            with self.subTest(name=old_name):
                self.assertEqual(validate(old_name,(SAMPLES/old_name).read_bytes())['biff_version'],80)
                newer,older=self.parse(filename,kind),self.parse(old_name,kind)
                def facts(doc):
                    return [(r['source_position'],r.get('account_code'),r.get('item_key'),r.get('column_scope'),
                             Decimal(r['declared_value']) if r.get('declared_value') is not None else None,
                             Decimal(r['current_debit']) if r.get('current_debit') is not None else None)
                            for r in doc['records']]
                self.assertEqual(facts(newer),facts(older))

    def test_samples_are_fixed_value_workbooks_without_active_parts(self):
        for name in [BALANCE+'.xlsx', GENERAL]:
            with self.subTest(name=name):
                book = self.book(name)
                for sheet in book:
                    self.assertIn('模拟星澜商贸有限公司', ' '.join(str(c.value) for row in sheet.iter_rows(max_row=5) for c in row))
                    self.assertFalse(any(c.data_type == 'f' for row in sheet for c in row))
                with ZipFile(SAMPLES / name) as archive:
                    self.assertFalse(any(any(part in path.lower() for part in ['vbaproject', 'externallinks', 'embeddings']) for path in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
