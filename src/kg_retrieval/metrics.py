"""Per-check material readiness and invalidation without blocking unrelated metrics."""
import json
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
        if code == "invoice_coverage_gap" and kind == "invoice_detail":
            direction = json.loads(definition.get("source_filter_json", "{}")).get("direction")
            if direction == "output" and issue["message"].startswith("进项") or direction == "input" and issue["message"].startswith("销项"):
                continue
        if code in {"forms_unconfirmed", "form_coverage_gap", "main_form_gap", "missing_form"} and kind != "vat_declaration":
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


def build_task_bundle(db, payload, task, rule_id, **options):
    from . import bundle, relation_search
    from webapp.core_inputs import require_scope
    require_scope(payload)
    from src import vat_reconciliation as vat
    definitions = relation_search.rule_context(db, rule_id)["metrics"]
    fact_assessment = None
    task = {**task, "period_start": payload["task"]["start_date"], "period_end": payload["task"]["end_date"],
            "data_scope": "synthetic" if any(marker in payload["task"]["company_name"] for marker in ("纯合成测试", "仿真")) else "enterprise"}
    if rule_id == vat.RULE:
        definitions, facts, checked = vat.resolve(definitions, payload)
        fact_assessment = vat.assess(facts, checked)
        if checked:
            task["comparability"] = {vat.RULE + ":difference": {"reviewed": checked["comparable"], "basis": checked["basis"]}}
    else:
        facts = collect(definitions, payload["processing"], payload["task"])
    catalog = [r["id"] for r in payload["processing"].get("records", [])]
    catalog += ["file:" + d["id"] for d in payload.get("documents", [])]
    from src.supporting_materials import evidence
    return bundle.build(db, task, rule_id, facts, source_catalog=catalog, metric_definitions=definitions,
                        supporting_materials=evidence(payload),
                        fact_assessment=fact_assessment, **options)


def mark_stale(payload):
    from src.supporting_materials import evidence
    for slot in payload.get("tax_checks", {}).values():
        package = slot.get("bundle", {})
        definitions = package.get("metric_definitions", {})
        from src import vat_reconciliation as vat
        current = vat.resolve(definitions, payload)[1] if package.get("check", {}).get("rule_id") == vat.RULE else collect(definitions, payload["processing"], payload["task"])
        previous = {f["metric_id"]: f for f in package.get("facts", [])}
        scope = {key: payload["task"].get(key) for key in core_materials.SCOPE_FIELDS}
        scope_changed = slot.get("input_scope", scope) != scope
        period_changed = any(package.get("task", {}).get(key, payload["task"][source]) != payload["task"][source]
                             for key, source in (("period_start", "start_date"), ("period_end", "end_date")))
        supporting_changed = evidence(payload) != package.get("supporting_materials", [])
        slot["stale"] = scope_changed or period_changed or supporting_changed or signature(current) != signature(previous)
    return payload
