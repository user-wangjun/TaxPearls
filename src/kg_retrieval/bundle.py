"""Hybrid retrieval, deterministic checks and complete, auditable AI evidence packages."""
from copy import deepcopy
import json
from time import perf_counter

from src import tax_conditions
from src.policy_paths import database_directory
from . import applicability, keyword_search, relation_search, semantic_search, passages
from .common import canonical, checked_task, clause_record, digest, period_basis, unit


def content_hash(package):
    """Compare evidence content, including legacy bundles with unordered review gaps."""
    core = {key: package[key] for key in ("task", "check", "facts", "metric_definitions", "conditions", "exceptions",
            "policy_evidence", "policy_leads", "relationships", "gaps", "fact_assessment", "risk_assessment")}
    core["gaps"] = sorted(core["gaps"], key=canonical)
    core["relationships"] = sorted(core["relationships"], key=canonical)
    return digest({**core, "token_budget": package["batching"]["soft_token_budget"]})


def fuse(routes, *, k=60):
    combined = {}
    for route, result in routes.items():
        for rank, hit in enumerate(result.get("hits", []), 1):
            key = (hit["clause_id"], hit.get("version"), hit.get("effective_from"), hit.get("effective_to"))
            current = combined.setdefault(key, {"clause_id": hit["clause_id"], "version": hit.get("version"),
                                                 "rrf_score": 0, "matches": []})
            current["rrf_score"] += 1 / (k + rank)
            current["matches"].append({**hit, "rank": rank, "route": route})
    return sorted(combined.values(), key=lambda row: (-row["rrf_score"], row["clause_id"]))


def _relevant(item, best_semantic, best_keyword):
    generic = {"增值税", "纳税人", "税务", "企业", "政策", "本期", "检查", "有关规定"}
    for match in item["matches"]:
        if match["route"] == "relation":
            return True
        if match["route"] == "keyword":
            specific = [term for term in match["reason"]["matched_terms"] if term not in generic]
            if specific and (match["reason"]["mode"] == "short_term_like" or match["score"] >= best_keyword * .25
                             or max(map(len, specific)) >= 5):
                return True
        if match["route"] == "semantic" and match["score"] >= max(0.60, best_semantic - 0.06):
            return True
    return False


def _facts(context, supplied, task, source_catalog):
    result, gaps = {}, []
    allowed = set(source_catalog)
    for mid, definition in context["metrics"].items():
        fact = deepcopy(supplied.get(mid) or {})
        source_ids = fact.get("source_ids", fact.get("source_record_ids", []))
        known = fact.get("state") == "known"
        reason = fact.get("reason", "")
        if not isinstance(source_ids, list) or not source_ids or any(sid not in allowed for sid in source_ids):
            known, reason = False, "指标来源 ID 缺失或不在本次可访问材料中。"
        if unit(fact.get("unit")) != unit(definition.get("unit")):
            known, reason = False, "指标单位与定义不一致。"
        basis = period_basis(fact.get("period_basis", definition.get("period_basis")))
        if basis != period_basis(definition.get("period_basis")) or not basis:
            known, reason = False, "指标期间口径尚未确认。"
        if fact.get("period_start", task["period_start"]) != task["period_start"] or fact.get("period_end", task["period_end"]) != task["period_end"]:
            known, reason = False, "指标所属期间与本次检查不一致。"
        try:
            value = tax_conditions.typed(fact.get("value"), definition["value_type"])
            if definition["value_type"] in {"decimal", "date"}:
                value = str(value)
        except (ValueError, TypeError, KeyError):
            known, value, reason = False, None, "指标值缺失或类型无效。"
        if not known:
            gaps.append({"code": "metric_missing", "metric_id": mid, "message": reason or "检查所需指标尚未取得。"})
        result[mid] = {"metric_id": mid, "name": definition["name"], "state": "known" if known else "unknown",
                       "value": value if known else None, "unit": unit(definition.get("unit")), "period_basis": basis,
                       "period_start": task["period_start"], "period_end": task["period_end"],
                       "source_ids": source_ids if isinstance(source_ids, list) and all(sid in allowed for sid in source_ids) else [], "reason": reason}
        if supplied.get(mid, {}).get('calculation'):
            result[mid]['calculation']=supplied[mid]['calculation']
        for key in ('metric_period','reference','company_scope','simulation'):
            if key in fact:
                result[mid][key]=deepcopy(fact[key])
        result[mid]['is_calculation']=definition.get('source_schema')=='RuleCalculation'
    return result, gaps


def _conditions(context, facts, task):
    definitions = {key: {**d, "unit": unit(d.get("unit"))} for key, d in context["metrics"].items()}
    values, output, gaps = {}, [], []
    for condition in context["conditions"]:
        normalized = {**condition, "unit": unit(condition.get("unit"))}
        value = tax_conditions.compare(normalized, definitions, facts)
        reason = ""
        right = condition.get("right_metric_id")
        if right:
            left_def, right_def = definitions[condition["left_metric_id"]], definitions[right]
            differing = (left_def.get("source_schema"), period_basis(left_def.get("period_basis"))) != (
                right_def.get("source_schema"), period_basis(right_def.get("period_basis")))
            confirmation = task.get("comparability", {}).get(condition["condition_id"], {})
            if differing and not (confirmation.get("reviewed") is True and confirmation.get("basis")):
                value, reason = "UNKNOWN", "跨材料指标的统计/调整口径尚未复核，数额不同不直接认定风险。"
                gaps.append({"code": "comparability_pending", "condition_id": condition["condition_id"], "message": reason})
        values[condition["condition_id"]] = value
        output.append({**normalized, "evaluation": value, "reason": reason or ("所需指标缺失或无效。" if value == "UNKNOWN" else "由程序精确比较。")})
    exceptions = []
    for item in context["exceptions"]:
        try:
            trigger = tax_conditions.logic_value(json.loads(item["trigger_logic_json"]), values)
        except (ValueError, KeyError, TypeError):
            trigger = "UNKNOWN"
        exceptions.append({**item, "trigger_state": trigger,
                           "source_clause_ids": [item["source_clause_id"]] if item.get("source_clause_id") else []})
        if trigger == "UNKNOWN" or trigger == "TRUE" and item["effect"] in {"needs_review", "adjust_outcome"}:
            gaps.append({"code": "exception_review", "exception_id": item["exception_id"], "message": "例外未知或要求人工复核。"})
    return output, exceptions, values, gaps


def split_bundle(bundle, token_budget=6000):
    from .batching import split
    return split(bundle, token_budget)


def build(db, task, rule_id, supplied_facts, *, topic="", keywords=(), source_catalog=(), model_dir=None,
          candidate_limit=20, review_limit=30, token_budget=6000, offline=True, metric_definitions=None, fact_assessment=None):
    started = perf_counter()
    task = checked_task(task)
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    context = relation_search.rule_context(db, rule_id)
    if metric_definitions is not None:
        if set(metric_definitions) != set(context["metrics"]):
            raise ValueError("任务指标定义必须对应同一规则的指标集合。")
        context["metrics"] = deepcopy(metric_definitions)
    rule = context["rule"]
    topic = topic.strip() or rule["name"]
    if not topic or len(topic) > 2000 or not 1 <= review_limit <= 100:
        raise ValueError("检查主题或核查候选数无效。")
    routes = {
        "relation": relation_search.search(db, rule_id, limit=candidate_limit),
        "keyword": keyword_search.search(db, topic, keywords=keywords, limit=candidate_limit),
        "semantic": semantic_search.search(db, topic, limit=candidate_limit, directory=model_dir),
    }
    combined = fuse(routes)
    best_semantic = max((hit["score"] for hit in routes["semantic"].get("hits", [])), default=0)
    best_keyword = max((hit["score"] for hit in routes["keyword"].get("hits", [])), default=0)
    selected, excluded = [], []
    for item in combined:
        if _relevant(item, best_semantic, best_keyword):
            selected.append(item)
        else:
            excluded.append({"clause_id": item["clause_id"], "stage": "relevance", "reasons": ["与当前主题的本地相关性不足。"]})
    selected = selected[:review_limit]
    selected_ids = {row["clause_id"] for row in selected}
    # Required dependencies bypass both route top-k and review_limit.
    required = set(context["required"])
    closure = relation_search.dependency_closure(db, selected_ids | required)
    # Preserve all required, parent and change-chain sources. Only optional
    # retrieval matches with reliable structural boundaries may be excerpted.
    dependency_ids = set(closure['clause_ids']) - selected_ids
    dependency_ids |= {edge[key] for edge in closure['relationships'] for key in ('from_id', 'to_id', 'evidence_clause_id')
                       if edge.get(key)}
    for cid in closure['clause_ids']:
        clause, _ = clause_record(db, cid)
        if clause.get('parent_clause_id'):
            dependency_ids.add(clause['parent_clause_id'])
    facts, gaps = _facts(context, supplied_facts, task, source_catalog)
    conditions, exceptions, truth_values, condition_gaps = _conditions(context, facts, task)
    gaps += [*condition_gaps, *closure["gaps"]]
    if rule["review_status"] != "approved" or not rule.get("reviewed_by") or not rule.get("reviewed_at"):
        gaps.append({"code": "rule_unapproved", "rule_id": rule_id, "message": "TaxRule 版本尚未审核。"})
    if not rule.get("valid_from") or rule["valid_from"] > task["period_start"] or rule.get("valid_to") and rule["valid_to"] < task["period_end"]:
        gaps.append({"code": "rule_period", "message": "规则有效期不能覆盖完整检查期间。"})
    if applicability.region_matches(rule.get("applicable_region"), task.get("region_code")) is not True:
        gaps.append({"code": "rule_region", "message": "规则适用地区不明确或不匹配。"})
    for edge in context["relationships"]:
        if edge["review_status"] != "approved":
            gaps.append({"code": "relationship_pending", "relationship_id": edge["relationship_id"], "message": "规则关系尚未审核。"})
        if edge["relation_type"] == "APPLIES_TO" and edge["to_id"] not in task.get("entity_ids", []):
            gaps.append({"code": "entity_pending", "entity_id": edge["to_id"], "message": "适用主体尚未核实。"})
    evidence, leads, decisions, source_cache = [], [], {}, {}
    matches = {item["clause_id"]: item["matches"] for item in selected}
    for cid in closure["clause_ids"]:
        clause, policy = clause_record(db, cid)
        decision = applicability.check(clause, policy, task, closure["relationships"], source_cache=source_cache,
                                       source_root=database_directory(db))
        paths = context["required"].get(cid, [])
        if paths and all(path.get("review_status", "approved") != "approved" for path in paths):
            decision["status"] = "needs_review"
            decision["reasons"].append("与本检查的关联关系尚未审核。")
        decisions[cid] = decision
        record = {"clause_id": cid, "policy_id": policy["policy_id"], "clause_version": clause.get("version"),
                  "policy_version": policy.get("version"), "document_no": policy.get("document_no"),
                  "title": policy["title"], "issuer": policy["issuer"], "text": clause["content"],
                  "source_location": clause.get("source_location"), "source_url": policy["source_url"],
                  "file_sha256": policy.get("file_sha256"), "region_code": policy["region_code"],
                  "effective_from": decision["effective_from"], "effective_to": decision["effective_to"],
                  "applicability": decision["status"], "reasons": decision["reasons"], "segments": decision["segments"],
                  "required": cid in required, "retrieval_matches": matches.get(cid, []),
                  "review_status": clause["extraction_status"]}
        selection = passages.select(clause['content'], matches.get(cid, []),
                                    required=cid in required, dependency=cid in dependency_ids)
        record.update(selection)
        if decision["status"] == "excluded":
            excluded.append({"clause_id": cid, "stage": "applicability", "reasons": decision["reasons"]})
            if cid in required:
                gaps.append({"code": "required_clause_inapplicable", "clause_id": cid, "message": ";".join(decision["reasons"])})
        elif decision["status"] == "needs_review":
            leads.append(record)
            gaps.append({"code": "policy_review", "clause_id": cid, "message": ";".join(decision["reasons"])})
        else:
            evidence.append(record)
            if cid in required and decision["status"] == "context_only" and decision.get('context_kind')!='supporting_basis':
                gaps.append({"code": "rule_source_changed", "clause_id": cid, "message": "本规则的原依据已修改，仅保留历史上下文，须复核执行版本。"})
        if decision['status'] != 'excluded' and selection.get('partial_clause'):
            gaps.append({'code': 'retrieval_context_partial', 'clause_id': cid,
                         'message': '本条仅取回命中段落及检测到的限制语境；完整适用条件、例外和引用仍须结合原文复核。'})
    if not evidence:
        gaps.append({"code": "policy_evidence_missing", "message": "没有可核实的相关政策证据。"})
    try:
        evaluation = tax_conditions.logic_value(json.loads(rule["condition_logic_json"]), truth_values)
    except (ValueError, KeyError, TypeError):
        evaluation = "UNKNOWN"
        gaps.append({"code": "rule_logic_pending", "message": "规则条件尚未完成可执行建模。"})
    fact_evaluation=evaluation if all(f['state']=='known' for f in facts.values()) else 'UNKNOWN'
    if gaps:
        evaluation = "UNKNOWN"
    check = {"topic": topic, "rule_id": rule_id, "rule_code": rule.get("rule_code"), "version": rule.get("version"),
             "rule_type": rule["rule_type"], "review_status": rule["review_status"],
             "condition_logic": json.loads(rule["condition_logic_json"]), "outcome_text": rule["outcome_text"],
             "valid_from": rule.get("valid_from"), "valid_to": rule.get("valid_to"), "evaluation": evaluation}
    if rule.get('conversion_json'):
        check['fact_evaluation']=fact_evaluation
        check['conversion']=json.loads(rule['conversion_json'])['compiler']
        check['fact_scope']='仅核对原有内部筛查计算；不代表政策有效、适用或违法认定。'
    check["disposition"] = ("excluded" if any(x["trigger_state"] == "TRUE" and x["effect"] == "exclude" for x in exceptions)
                            else "needs_review" if evaluation == "UNKNOWN" else "condition_met" if evaluation == "TRUE" else "condition_not_met")
    safe_task = {key: task.get(key) for key in ("audit_task_id", "tax_type", "region_code", "period_start", "period_end", "data_scope")}
    bundle = {"schema_version": 1, "task": safe_task, "check": check, "facts": list(facts.values()),
              "metric_definitions": context["metrics"],
              "conditions": conditions, "exceptions": exceptions, "policy_evidence": evidence, "policy_leads": leads,
              "relationships": closure["relationships"], "gaps": gaps,
              "bundle_status": "insufficient" if not evidence else "needs_review" if gaps else "ready",
              "retrieval_trace": {**{name + "_hits": [h["clause_id"] for h in result.get("hits", [])] for name, result in routes.items()},
                                  "routes": routes, "semantic_status": routes["semantic"]["status"], "rrf_k": 60,
                                  "candidate_limit": candidate_limit, "review_limit": review_limit,
                                  "fused": combined, "required_clause_ids": sorted(required),
                                  "dependency_clause_ids": closure["clause_ids"], "decisions": decisions, "excluded": excluded},
              "execution": {"offline": bool(offline), "external_api_calls": 0}}
    if fact_assessment is None:
        try:
            main_conditions = tax_conditions.validate_logic(json.loads(rule["condition_logic_json"]), set(truth_values))
        except (ValueError, KeyError, TypeError):
            main_conditions = set(truth_values)
        main_metrics = {mid for c in conditions if c["condition_id"] in main_conditions
                        for mid in (c["left_metric_id"], c.get("right_metric_id")) if mid}
        main_facts = {mid:f for mid,f in facts.items() if mid in main_metrics}
        status = "FACT_INCOMPLETE" if any(f["state"] != "known" for f in main_facts.values()) or not main_facts else (
            "NOT_COMPARABLE" if any(g["code"] == "comparability_pending" and g["condition_id"] in main_conditions for g in gaps) else "FACT_CONFIRMED")
        fact_assessment = {"status": status, "amount": None, "unit": None,
                           "source_ids": sorted({sid for f in facts.values() for sid in f["source_ids"]}), "calculation_trace": []}
    bundle["fact_assessment"] = deepcopy(fact_assessment)
    risk_status = "NEEDS_MORE_DATA" if fact_assessment["status"] == "FACT_INCOMPLETE" else "NEEDS_REVIEW"
    if check["disposition"] == "excluded":
        risk_status = "NOT_APPLICABLE"
    elif not gaps:
        risk_status = "RISK_CANDIDATE" if evaluation == "TRUE" else "NO_RISK_SIGNAL" if evaluation == "FALSE" else "NEEDS_REVIEW"
    elif rule_id == "VAT-R01" and fact_assessment["status"] == "FACT_CONFIRMED" and fact_assessment["amount"] is not None:
        from decimal import Decimal
        if Decimal(fact_assessment["amount"]) == 0:
            risk_status = "NO_RISK_SIGNAL"
    bundle["risk_assessment"] = {"status": risk_status, "policy_approval": rule["review_status"],
                                 "scope": "仅本项数值核对；无差异不代表全面合规。", "missing_evidence": [g["message"] for g in gaps]}
    bundle["analysis_readiness"] = bundle["bundle_status"]
    bundle["analysis_mode"] = "evidence_assessment" if bundle["bundle_status"] == "ready" else "review_only"
    core = {key: bundle[key] for key in ("task", "check", "facts", "metric_definitions", "conditions", "exceptions", "policy_evidence", "policy_leads", "relationships", "gaps", "fact_assessment", "risk_assessment")}
    bundle["bundle_id"] = digest(core)
    bundle["batching"] = split_bundle(bundle, token_budget)
    if bundle["batching"]["unsplittable"]:
        bundle["bundle_status"] = "needs_review"
        bundle["gaps"].append({"code": "budget_unsplittable", "message": "单个完整条款或必要上下文超出预算，保留原文并转复核。"})
    bundle["execution"]["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    bundle["analysis_readiness"] = bundle["bundle_status"]
    bundle["analysis_mode"] = "evidence_assessment" if bundle["bundle_status"] == "ready" else "review_only"
    bundle["content_hash"] = content_hash(bundle)
    return bundle
