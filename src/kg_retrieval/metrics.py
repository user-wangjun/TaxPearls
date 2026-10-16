"""Per-check material readiness and invalidation without blocking unrelated metrics."""
from src import core_materials, tax_conditions
from .common import digest, unit, period_basis


def resolve(definition, processing, task):
    if definition.get('source_schema') == 'BusinessMetric':
        from src.rule_bindings import resolve
        return resolve(definition, processing, task)
    source = tax_conditions.SOURCES.get(definition.get("source_schema"))
    if not source:
        return {"state": "unknown", "value": None, "unit": definition.get("unit"), "reason": "指标映射尚未配置。", "source_record_ids": []}
    kind = source[0]
    relevant_files = {r["source_file_id"] for r in processing.get("records", []) if r.get("material_type") == kind}
    problems = []
    for issue in [*processing.get("issues", []), *[n for n in processing.get("notices", []) if n["code"] == "identity_unverified"]]:
        file_id, code = issue.get("source_file_id"), issue["code"]
        if file_id and file_id not in relevant_files:
            continue
        if code in {"missing_material", "coverage_gap"} and core_materials.KINDS[kind] not in issue["message"]:
            continue
        if code in {"invoice_coverage_gap", "direction_missing"} and kind != "invoice_detail":
            continue
        if code in {"forms_unconfirmed", "form_coverage_gap", "main_form_gap"} and kind != "vat_declaration":
            continue
        problems.append(issue)
    parsing = processing.get("status") in {"parsing", "processing"}
    scoped = {**processing, "issues": problems, "status": "needs_completion" if problems or parsing else "ready_for_analysis"}
    return tax_conditions.resolve_metric(definition, scoped, task)


def collect(definitions, processing, task):
    result = {}
    for mid, definition in definitions.items():
        if definition.get('source_schema') == 'RuleCalculation':
            continue
        fact = resolve(definition, processing, task)
        result[mid] = {**fact, "period_basis": definition.get("period_basis"),
                       "period_start": task["start_date"], "period_end": task["end_date"]}
    from src.rule_conversion import calculate
    for mid, definition in definitions.items():
        if definition.get('source_schema') == 'RuleCalculation':
            result[mid] = {**calculate(definition, result, task=task), 'period_basis':'rule_period',
                           'period_start':task['start_date'], 'period_end':task['end_date']}
    return result


def signature(facts):
    return digest({mid: {"state": fact.get("state"), "value": str(fact.get("value")) if fact.get("value") is not None else None,
                          "unit": unit(fact.get("unit")), "period_basis": period_basis(fact.get("period_basis")),
                          "source_ids": sorted(fact.get("source_ids", fact.get("source_record_ids", []))),
                          **{key:fact.get(key) for key in ('period_start','period_end','metric_period',
                                                         'reference','company_scope','simulation')}}
                   for mid, fact in facts.items()})


def mark_stale(payload):
    for slot in payload.get("tax_checks", {}).values():
        package = slot.get("bundle", {})
        definitions = package.get("metric_definitions", {})
        current = collect(definitions, payload["processing"], payload["task"])
        previous = {f["metric_id"]: f for f in package.get("facts", [])}
        slot["stale"] = signature(current) != signature(previous)
    return payload
