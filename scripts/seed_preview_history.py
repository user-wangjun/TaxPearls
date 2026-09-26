"""Add deterministic half-year history to the isolated preview database.

This script is intentionally opt-in: it only updates the database path passed
on the command line and only touches the synthetic preview company.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
from pathlib import Path


SERIES = [
    ("2024-01-01 至 2024-06-30", "2024-07-08T10:20:00", 950_000, 610_000, 138_000, 20_500),
    ("2024-07-01 至 2024-12-31", "2025-01-09T09:45:00", 1_040_000, 665_000, 151_000, 22_700),
    ("2025-01-01 至 2025-06-30", "2025-07-08T11:10:00", 1_110_000, 708_000, 162_000, 24_100),
    ("2025-07-01 至 2025-12-31", "2026-01-09T10:05:00", 1_195_000, 752_000, 178_000, 25_800),
    ("2026-01-01 至 2026-06-30", "2026-09-21T21:00:00", 1_280_000, 780_000, 196_000, 27_320),
]


def metric(name: str, value: int) -> dict[str, str]:
    return {
        "name": name,
        "value": str(value),
        "source": "仿真样例历史经营数据",
        "detail": f"{name} {value:,.2f}",
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: seed_preview_history.py <preview.db>")
    db_path = Path(sys.argv[1]).resolve()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        base = db.execute("SELECT * FROM audits WHERE id='preview-sample'").fetchone()
        if not base or "仿真样例" not in base["company_name"]:
            raise SystemExit("synthetic preview audit not found; database was not changed")

        for index, (period, audited_at, revenue, cost, profit, vat) in enumerate(SERIES):
            dataset = copy.deepcopy(json.loads(base["dataset_json"]))
            dataset["company"]["period"] = period
            dataset["metrics"]["营业收入"] = metric("营业收入", revenue)
            dataset["metrics"]["营业成本"] = metric("营业成本", cost)
            dataset["metrics"]["利润表.净利润"] = metric("利润表.净利润", profit)
            dataset["metrics"]["增值税.应纳税额"] = metric("增值税.应纳税额", vat)
            audit_id = "preview-sample" if index == len(SERIES) - 1 else f"preview-history-{index + 1}"
            values = (
                audit_id, base["org_id"], base["client_id"], base["created_by"],
                base["company_name"], base["taxpayer_id"], base["industry"], period,
                json.dumps(dataset, ensure_ascii=False, separators=(",", ":")),
                base["findings_json"], base["summary_json"], audited_at,
            )
            db.execute(
                """INSERT INTO audits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET period=excluded.period,
                   dataset_json=excluded.dataset_json, audited_at=excluded.audited_at""",
                values,
            )

    print(f"seeded {len(SERIES)} half-year preview periods in {db_path}")


if __name__ == "__main__":
    main()
