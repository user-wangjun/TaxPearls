"""从 webapp 持久化库（instance/taxpearls.db）导出某条审计记录的 PDF 报告。

用法：
    python scripts/export_audit_pdf.py              # 导出最新一条审计
    python scripts/export_audit_pdf.py <audit_id>   # 导出指定审计
    python scripts/export_audit_pdf.py --list       # 只列出审计记录

产物写入 output/，文件名与 Web 端导出一致：
    税务风险审计报告-<企业简称>-<审计日期YYYYMMDD>.pdf
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import render  # noqa: E402
from webapp.storage import deserialize_dataset, deserialize_findings  # noqa: E402

DB_PATH = ROOT / "instance" / "taxpearls.db"
OUT_DIR = ROOT / "output"


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if "--list" in argv:
        con = sqlite3.connect(DB_PATH)
        for row in con.execute(
            "select id, company_name, period, audited_at from audits order by audited_at desc"
        ):
            print(f"{row[0]}  {row[1]}  {row[2]}  {row[3]}")
        return 0

    con = sqlite3.connect(DB_PATH)
    if args:
        row = con.execute(
            "select id, company_name, audited_at, dataset_json, findings_json"
            " from audits where id = ?",
            (args[0],),
        ).fetchone()
    else:
        row = con.execute(
            "select id, company_name, audited_at, dataset_json, findings_json"
            " from audits order by audited_at desc limit 1"
        ).fetchone()
    con.close()

    if row is None:
        print("库中没有审计记录", file=sys.stderr)
        return 1

    audit_id, company_name, audited_at, dataset_json, findings_json = row
    dataset = deserialize_dataset(json.loads(dataset_json))
    findings = deserialize_findings(json.loads(findings_json))

    from datetime import datetime

    when = datetime.strptime(audited_at[:19], "%Y-%m-%d %H:%M:%S")
    html, _ = render.render_html(dataset, findings, when=when, write=True)
    report_no = render.make_report_no(company_name, when)
    short = company_name[:12]
    pdf_name = f"税务风险审计报告-{short}-{audited_at[:10].replace('-', '')}.pdf"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = render.export_pdf(html, OUT_DIR / pdf_name, report_no=report_no)

    print(f"  审计 ID：{audit_id}")
    print(f"  被审计单位：{company_name}（{findings and sum(1 for f in findings if f.hit)} 项命中 / 共 {len(findings)} 条规则）")
    print(f"  PDF 报告：{pdf_path}")
    print(f"  文件大小：{pdf_path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
