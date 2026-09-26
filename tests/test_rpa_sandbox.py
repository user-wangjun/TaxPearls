"""FR-A13: browser-driven localhost sandbox collection and boundary checks."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import engine, loader
from scripts import rpa_sandbox


class RPASandboxTests(unittest.TestCase):
    def test_collects_two_synthetic_portals_into_auditable_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collected.xlsx"
            self.assertEqual(rpa_sandbox.collect(path), path)
            dataset = loader.load(path)
            self.assertIn("仿真", dataset.company.name)
            self.assertEqual(str(dataset.metrics["营业收入"].value), "1280000")
            self.assertEqual(str(dataset.metrics["增值税.销售额"].value), "964000")
            findings = {item.rule.id: item for item in engine.run(engine.load_rules(Path(__file__).resolve().parent.parent / "rules"), dataset)}
            self.assertEqual(findings["R-001"].status, "hit")
            with self.assertRaises(FileExistsError):
                rpa_sandbox.collect(path)

    def test_rejects_portal_identity_mismatch_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            original = rpa_sandbox._read_portal

            def wrong_period(page, base_url, route, headers):
                company, rows = original(page, base_url, route, headers)
                if route == "/tax":
                    company["所属期"] = "2025-01"
                return company, rows

            with patch.object(rpa_sandbox, "_read_portal", side_effect=wrong_period):
                with self.assertRaisesRegex(ValueError, "身份不一致"):
                    rpa_sandbox.collect(path)
            self.assertFalse(path.exists())

    def test_rejects_bad_amount_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            original = rpa_sandbox._read_portal

            def bad_amount(page, base_url, route, headers):
                company, rows = original(page, base_url, route, headers)
                if route == "/tax":
                    rows[0][1] = "nan"
                return company, rows

            with patch.object(rpa_sandbox, "_read_portal", side_effect=bad_amount):
                with self.assertRaisesRegex(ValueError, "采集金额"):
                    rpa_sandbox.collect(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
