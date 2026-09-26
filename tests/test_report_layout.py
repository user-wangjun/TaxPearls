"""Exercise the actual print layout with the full synthetic audit evidence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from src import engine, loader, render

ROOT = Path(__file__).resolve().parents[1]


class EvidencePrintLayout(unittest.TestCase):
    def test_long_threshold_and_source_do_not_collapse_evidence_columns(self):
        dataset = loader.load(ROOT / "samples" / "合成测试数据-20260921" / "02-收入少申报-合成.xlsx")
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
