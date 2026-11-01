"""VAT-R01 facts over retained materials; policy risk remains a separate result."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json

from src.kg_retrieval.common import digest

RULE = "VAT-R01"
INVOICE = "M_INV_OUT_EXCL"
DECLARATION = "M_DECL_SALES"
BLUE = ["蓝字", "蓝票", "正常", "false", "False", "0", "blue"]
RED = ["红字", "红票", "true", "True", "1", "red"]
VALID = ["正常", "未作废", "有效", "false", "False", "0", "valid", "normal"]
VOID = ["作废", "已作废", "true", "True", "1", "void"]


def initialize(db):
    """Install a candidate check card, never fabricate policy approval or form lines."""
    if db.execute("SELECT 1 FROM TaxRule WHERE rule_id=?", (RULE,)).fetchone():
        return  # Never overwrite or add dependencies to an existing reviewed card.
    db.execute("INSERT OR IGNORE INTO TaxRule(rule_id,rule_code,name,tax_type,rule_type,outcome_text,notes) "
               "VALUES (?,?,?,?,?,?,?)", (RULE, RULE, "销项发票与申报销售额差异", "VAT", "risk_screen",
               "仅确认已核实口径下的数值差异；差异不等于少缴税或违法。", "企业资料核对检查；政策适用与表单映射须另行核实。"))
    for mid, name, schema, field, aggregation, period in [
        (INVOICE, "销项发票不含税金额", "Invoice", "amount_excluding_tax", "sum", "issue_date"),
        (DECLARATION, "申报销售额", "VATDeclaration", "declared_value", "direct", "tax_period"),
    ]:
        db.execute("INSERT OR IGNORE INTO Metric(metric_id,metric_code,name,value_type,unit,source_schema,source_field,aggregation,period_basis,definition) "
                   "VALUES (?,?,?,'decimal','元',?,?,?,?,?)", (mid, mid, name, schema, field, aggregation, period,
                   "须由当前任务已核对的来源及口径绑定；不按同名字段猜测。"))
        db.execute("INSERT OR IGNORE INTO Relationship(relationship_id,from_type,from_id,relation_type,to_type,to_id) "
                   "VALUES (?,'TaxRule',?,'USES_METRIC','Metric',?)", (RULE + ":" + mid, RULE, mid))
    db.execute("INSERT OR IGNORE INTO Condition(condition_id,rule_id,left_metric_id,operator,right_kind,right_metric_id,unit,condition_source) "
               "VALUES (?,?,?,'!=','metric',?,'元','internal_screening')", (RULE + ":difference", RULE, INVOICE, DECLARATION))
    db.execute("UPDATE TaxRule SET condition_logic_json=? WHERE rule_id=? AND condition_logic_json='{}'",
               (json.dumps({"ref": RULE + ":difference"}), RULE))


def source_signature(payload):
    # Binding includes coverage, identity conflicts, exclusions and exact original
    # hashes; unrelated account uploads do not invalidate invoice/return review.
    documents = [d for d in payload.get("documents", []) if d["material_type"] in {"invoice_detail", "vat_declaration"}]
    return digest({"documents": documents, "period": [payload["task"][k] for k in ("start_date", "end_date")],
                   "forms": payload["task"].get("applicable_forms"),
                   "audit_metrics": payload['task'].get('audit_metric_records'),
                   "audit_source_signature": payload['task'].get('audit_metric_source_signature')})


def frozen_declaration(payload, row):
    """Only a server-authorized frozen declaration metric can bridge periods."""
    from src import rule_bindings
    if (not row or row.get('material_type') != 'business_metric' or not row.get('audit_id')
            or row.get('metric_name') != '增值税.销售额' or not row.get('field_sources', {}).get('value')):
        return False
    task = payload['task']
    frozen = next((r for r in task.get('audit_metric_records', []) if r['id'] == row['id']), None)
    if (not frozen or any(row.get(k) != v for k, v in frozen.items())
            or task.get('audit_metric_source_signature') != rule_bindings.material_signature(payload['processing'])):
        return False
    fact = rule_bindings.resolve({'unit':'元', 'source_filter_json':json.dumps({'name':'增值税.销售额'})},
                                 payload['processing'], task)
    return fact['state'] == 'known' and fact['source_record_ids'] == [row['id']]


def review(payload):
    value = payload.get("check_reviews", {}).get(RULE, {})
    return value if value.get("source_signature") == source_signature(payload) else {}


def make_review(payload, values, actor, stamp):
    allowed = {"mapping_record_id", "form_revision", "taxpayer_category", "comparable", "basis", "zero_proof_file_id"}
    if set(values) - allowed or type(values.get("comparable")) is not bool:
        raise ValueError("请明确本项目可比性，不能提交数值或未知核对字段。")
    for field in ("form_revision", "taxpayer_category", "basis"):
        if not isinstance(values.get(field), str) or not 1 <= len(values[field].strip()) <= 500:
            raise ValueError("请填写已核实的表单版本、纳税人类型和口径依据；未知时保留待核对。")
    rows = payload["processing"].get("records", [])
    row = next((r for r in rows if r["id"] == values.get("mapping_record_id")), None)
    native = row and row.get('material_type') == 'vat_declaration' and row.get('field_sources', {}).get('declared_value')
    if not native and not frozen_declaration(payload, row):
        raise ValueError("请选择本任务有原表定位的申报销售额记录。")
    proof = values.get("zero_proof_file_id") or ""
    if proof and not any(d["id"] == proof and d["material_type"] == "invoice_detail" and not d.get("records")
                         for d in payload.get("documents", [])):
        raise ValueError("零发生额核对须绑定本任务的空发票明细原件，不能仅用勾选或空集合证明。")
    return {**deepcopy(values), "source_signature": source_signature(payload), "reviewed_by": actor,
            "reviewed_at": stamp, "review_scope": "task_source_mapping_not_policy_approval"}


def resolve(definitions, payload):
    from src.kg_retrieval import metrics
    checked = review(payload)
    definitions = deepcopy(definitions)
    definitions[INVOICE]["source_filter_json"] = json.dumps({"direction": "output", "red_letter_status": BLUE + RED, "void_status": VALID})
    rows = payload["processing"].get("records", [])
    selected = next((r for r in rows if r["id"] == checked.get("mapping_record_id")), None)
    if frozen_declaration(payload, selected):
        definitions[DECLARATION].update(source_schema='BusinessMetric', source_field='value', aggregation='direct',
            source_filter_json=json.dumps({'name':'增值税.销售额'}))
    elif selected and selected.get('material_type') == 'vat_declaration':
        heading = selected.get("column_scope") or selected.get("field_sources", {}).get("declared_value", {}).get("source_label")
        definitions[DECLARATION]["source_filter_json"] = json.dumps({k: selected.get(k) for k in ("form_type", "form_name", "item_key")} | {"column_scope": heading})
    else:
        checked = {}
    facts = metrics.collect(definitions, payload["processing"], payload["task"])
    if any(d["material_type"] == "invoice_detail" and d.get("issues") for d in payload.get("documents", [])):
        facts[INVOICE].update(state="unknown", value=None, reason="发票来源仍含解析或字段缺口，不能只汇总成功解析的部分。")
    if not checked:
        facts[DECLARATION].update(state="unknown", value=None, source_record_ids=[], reason="申报版本、取数栏次及比较口径尚未核对，或材料已变化。")
    for row in rows:
        if row.get("material_type") != "invoice_detail" or row.get("direction") != "output" or not row.get("in_scope"):
            continue
        if row.get("void_status") in VOID:
            continue
        reason = ""
        if row.get("void_status") not in VALID or row.get("red_letter_status") not in BLUE + RED:
            reason = "发票红字或作废状态未知，不能静默剔除后形成完整合计。"
        elif row.get("value_states", {}).get("amount_excluding_tax") == "present":
            amount = Decimal(row["amount_excluding_tax"])
            if row["red_letter_status"] in RED and amount > 0 or row["red_letter_status"] in BLUE and amount < 0:
                reason = "红蓝字与金额符号不一致，须核对原件，不能自行改号。"
        if reason:
            facts[INVOICE].update(state="unknown", value=None, reason=reason, quality_state="ambiguous")
    proof = checked.get("zero_proof_file_id")
    if proof and not any(r.get("material_type") == "invoice_detail" for r in rows):
        documents = [d for d in payload["documents"] if d["material_type"] == "invoice_detail"]
        parts = [p for d in documents for p in d.get("parts", [])]
        task = payload["task"]
        if documents and all(d.get("parts") and not d.get("issues") for d in documents) and parts and all(p.get("empty_table") is True and p.get("period") == [task["start_date"], task["end_date"]] and p.get("tax_nos") == [task["tax_no"]]
                         and p.get("unit") == "元" for p in parts):
            facts[INVOICE].update(state="known", value="0.00", source_record_ids=["file:" + proof], reason="空明细原件及同期间零发生额由核对人确认。")
    return definitions, facts, checked


def assess(facts, checked):
    ids = sorted({sid for f in facts.values() for sid in f.get("source_record_ids", f.get("source_ids", []))})
    base = {"status": "FACT_INCOMPLETE", "comparison_status": "incomplete", "amount": None, "unit": "元",
            "source_ids": ids, "calculation_trace": [], "mapping_review": deepcopy(checked)}
    if any(f.get("state") != "known" for f in facts.values()):
        return {**base, "status": "FACT_UNVERIFIED"} if any(f.get("quality_state") == "ambiguous" for f in facts.values()) else base
    if not checked.get("comparable"):
        return {**base, "status": "NOT_COMPARABLE", "comparison_status": "not_comparable"}
    with localcontext() as context:
        context.prec = 64
        left, right = Decimal(facts[INVOICE]["value"]), Decimal(facts[DECLARATION]["value"])
        difference = left - right
    return {**base, "status": "FACT_CONFIRMED", "comparison_status": "comparable", "amount": str(difference),
            "calculation_trace": [{"operation": "subtract", "left_metric_id": INVOICE, "left_value": str(left),
                                   "right_metric_id": DECLARATION, "right_value": str(right), "result": str(difference),
                                   "definition": "销项发票不含税金额减申报销售额；不是应补税额。"}]}
