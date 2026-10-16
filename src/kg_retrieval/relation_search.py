"""Rule-led, bounded graph retrieval; required conditions/exceptions are not top-k truncated."""
from collections import deque
from .common import by_id, rows, clause_record, candidate


CHANGE_RELATIONS = {"AMENDS", "REPEALS", "INTERPRETS", "SUPPLEMENTS"}


def rule_context(db, rule_id):
    rule = by_id(db, "TaxRule", "rule_id", rule_id)
    conditions = rows(db, "Condition", "rule_id=?", (rule_id,))
    exceptions = rows(db, "Exception", "rule_id=?", (rule_id,))
    metric_ids = {value for c in conditions for value in (c["left_metric_id"], c.get("right_metric_id")) if value}
    edges = rows(db, "Relationship", "(from_type='TaxRule' AND from_id=?) OR (to_type='TaxRule' AND to_id=?)", (rule_id, rule_id))
    metric_ids |= {e["to_id"] for e in edges if e["from_type"] == "TaxRule" and e["to_type"] == "Metric" and e["relation_type"] == "USES_METRIC"}
    metrics = {mid: by_id(db, "Metric", "metric_id", mid) for mid in sorted(metric_ids)}
    required = {}
    for kind, items in (("Condition", conditions), ("Exception", exceptions)):
        for item in items:
            if item.get("source_clause_id"):
                cid = item["source_clause_id"]
                required.setdefault(cid, []).append({"type": kind, "id": item[kind.lower() + "_id"], "via": "source_clause_id"})
    for edge in edges:
        if edge["from_type"] == "Clause":
            required.setdefault(edge["from_id"], []).append({"relationship_id": edge["relationship_id"], "review_status": edge["review_status"]})
        if edge["to_type"] == "Clause":
            required.setdefault(edge["to_id"], []).append({"relationship_id": edge["relationship_id"], "review_status": edge["review_status"]})
        if edge.get("evidence_clause_id"):
            required.setdefault(edge["evidence_clause_id"], []).append({"relationship_id": edge["relationship_id"], "via": "evidence_clause_id"})
    return {"rule": rule, "conditions": conditions, "exceptions": exceptions, "metrics": metrics,
            "relationships": edges, "required": required}


def search(db, rule_id, *, limit=20, hops=2):
    if not 1 <= hops <= 2 or not 1 <= limit <= 100:
        raise ValueError("图关系候选须为 1–100 条，默认扩展 1–2 跳。")
    context = rule_context(db, rule_id)
    if context["rule"]["review_status"] != "approved":
        return {"status": "needs_review", "hits": [], "reason": "TaxRule 尚未审核。"}
    found = {}
    for cid, paths in context["required"].items():
        # FK provenance is backed by the reviewed rule. Explicit candidate edges remain leads.
        if all(p.get("review_status", "approved") == "approved" for p in paths):
            clause, policy = clause_record(db, cid)
            found[cid] = candidate(clause, policy, "relation", score=1, reason="规则条件、例外或已审核关系", path=paths)
    queue = deque([("TaxRule", rule_id, [], 0)])
    seen = {("TaxRule", rule_id)}
    while queue:
        kind, node, path, depth = queue.popleft()
        if depth >= hops:
            continue
        links = rows(db, "Relationship", "review_status='approved' AND ((from_type=? AND from_id=?) OR (to_type=? AND to_id=?))",
                     (kind, node, kind, node))
        for edge in links:
            if edge["relation_type"] in CHANGE_RELATIONS:
                continue  # Complete change chains are followed separately, with cycle detection.
            nxt = (edge["to_type"], edge["to_id"]) if (edge["from_type"], edge["from_id"]) == (kind, node) else (edge["from_type"], edge["from_id"])
            if nxt in seen or nxt[0] == "Policy":
                continue  # A policy hub is not a reason to retrieve every unrelated article.
            seen.add(nxt)
            current_path = [*path, {"relationship_id": edge["relationship_id"], "relation_type": edge["relation_type"]}]
            if nxt[0] == "Clause":
                clause, policy = clause_record(db, nxt[1])
                found.setdefault(nxt[1], candidate(clause, policy, "relation", score=1 / (depth + 1), reason="已审核图关系", path=current_path))
            queue.append((*nxt, current_path, depth + 1))
    hits = list(found.values())
    hits.sort(key=lambda hit: (hit["clause_id"] not in context["required"], -hit["score"], hit["clause_id"]))
    return {"status": "ready", "hits": hits[:limit], "truncated": len(hits) > limit}


def dependency_closure(db, clause_ids):
    """Mandatory ancestors and modification evidence, including pending links as explicit gaps."""
    required, edges, gaps = set(clause_ids), {}, []
    queue, visited = deque(clause_ids), set()
    while queue:
        cid = queue.popleft()
        if cid in visited:
            continue
        visited.add(cid)
        clause, policy = clause_record(db, cid)
        added = []
        if clause.get("parent_clause_id"):
            added.append(clause["parent_clause_id"])
        links = rows(db, "Relationship", "((from_type='Clause' AND from_id=?) OR (to_type='Clause' AND to_id=?) "
                     "OR (from_type='Policy' AND from_id=?) OR (to_type='Policy' AND to_id=?))",
                     (cid, cid, policy["policy_id"], policy["policy_id"]))
        for edge in links:
            if edge["relation_type"] not in CHANGE_RELATIONS:
                continue
            edges[edge["relationship_id"]] = edge
            if edge["review_status"] != "approved":
                gaps.append({"code": "change_pending", "relationship_id": edge["relationship_id"], "clause_id": cid})
            for direction in ("from", "to"):
                if edge[direction + "_type"] == "Clause":
                    added.append(edge[direction + "_id"])
            if edge.get("evidence_clause_id"):
                added.append(edge["evidence_clause_id"])
            else:
                gaps.append({"code": "change_source_missing", "relationship_id": edge["relationship_id"]})
        for item in added:
            if item not in required:
                required.add(item)
                queue.append(item)
    return {"clause_ids": sorted(required), "relationships": list(edges.values()), "gaps": gaps}
