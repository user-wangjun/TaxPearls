"""Public statistical collection must never manufacture audit thresholds."""
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.collect_guangdong_reference import AGGREGATES, SOURCES, number, parse_statistics, collect

ROOT = Path(__file__).resolve().parents[1]


def page(revenue='100.00', cost='80.00', publication='2026-02-12'):
    metadata = ('<meta name="ArticleTitle" content="2025年1-12月分行业规模以上工业企业主要经济指标">'
                f'<meta name="PubDate" content="{publication} 09:15:51">')
    header = ('<table><tr><th rowspan="2">主要行业</th><th colspan="2">营业收入</th>'
              '<th colspan="2">营业成本</th></tr><tr><td>本月止累计（亿元）</td>'
              '<td>累计比上年同期增长（%）</td><td>本月止累计（亿元）</td>'
              '<td>累计比上年同期增长（%）</td></tr>')
    rows = ''.join(f'<tr><td>{name}</td><td>{revenue}</td><td>-1.2</td><td>{cost}</td><td>3.4</td></tr>'
                   for name in sorted(AGGREGATES))
    return (metadata + header + rows + '</table>').encode()


def parse(raw):
    return parse_statistics(raw, '2025年1-12月', SOURCES[1][1], '2026-10-05')


class GuangdongPublicDataTests(unittest.TestCase):
    def test_amount_columns_not_growth_columns_and_period(self):
        data = parse(page())
        row = data['rows'][0]
        self.assertEqual(row['revenue_100m_cny'], '100.00')
        self.assertEqual(row['cost_100m_cny'], '80.00')
        self.assertEqual(row['derived_gross_margin_ratio'], '0.20000000')
        self.assertEqual(row['revenue_yoy_percent'], '-1.2')
        self.assertEqual((data['period_start'], data['period_end']), ('2025-01-01', '2025-12-31'))
        self.assertEqual(data['published_on'], '2026-02-12')

    def test_blank_zero_and_negative_growth_are_distinct(self):
        blank, zero = parse(page('', ''))['rows'][0], parse(page('0.00', '0.00'))['rows'][0]
        self.assertIsNone(blank['revenue_100m_cny'])
        self.assertEqual(zero['revenue_100m_cny'], '0.00')
        self.assertIsNone(blank['derived_gross_margin_ratio'])
        self.assertIsNone(zero['derived_gross_margin_ratio'])
        self.assertEqual(parse(page())['rows'][0]['revenue_yoy_percent'], '-1.2')

    def test_expense_above_revenue_keeps_negative_ratio(self):
        self.assertEqual(parse(page('100', '120'))['rows'][0]['derived_gross_margin_ratio'], '-0.20000000')

    def test_unknown_number_is_not_zero(self):
        for value in ('—', 'NaN', '无限', '1,000.00', '1e3', '25%'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                number(value)

    def test_wrong_date_or_title_rejected(self):
        for raw in (page(publication='2027-01-01'), page().replace(b'2025', b'2024'),
                    page().replace(b'2026-02-12', b'2026-02-30')):
            with self.assertRaises(ValueError):
                parse(raw)

    def test_unknown_units_headers_shifted_rows_rejected(self):
        for raw in (page().replace('（亿元）'.encode(), '（万元）'.encode()),
                    page().replace('营业收入'.encode(), '主营业务收入'.encode()),
                    page().replace(b'<td>100.00</td>', b'<td colspan="2">100.00</td>'),
                    page().replace(b'<td>100.00</td>', b'<td>100.00</td><td>1</td>')):
            with self.assertRaises(ValueError):
                parse(raw)

    def test_missing_duplicate_table_or_industry_rejected(self):
        raw = page()
        for candidate in (raw[:raw.index(b'<table>')], raw + raw[raw.index(b'<table>'):],
                          raw.replace('制造业'.encode(), '采矿业'.encode())):
            with self.assertRaises(ValueError):
                parse(candidate)

    def test_negative_income_requires_review(self):
        with self.assertRaises(ValueError):
            parse(page('-100', '80'))

    def test_saved_three_editions_and_all_derived_values_have_no_bounds(self):
        data = json.loads((ROOT / 'docs/广东省公开行业数据.json').read_text(encoding='utf-8'))
        self.assertFalse(data['runtime_integration'])
        self.assertEqual(data['rule_bounds'], [])
        self.assertEqual([e['url'] for e in data['editions']], [u for _, u in SOURCES])
        for edition in data['editions']:
            self.assertEqual(edition['region'], '广东省')
            self.assertEqual(len(edition['rows']), 45)
            self.assertEqual(sum(row['aggregate'] for row in edition['rows']), 4)
            self.assertRegex(edition['response_sha256'], r'^[0-9a-f]{64}$')
            for row in edition['rows']:
                revenue, cost = number(row['revenue_100m_cny'] or ''), number(row['cost_100m_cny'] or '')
                if revenue is None or cost is None or revenue == 0:
                    self.assertIsNone(row['derived_gross_margin_ratio'])
                else:
                    self.assertEqual(Decimal(row['derived_gross_margin_ratio']),
                        ((revenue - cost) / revenue).quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP))
        rows = {r['industry']: r for r in data['editions'][1]['rows']}
        self.assertEqual(rows['计算机、通信和其他电子设备制造业']['revenue_100m_cny'], '57529.49')
        self.assertEqual(rows['计算机、通信和其他电子设备制造业']['cost_100m_cny'], '47240.31')
        missing = [r for r in data['editions'][2]['rows'] if r['revenue_100m_cny'] is None]
        self.assertEqual({r['industry'] for r in missing}, {'煤炭开采和洗选业', '其他采矿业'})

    def test_changed_tax_source_requires_review(self):
        with patch('scripts.collect_guangdong_reference.parse_statistics', return_value={}), \
                patch('scripts.collect_guangdong_reference.fetch', return_value=b'<html>changed</html>'):
            with self.assertRaisesRegex(ValueError, '须重新核对'):
                collect('2026-10-05')


if __name__ == '__main__':
    unittest.main()
