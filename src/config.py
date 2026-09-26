"""科目映射与行业参考值。

这里是「标准化」落地的位置：各家财务软件的科目编码与口径不同，
统一在此映射为标准指标，规则引擎只认标准指标，不认原始科目。

⚠️ 行业参考值（税负率区间等）为演示用示意值，正式使用前须按
   当地税务机关公布的行业预警值 / 企业实际经营情况校准。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 工作表名
SHEET_COMPANY = "企业信息"
SHEET_ACCOUNTS = "科目余额表"
SHEET_DECLARATION = "增值税申报"

# 科目余额表列名
COL_ACCOUNTS = ["科目编码", "科目名称", "期初余额", "本期借方", "本期贷方", "期末余额"]

# 企业信息字段
COMPANY_FIELDS = ["企业名称", "纳税人识别号", "所属行业", "所属期"]

# ---------------------------------------------------------------- 科目 -> 标准指标
# side: debit = 取本期借方发生额, credit = 取本期贷方发生额
ACCOUNT_MAP: dict[str, dict] = {
    "营业收入": {
        "side": "credit",
        "accounts": ["6001", "6051"],
        "label": "6001 + 6051 本期贷方发生额",
    },
    "营业成本": {
        "side": "debit",
        "accounts": ["6401", "6402"],
        "label": "6401 + 6402 本期借方发生额",
    },
    "账面.销项税额": {
        "side": "credit",
        "accounts": ["22210105"],
        "label": "22210105 本期贷方发生额",
    },
    "账面.进项税额": {
        "side": "debit",
        "accounts": ["22210101"],
        "label": "22210101 本期借方发生额",
    },
}

# ---------------------------------------------------------------- 申报表 -> 标准指标
DECLARATION_ITEMS: dict[str, str] = {
    "增值税.销售额": "销售额",
    "增值税.销项税额": "销项税额",
    "增值税.进项税额": "进项税额",
    "增值税.应纳税额": "应纳税额",
    "增值税.期末留抵税额": "期末留抵税额",
}

# ---------------------------------------------------------------- 派生指标
DERIVED: dict[str, dict] = {
    "账面.应纳税额": {
        "deps": ["账面.销项税额", "账面.进项税额"],
        "fn": lambda a, b: a - b,
        "source": "账面销项税额 − 账面进项税额",
        "detail": "账面销项税额 {a} − 账面进项税额 {b}",
    },
}

# ---------------------------------------------------------------- 行业参考值
INDUSTRY_REFERENCE: dict[str, dict] = {
    "批发和零售业": {"tax_burden_min": 0.010, "tax_burden_max": 0.045},
    "制造业": {"tax_burden_min": 0.020, "tax_burden_max": 0.060},
    "建筑业": {"tax_burden_min": 0.020, "tax_burden_max": 0.055},
    "服务业": {"tax_burden_min": 0.015, "tax_burden_max": 0.050},
    "default": {"tax_burden_min": 0.010, "tax_burden_max": 0.050},
}


def industry_reference(industry: str) -> dict:
    return INDUSTRY_REFERENCE.get(industry, INDUSTRY_REFERENCE["default"])
