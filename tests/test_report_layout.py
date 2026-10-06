"""Exercise the actual print layout with the full synthetic audit evidence."""
from pathlib import Path
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from src import engine, loader, render
from src.models import Company, Dataset, Metric
from src.snapshots import serialize_dataset

ROOT = Path(__file__).resolve().parents[1]


class EvidencePrintLayout(unittest.TestCase):
    def test_confirmed_enterprise_pass_and_unused_inputs_keep_exact_frozen_sources(self):
        reference = {'batch_id': 'constructed-batch', 'analysis_revision': 2, 'confirmation_revision': 3}
        source = '合成原件.pdf [SHA256:' + 'a' * 64 + '] / PDF 第 8 页；本次仅解析原件第 8–8 页/共 97 页'
        detail = '合并资产负债表；本期栏次 期末数；报表时点 2024-12-31（不代表完整期间）；单位 人民币元；原页金额列 x=163.50–230.10'
        # Constructed evidence only, not an actual-original acceptance claim.
        metrics = {key: Metric(key, Decimal(value), source, detail) for key, value in
                   [('资产负债表.资产总额', '150'), ('资产负债表.负债总额', '90'), ('资产负债表.所有者权益', '60')]}
        metrics['现金.期初现金及等价物'] = Metric('现金.期初现金及等价物', Decimal('0'), source, '合成零值原行')
        metrics['现金流量表.经营净额'] = Metric('现金流量表.经营净额', Decimal('-12.34'), source, '合成负数原行')
        metrics['补充指标.未知来源'] = Metric('补充指标.未知来源', Decimal('1'), '', '<script>untrusted()</script>')
        dataset = Dataset(Company('合成企业', 'TEST-REPORT', '服务业', '2024'), [], {}, metrics, sources=['合成原件.pdf'])
        frozen = serialize_dataset(dataset)
        findings = engine.run(engine.load_rules(ROOT / 'rules'), dataset)
        html, _ = render.render_html(dataset, findings, write=False, protect=False, material_reference=reference)
        self.assertIn('附录：已确认输入指标与来源', html)
        self.assertIn('不等于所有指标都已执行检查或原件全部内容已读取', html)
        self.assertNotIn('<script>untrusted()</script>', html)
        self.assertIn('&lt;script&gt;untrusted()&lt;/script&gt;', html)
        self.assertEqual(serialize_dataset(dataset), frozen)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                page.set_content(html, wait_until='load')
                page.emulate_media(media='print')
                page.wait_for_function("document.fonts.status === 'loaded'")
                passed = page.locator('.confirmed-pass')
                self.assertEqual(passed.count(), sum(f.status == 'pass' for f in findings))
                self.assertIn('PDF 第 8 页', passed.inner_text())
                self.assertIn('本期栏次 期末数', passed.inner_text())
                self.assertIn('R-009', passed.locator('thead').inner_text())
                self.assertEqual(page.locator('.confirmed-pass-card').evaluate_all(
                    'cards => cards.map(c => getComputedStyle(c).breakInside)'), ['auto'])
                self.assertEqual(page.locator('.confirmed-pass-intro').evaluate_all(
                    'intros => intros.map(c => getComputedStyle(c).breakInside)'), ['avoid'])
                self.assertEqual(page.locator('.confirmed-section-lead').evaluate(
                    'p => getComputedStyle(p).breakAfter'), 'avoid')
                self.assertEqual(page.locator('.confirmed-input-heading').evaluate(
                    'h => getComputedStyle(h).breakBefore'), 'page')
                self.assertEqual(passed.locator('tbody tr').evaluate_all(
                    'rows => rows.map(r => getComputedStyle(r).breakInside)'), ['avoid'] * passed.locator('tbody tr').count())
                inputs = page.locator('.confirmed-inputs')
                self.assertEqual(inputs.locator('tbody tr').count(), len(metrics))
                self.assertIn('现金流量表.经营净额', inputs.inner_text())
                self.assertIn('-12.34', inputs.inner_text())
                self.assertIn('未记录原始取数来源，不补造', inputs.inner_text())
                self.assertEqual(inputs.locator('tbody tr').filter(has_text='现金.期初现金及等价物').locator('td').nth(1).inner_text(), '0')
                self.assertNotIn('现金流量表.汇率影响', inputs.inner_text())
                self.assertFalse(inputs.locator('td').evaluate_all('cells => cells.some(c => c.scrollWidth > c.clientWidth + 2)'))
            finally:
                browser.close()
        # Shared teaching/legacy reports do not gain an enterprise-confirmed label.
        legacy, _ = render.render_html(dataset, findings, write=False, protect=False)
        self.assertNotIn('附录：已确认输入指标与来源', legacy)
        self.assertNotIn('class="evidence-table confirmed-pass"', legacy)

    def test_no_executable_checks_is_not_clean_and_missing_sources_are_not_invented(self):
        dataset = Dataset(Company('仿真企业', 'TEST-REPORT', '服务业', '2026-01'), [], {}, {}, sources=['仅有不完整底稿.pdf'])
        rules = engine.load_rules(ROOT / 'rules')
        findings = engine.run(rules, dataset)
        html, _ = render.render_html(dataset, findings, write=False)
        self.assertIn('实际完成 0 项', html)
        self.assertIn('本次没有完成可形成判定的检查，不能据此判断是否存在风险', html)
        self.assertNotIn('提供的科目余额表与增值税纳税申报表', html)
        self.assertNotIn('共执行 24 项', html)
        self.assertNotIn('本次审计未发现超出阈值', html)
        self.assertIn('仅有不完整底稿.pdf', html)

    def test_long_threshold_and_source_do_not_collapse_evidence_columns(self):
        dataset = loader.load(ROOT / "samples" / "合成测试数据" / "02-收入少申报-合成.xlsx")
        findings = engine.run(engine.load_rules(ROOT / "rules"), dataset)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(render, "OUTPUT_DIR", Path(directory)):
                html, _ = render.render_html(dataset, findings)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                try:
                    page = browser.new_page()
                    page.set_content(html, wait_until="load")
                    page.emulate_media(media="print")
                    page.wait_for_function("document.fonts.status === 'loaded'")
                    tables = page.locator(".evidence-table").evaluate_all("""tables => tables.map(table => {
                        const row = table.querySelector('tr:nth-child(2)');
                        const width = table.getBoundingClientRect().width;
                        return {
                            columns: [...row.cells].map(cell => cell.getBoundingClientRect().width / width),
                            overflow: [...table.querySelectorAll('td')].some(cell => cell.scrollWidth > cell.clientWidth + 2)
                        };
                    })""")
                    self.assertEqual(len(tables), 4)
                    for table in tables:
                        self.assertGreater(table["columns"][0], .20)
                        self.assertLess(table["columns"][1], .32)
                        self.assertGreater(table["columns"][2], .44)
                        self.assertFalse(table["overflow"])
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
