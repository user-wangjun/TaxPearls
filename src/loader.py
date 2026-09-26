"""审计材料解析与标准化。

流程：Excel → 原始科目 / 申报数据 → 按 config 的映射聚合成标准指标。
每个指标记录 source 与 detail，供报告举证。
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from . import config
from .models import Account, Company, Dataset, Metric


class InputError(Exception):
    """输入材料不符合模板要求。"""


def _require_sheet(wb, name: str):
    if name not in wb.sheetnames:
        raise InputError(
            f"缺少工作表「{name}」。当前工作表：{wb.sheetnames}"
        )
    return wb[name]


def _read_company(wb) -> Company:
    ws = _require_sheet(wb, config.SHEET_COMPANY)
    data: dict[str, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        data[str(row[0]).strip()] = "" if row[1] is None else str(row[1]).strip()
    missing = [f for f in config.COMPANY_FIELDS if not data.get(f)]
    if missing:
        raise InputError(f"「{config.SHEET_COMPANY}」缺少字段：{missing}")
    return Company(
        name=data["企业名称"],
        taxpayer_id=data["纳税人识别号"],
        industry=data["所属行业"],
        period=data["所属期"],
    )


def _read_accounts(wb) -> list[Account]:
    ws = _require_sheet(wb, config.SHEET_ACCOUNTS)
    header = [c.value for c in ws[1]]
    if header[: len(config.COL_ACCOUNTS)] != config.COL_ACCOUNTS:
        raise InputError(
            f"「{config.SHEET_ACCOUNTS}」表头不符。\n"
            f"期望：{config.COL_ACCOUNTS}\n实际：{header}"
        )
    out: list[Account] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        code = str(row[0]).strip()
        out.append(
            Account(
                code=code,
                name=str(row[1] or "").strip(),
                opening=float(row[2] or 0),
                debit=float(row[3] or 0),
                credit=float(row[4] or 0),
                closing=float(row[5] or 0),
            )
        )
    if not out:
        raise InputError(f"「{config.SHEET_ACCOUNTS}」没有数据行。")
    return out


def _read_declarations(wb) -> dict[str, float]:
    ws = _require_sheet(wb, config.SHEET_DECLARATION)
    raw: dict[str, float] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        raw[str(row[0]).strip()] = float(row[1] or 0)
    return raw


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _build_metrics(
    accounts: list[Account], declarations: dict[str, float]
) -> dict[str, Metric]:
    metrics: dict[str, Metric] = {}

    # --- 来自科目余额表 ---
    for metric_name, spec in config.ACCOUNT_MAP.items():
        parts: list[str] = []
        total = 0.0
        for code in spec["accounts"]:
            acc = next((a for a in accounts if a.code == code), None)
            if acc is None:
                continue
            amount = acc.credit if spec["side"] == "credit" else acc.debit
            total += amount
            parts.append(f"{code} {acc.name} {_money(amount)}")
        if not parts:
            continue
        metrics[metric_name] = Metric(
            name=metric_name,
            value=total,
            source=spec["label"],
            detail=" + ".join(parts),
        )

    # --- 来自增值税申报表 ---
    for metric_name, item in config.DECLARATION_ITEMS.items():
        if item not in declarations:
            continue
        metrics[metric_name] = Metric(
            name=metric_name,
            value=declarations[item],
            source=f"增值税申报表—{item}",
            detail=f"{item} {_money(declarations[item])}",
        )

    # --- 派生指标 ---
    for metric_name, spec in config.DERIVED.items():
        deps = spec["deps"]
        vals = [metrics[d].value for d in deps if d in metrics]
        if len(vals) != len(deps):
            continue
        metrics[metric_name] = Metric(
            name=metric_name,
            value=spec["fn"](*vals),
            source=spec["source"],
            detail=spec["detail"].format(a=_money(vals[0]), b=_money(vals[1])),
        )

    return metrics


def load(path: str | Path) -> Dataset:
    """读取审计材料 Excel，返回标准化后的 Dataset。"""
    path = Path(path)
    if not path.exists():
        raise InputError(f"文件不存在：{path}")

    wb = load_workbook(path, data_only=True)
    company = _read_company(wb)
    accounts = _read_accounts(wb)
    declarations = _read_declarations(wb)
    metrics = _build_metrics(accounts, declarations)

    if not metrics:
        raise InputError("未能从材料中提取到任何指标，请检查科目编码是否在映射表覆盖范围内。")

    return Dataset(
        company=company,
        accounts=accounts,
        declarations=declarations,
        metrics=metrics,
    )
