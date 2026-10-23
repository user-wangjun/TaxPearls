"""Independent expected-ID assertions and transparent, per-route acceptance reporting."""
from time import perf_counter

from .bundle import build
from .common import by_id, digest, implementation_fingerprint
from .relation_search import rule_context
from .passages import exact_source


def case_input(db, case):
    if "task" in case and "facts" in case:
        return case["task"], case["facts"], case.get("source_catalog", [])
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='kg_fixture_identity'").fetchone():
        raise ValueError("真实语料案例须明确提供 task、facts 和来源，不生成假企业事实。")
    task = {"audit_task_id": "SYNTHETIC-" + case["id"], "data_scope": "synthetic", "tax_type": "VAT",
            "region_code": case.get("region_code", "CN-33"), "period_start": case.get("period_start", "2026-02-01"),
            "period_end": case.get("period_end", "2026-02-28"), "entity_ids": case.get("entity_ids", [])}
    definitions = rule_context(db, case["rule_id"])["metrics"]
    catalog = ["SYNTHETIC-SOURCE-" + mid for mid in definitions]
    facts = {mid: {"state": "known", "value": "0" if mid.startswith("MX") else "200000.01",
                   "unit": definition.get("unit"), "period_basis": definition.get("period_basis"),
                   "source_ids": ["SYNTHETIC-SOURCE-" + mid]}
             for mid, definition in definitions.items() if mid not in case.get("missing_metrics", [])}
    for mid, overrides in case.get("fact_overrides", {}).items():
        facts.setdefault(mid, {}).update(overrides)
    return task, facts, catalog


def evaluate(db, cases, *, model_dir=None, offline=True, token_budget=6000):
    started = perf_counter()
    records, bundles = [], {}
    required_total = recalled = sent_total = relevant_total = exception_total = exception_found = 0
    wrong_applicability = invented = duplicate_count = traceable = source_total = change_total = change_found = 0
    for case in cases:
        task, facts, catalog = case_input(db, case)
        bundle = build(db, task, case["rule_id"], facts, topic=case.get("topic", ""), keywords=case.get("keywords", []),
                       source_catalog=catalog, model_dir=model_dir, token_budget=case.get("token_budget", token_budget), offline=offline)
        bundles[case["id"]] = bundle
        evidence = bundle["policy_evidence"]
        actual = {r["clause_id"] for r in evidence}
        applicable = {r["clause_id"] for r in evidence if r["applicability"] == "applicable"}
        expected, allowed = set(case["expected_clauses"]), set(case.get("allowed_clauses", case["expected_clauses"]))
        required_total += len(expected)
        recalled += len(expected & actual)
        sent_total += len(actual)
        relevant_total += len(actual & allowed)
        exceptions = {item["exception_id"] for item in bundle["exceptions"]}
        expected_exceptions = set(case.get("expected_exceptions", []))
        exception_total += len(expected_exceptions)
        exception_found += len(expected_exceptions & exceptions)
        failures = []
        def require(ok, message):
            if not ok:
                failures.append(message)
        require(expected <= actual, "缺少期望条款：" + ",".join(sorted(expected - actual)))
        require(actual <= allowed, "材料包包含未标为相关的条款：" + ",".join(sorted(actual - allowed)))
        require(expected_exceptions <= exceptions, "缺少必要例外")
        relationships = {item["relationship_id"] for item in bundle["relationships"]}
        change_total += len(case.get("expected_relationships", []))
        change_found += len(set(case.get("expected_relationships", [])) & relationships)
        require(set(case.get("expected_relationships", [])) <= relationships, "缺少必要修改/废止关系")
        require(set(case.get("context_only", [])) <= {r["clause_id"] for r in evidence if r["applicability"] == "context_only"}, "旧版条款未明确标为历史上下文")
        require(set(case.get("must_be_applicable", [])) <= applicable, "历史时点有效条款未正确保留")
        misuse = len(set(case.get("must_not_be_applicable", [])) & applicable)
        wrong_applicability += misuse
        require(misuse == 0, "错误期间/地区政策被作为有效依据")
        excluded = {r["clause_id"] for r in bundle["retrieval_trace"]["excluded"]}
        require(set(case.get("expected_excluded", [])) <= excluded, "未记录预期过滤原因")
        require(set(case.get("expected_leads", [])) <= {r["clause_id"] for r in bundle["policy_leads"]}, "待复核政策线索丢失")
        if case.get("expected_status"):
            require(bundle["bundle_status"] == case["expected_status"], "材料包状态不符")
        if case.get("expected_evaluation"):
            require(bundle["check"]["evaluation"] == case["expected_evaluation"], "条件结果不符")
        require(set(case.get("expected_gap_codes", [])) <= {r["code"] for r in bundle["gaps"]}, "缺少应提示的数据/口径缺口")
        for route in ("relation", "keyword", "semantic"):
            require(set(case.get(route + "_required", [])) <= set(bundle["retrieval_trace"][route + "_hits"]), route + " 路线未召回指定条款")
        for cid in case.get("multi_route_required", []):
            count = sum(cid in bundle["retrieval_trace"][route + "_hits"] for route in ("relation", "keyword", "semantic"))
            require(count >= 2, "没有验证多路重复命中")
        duplicates = len(evidence) - len(actual)
        duplicate_count += duplicates
        require(duplicates == 0, "材料包含重复条款")
        for item in [*evidence, *bundle["policy_leads"]]:
            source_total += 1
            try:
                original = by_id(db, "Clause", "clause_id", item["clause_id"])
                exact = exact_source(item, original['content'])
                require(exact, "条款原文或片段坐标、哈希不一致")
                if item.get('partial_clause'):
                    require(any(g['code'] == 'retrieval_context_partial' and g.get('clause_id') == item['clause_id']
                                for g in bundle['gaps']), '局部取回未明确记录完整性缺口')
                require(bool(item["source_location"] and item["source_url"] and item["file_sha256"]), "来源不可追溯")
                traceable += bool(exact and item["source_location"] and item["source_url"] and item["file_sha256"])
            except ValueError:
                invented += 1
                failures.append("出现不存在的条款引用")
        if case.get("must_preserve_full_text"):
            parts = bundle["batching"]["parts"]
            fragments = {}
            for part in parts:
                for item in part['policy_evidence']:
                    fragments.setdefault(item['clause_id'], {})[item.get('source_char_start', 0)] = item['text']
            restored = {cid: ''.join(text for _, text in sorted(pieces.items())) for cid, pieces in fragments.items()}
            require(all(restored.get(item["clause_id"]) == item["text"] for item in evidence), "超预算拆包遗漏原文")
            require(bundle["batching"]["part_count"] > 1 or bundle["batching"]["unsplittable"], "未实际触发预算边界")
            require(all(part["exceptions"] == bundle["exceptions"] for part in parts), "拆包丢失例外")
        if case.get("unaffected_rule"):
            other_case = {"id": case["id"] + "-unaffected", "rule_id": case["unaffected_rule"]}
            other_task, other_facts, other_catalog = case_input(db, other_case)
            other = build(db, other_task, other_case["rule_id"], other_facts, source_catalog=other_catalog, model_dir=model_dir, offline=offline)
            require(not any(g["code"] == "metric_missing" for g in other["gaps"]), "一个指标缺失阻断了不相关检查")
        records.append({"id": case["id"], "passed": not failures, "failures": failures, "expected": sorted(expected),
                        "actual": sorted(actual), "bundle_status": bundle["bundle_status"],
                        "route_hits": {route: bundle["retrieval_trace"][route + "_hits"] for route in ("relation", "keyword", "semantic")},
                        "semantic_status": bundle["retrieval_trace"]["semantic_status"],
                        "route_recall": {route: len(expected & set(bundle["retrieval_trace"][route + "_hits"])) / len(expected) if expected else None
                                         for route in ("relation", "keyword", "semantic")},
                        "candidate_count": len(bundle["retrieval_trace"]["fused"]),
                        "latency_ms": bundle["execution"]["elapsed_ms"],
                        "token_upper_bound": sum(p["token_upper_bound"] for p in bundle["batching"]["parts"]),
                        "external_api_calls": bundle["execution"]["external_api_calls"]})
    recall, precision = recalled / required_total if required_total else 1, relevant_total / sent_total if sent_total else 1
    exception_coverage = exception_found / exception_total if exception_total else 1
    semantic_ready = bool(records) and all(r["semantic_status"] == "ready" for r in records)
    categories = {r["id"] for r in records if r["passed"]}
    gate = (bool(records) and all(r["passed"] for r in records) and semantic_ready and recall >= .95 and precision >= .8
            and exception_coverage == 1 and wrong_applicability == invented == duplicate_count == 0
            and {f"K{i:02}" for i in range(1, 13)} <= categories)
    return {"report_version": 1, "data_scope": "synthetic" if db.execute("SELECT 1 FROM sqlite_master WHERE name='kg_fixture_identity'").fetchone() else "provided_cases",
            "gate_passed": gate, "formal_policy_acceptance": False, "offline": bool(offline),
            "metrics": {"recall": recall, "precision": precision, "exception_coverage": exception_coverage,
                        "wrong_period_region_uses": wrong_applicability, "invented_references": invented,
                        "duplicate_clauses": duplicate_count, "semantic_ready": semantic_ready,
                        "source_traceability": traceable / source_total if source_total else 1,
                        "change_relation_coverage": change_found / change_total if change_total else 1,
                        "external_api_calls": sum(r["external_api_calls"] for r in records)},
            "case_count": len(records), "passed_count": sum(r["passed"] for r in records), "cases": records,
            "policy_count": db.execute("SELECT count(*) FROM Policy").fetchone()[0],
            "gold_cases_hash": digest(cases), "implementation_fingerprint": implementation_fingerprint(),
            "model_fingerprints": sorted({b["retrieval_trace"]["routes"]["semantic"].get("model_fingerprint") for b in bundles.values()
                                          if b["retrieval_trace"]["routes"]["semantic"].get("model_fingerprint")}),
            "elapsed_ms": round((perf_counter() - started) * 1000, 3)}, bundles
