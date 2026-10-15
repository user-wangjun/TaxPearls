"""Typed, three-valued conditions over confirmed business facts; no code execution."""
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import json


TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
OPERATORS = {"=", "!=", "<", "<=", ">", ">=", "IN", "NOT IN"}
SOURCES = {
    "VATDeclaration": ("vat_declaration", "tax_period", {
        "declared_value", "item_key", "item_label", "form_type", "form_name", "column_scope"}),
    "Invoice": ("invoice_detail", "issue_date", {
        "amount_excluding_tax", "tax_amount", "amount_including_tax", "direction",
        "red_letter_status", "void_status", "invoice_no", "issue_date"}),
    "AccountBalanceRow": ("account_balance", "accounting_period", {
        "account_code", "account_name", "opening_balance", "current_debit", "current_credit", "closing_balance"}),
}


def validate_logic(logic, condition_ids, depth=0):
    if depth > 8 or not isinstance(logic, dict) or len(logic) != 1:
        raise ValueError("条件逻辑须为单键对象，嵌套不能超过八层。")
    op, args = next(iter(logic.items()))
    if op == "ref":
        if not isinstance(args, str) or args not in condition_ids:
            raise ValueError("条件引用必须指向本规则的 Condition。")
        return {args}
    if op == "not":
        return validate_logic(args, condition_ids, depth + 1)
    if op not in {"and", "or"} or not isinstance(args, list) or not 1 <= len(args) <= 64:
        raise ValueError("仅支持 ref、非空 AND/OR 和单项 NOT。")
    return set().union(*(validate_logic(item, condition_ids, depth + 1) for item in args))


def logic_value(logic, values):
    validate_logic(logic, set(values))

    def evaluate(item):
        op, args = next(iter(item.items()))
        if op == "ref":
            return values[args]
        if op == "not":
            return {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[evaluate(args)]
        children = [evaluate(child) for child in args]
        dominant, otherwise = (FALSE, TRUE) if op == "and" else (TRUE, FALSE)
        return dominant if dominant in children else UNKNOWN if UNKNOWN in children else otherwise

    return evaluate(logic)


def typed(value, value_type):
    if value is None:
        raise ValueError("缺少指标值。")
    if value_type == "decimal":
        if isinstance(value, (bool, float)):
            raise ValueError("金额应为精确十进制字符串。")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("指标不是有效十进制数。") from None
        if not number.is_finite() or abs(number) > Decimal("1e24") or number.as_tuple().exponent < -20:
            raise ValueError("数值超出支持范围。")
        return number
    if value_type == "boolean":
        if value is True or value == "true":
            return True
        if value is False or value == "false":
            return False
        raise ValueError("布尔指标只接受 true/false。")
    if not isinstance(value, str):
        raise ValueError("文本和日期指标须为字符串。")
    if value_type == "date":
        return date.fromisoformat(value)
    if value_type != "text":
        raise ValueError("未知的指标类型。")
    return value


def compare(condition, definitions, facts):
    left_def = definitions[condition["left_metric_id"]]
    left = facts.get(condition["left_metric_id"], {})
    try:
        if left.get("state") != "known" or left.get("unit") != left_def.get("unit"):
            return UNKNOWN
        a = typed(left.get("value"), left_def["value_type"])
        op = condition["operator"]
        if condition["right_kind"] == "metric":
            right_def = definitions[condition["right_metric_id"]]
            right = facts.get(condition["right_metric_id"], {})
            if (right.get("state") != "known" or right_def["value_type"] != left_def["value_type"]
                    or right_def.get("unit") != left_def.get("unit") or right.get("unit") != right_def.get("unit")):
                return UNKNOWN
            b = typed(right.get("value"), right_def["value_type"])
        elif op in {"IN", "NOT IN"}:
            b = [typed(v, left_def["value_type"]) for v in json.loads(condition["right_literal"])]
        else:
            b = typed(condition["right_literal"], left_def["value_type"])
        if condition.get("unit") != left_def.get("unit"):
            return UNKNOWN
        result = {"=": lambda: a == b, "!=": lambda: a != b, "<": lambda: a < b,
                  "<=": lambda: a <= b, ">": lambda: a > b, ">=": lambda: a >= b,
                  "IN": lambda: a in b, "NOT IN": lambda: a not in b}[op]()
        return TRUE if result else FALSE
    except (ValueError, InvalidOperation, TypeError, KeyError):
        return UNKNOWN


def validate_mapping(metric):
    """A pending mapping is allowed in a candidate, never at approval/execution."""
    schema = metric.get("source_schema")
    if schema not in SOURCES:
        raise ValueError("指标尚未配置受支持的材料来源。")
    _, period, fields = SOURCES[schema]
    filters = json.loads(metric.get("source_filter_json", "{}"))
    if (metric.get("source_field") not in fields or metric.get("period_basis") != period
            or not isinstance(filters, dict) or not set(filters) <= fields
            or any(not isinstance(v, (str, bool, int)) for v in filters.values())):
        raise ValueError("指标字段、过滤条件或期间口径无效。")
    if metric.get("aggregation", "direct") not in {"direct", "sum", "count"} or metric.get("calculation_code"):
        raise ValueError("派生指标须先接入审核过的函数注册表；不执行文本公式。")
    if metric.get("aggregation") in {"sum", "count"} and metric["value_type"] != "decimal":
        raise ValueError("汇总或计数指标须为数值类型。")
    if schema == "VATDeclaration" and not {"form_type", "form_name", "item_key", "column_scope"} <= filters.keys():
        raise ValueError("申报指标须明确主附表、表名、行次和本期/累计列。")
    if schema == "Invoice" and not {"direction", "red_letter_status", "void_status"} <= filters.keys():
        raise ValueError("发票指标须明确进销项、红字和作废口径。")


def resolve_metric(metric, processing, task):
    """Read existing core_materials.process records without making cross-form assumptions."""
    unknown = {"state": "unknown", "value": None, "unit": metric.get("unit"), "source_record_ids": []}
    try:
        validate_mapping(metric)
        if processing.get("status") != "ready_for_analysis" or processing.get("issues"):
            return {**unknown, "reason": "材料或覆盖范围尚未确认。"}
        start, end = date.fromisoformat(task["start_date"]), date.fromisoformat(task["end_date"])
        if start > end:
            raise ValueError("期间倒置。")
        filters = json.loads(metric.get("source_filter_json", "{}"))
        source_kind = SOURCES[metric["source_schema"]][0]
        selected = []
        for record in processing.get("records", []):
            if record.get("material_type") != source_kind:
                continue
            # Older single-column VAT records retain the exact column heading in
            # field_sources instead of column_scope. Read that evidence; never assume 本期数.
            if source_kind == "vat_declaration" and not record.get("column_scope"):
                source = record.get("field_sources", {}).get("declared_value", {})
                if source.get("target_field") == "declared_value" and source.get("source_label"):
                    record = {**record, "column_scope": source["source_label"]}
            if (record.get("tenant_id") != task["tenant_id"] or record.get("company_id") != task["company_id"]
                    or record.get("task_id") != task["id"]):
                return {**unknown, "reason": "资料企业、机构或任务归属不一致。"}
            if any(record.get(k) is None for k in filters):
                return {**unknown, "reason": "筛选所需字段未核实。"}
            if any(record[k] != v for k, v in filters.items()):
                continue
            if record.get("duplicate_of"):
                continue
            if not record.get("in_scope"):
                return {**unknown, "reason": "指标来源期间尚未对齐。"}
            if source_kind == "invoice_detail":
                day = date.fromisoformat(record["issue_date"])
                if not start <= day <= end:
                    return {**unknown, "reason": "开票日期不在本次期间。"}
            elif record.get("period_start") != task["start_date"] or record.get("period_end") != task["end_date"]:
                return {**unknown, "reason": "暂不自动拼接不同期间的申报或余额。"}
            if (metric.get("aggregation") != "count" and record.get("unit") != metric.get("unit")) or not record.get("id"):
                return {**unknown, "reason": "金额单位不一致或缺少来源编号。"}
            if record["id"] in {r["id"] for r in selected}:
                return {**unknown, "reason": "重复来源编号尚未核对。"}
            selected.append(record)
        if not selected or (metric.get("aggregation", "direct") == "direct" and len(selected) != 1):
            return {**unknown, "reason": "未找到唯一来源或匹配记录为空，未视为零。"}
        field = metric["source_field"]
        if any(r.get("value_states", {}).get(field) != "present" for r in selected):
            return {**unknown, "reason": "指标字段缺失或无效。"}
        aggregation = metric.get("aggregation", "direct")
        values = [typed(r.get(field), metric["value_type"]) for r in selected] if aggregation != "count" else []
        with localcontext() as context:
            context.prec = 64
            value = len(selected) if aggregation == "count" else sum(values, Decimal(0)) if aggregation == "sum" else values[0]
        return {"state": "known", "value": str(value) if isinstance(value, (Decimal, date)) else value,
                "unit": metric.get("unit"), "source_record_ids": [r["id"] for r in selected]}
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return {**unknown, "reason": "指标配置或来源字段未通过校验。"}
