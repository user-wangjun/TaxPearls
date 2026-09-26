"""生成仿真审计材料样例。

⚠️ 本脚本生成的全部数据均为**人工构造的仿真数据**，不含任何真实企业信息，
   仅用于开发与教学演示，严禁替换为真实企业账套。

运行：
    python scripts/make_sample.py

预期规则判定结果（用于回归验证）：
    R-001  命中    营业收入 1,280,000 与申报销售额 964,000 偏离约 32.78%
    R-002  未执行  未提供进项转出、期初留抵、抵扣调整及一般计税税额
    R-003  未执行  未提供带来源的行业参考区间
    R-004  未执行  未提供社保参保数据
    R-005  命中    账面销项166,400与申报销项125,320相差41,080
    R-006  通过    账面与申报进项均为98,000
    其他新增规则缺少材料而未执行；完整96例见 scripts/check_rules.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

OUT = ROOT / "samples" / "样例企业-审计材料.xlsx"

COMPANY = {
    "企业名称": "东莞市启明商贸有限公司（仿真样例）",
    "纳税人识别号": "91441900MA5TEST0X0",
    "所属行业": "批发和零售业",
    "所属期": "2026-01-01 至 2026-06-30",
}

# 科目编码, 科目名称, 期初余额, 本期借方, 本期贷方, 期末余额
ACCOUNTS = [
    ("1001", "库存现金", 20_000, 5_000, 3_000, 22_000),
    ("1002", "银行存款", 480_000, 1_050_000, 980_000, 550_000),
    ("1122", "应收账款", 620_000, 1_280_000, 1_100_000, 800_000),
    ("1405", "库存商品", 350_000, 900_000, 780_000, 470_000),
    ("22210101", "应交税费—应交增值税—进项税额", 0, 98_000, 0, 0),
    ("22210105", "应交税费—应交增值税—销项税额", 0, 0, 166_400, 0),
    ("6001", "主营业务收入", 0, 0, 1_200_000, 0),
    ("6051", "其他业务收入", 0, 0, 80_000, 0),
    ("6401", "主营业务成本", 0, 780_000, 0, 0),
    ("6601", "销售费用", 0, 45_000, 0, 0),
    ("6602", "管理费用", 0, 96_000, 0, 0),
]

# 项目, 金额
DECLARATION = [
    ("销售额", 964_000),
    ("销项税额", 125_320),
    ("进项税额", 98_000),
    ("应纳税额", 27_320),
    ("期末留抵税额", 0),
]

NOTE = [
    ["填表说明"],
    [""],
    ["1. 本工作簿为仿真样例，全部数据由脚本生成，不含任何真实企业信息。"],
    ["2. 工作表「企业信息」为必填，用于报告封面。"],
    ["3. 工作表「科目余额表」列名须与模板完全一致，金额单位为元，不含千分位。"],
    ["4. 科目编码须落在系统映射表覆盖范围内，否则该科目不参与计算。"],
    ["5. 工作表「增值税申报」的「项目」须与系统约定的项目名一致。"],
    ["6. 所属期跨度与科目发生额口径需保持一致，否则勾稽规则会误报。"],
]

HEADER_FILL = PatternFill("solid", fgColor="DCE6F1")
HEADER_FONT = Font(bold=True)


def _style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"


def _set_widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build() -> Path:
    wb = Workbook()

    # ---------------- 企业信息 ----------------
    ws = wb.active
    ws.title = config.SHEET_COMPANY
    ws.append(["项目", "内容"])
    for k, v in COMPANY.items():
        ws.append([k, v])
    _style_header(ws, 2)
    _set_widths(ws, [20, 46])

    # ---------------- 科目余额表 ----------------
    ws = wb.create_sheet(config.SHEET_ACCOUNTS)
    ws.append(config.COL_ACCOUNTS)
    for row in ACCOUNTS:
        ws.append(list(row))
    _style_header(ws, len(config.COL_ACCOUNTS))
    _set_widths(ws, [14, 30, 14, 14, 14, 14])
    for r in range(2, len(ACCOUNTS) + 2):
        for c in range(3, 7):
            ws.cell(row=r, column=c).number_format = "#,##0.00"

    # ---------------- 增值税申报 ----------------
    ws = wb.create_sheet(config.SHEET_DECLARATION)
    ws.append(["项目", "金额"])
    for k, v in DECLARATION:
        ws.append([k, v])
    _style_header(ws, 2)
    _set_widths(ws, [22, 18])
    for r in range(2, len(DECLARATION) + 2):
        ws.cell(row=r, column=2).number_format = "#,##0.00"

    # ---------------- 填表说明 ----------------
    ws = wb.create_sheet("填表说明")
    for line in NOTE:
        ws.append(line)
    _set_widths(ws, [90])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    return OUT


if __name__ == "__main__":
    p = build()
    print(f"已生成：{p}")
