"""Allow only a minimal evidence payload; readiness is independent of permission."""
from copy import deepcopy
import re
from urllib.parse import urlsplit
from collections import Counter

from .common import canonical


def compact(payload):
    """Intern repeated prose, losslessly, within this independent request only.

    Identifiers and citation lists remain literal. No evidence is summarized or
    dropped; the dictionary travels with every request that references it.
    """
    protected = {'response_constraints', 'clause_ids', 'source_ids', 'required_clause_ids'}
    counts = Counter()
    collision = False

    def visit(value, key='', collect=False):
        nonlocal collision
        if key in protected or key.endswith(('_id', '_ids')):
            return deepcopy(value)
        if isinstance(value, dict):
            if set(value) == {'context_ref'}:
                collision = True
            return {k: visit(v, k, collect) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v, key, collect) for v in value]
        if isinstance(value, str) and len(value.encode()) >= 100:
            if collect:
                counts[value] += 1
            elif value in refs:
                return {'context_ref': refs[value]}
        return value

    refs = {}
    visit(payload, collect=True)
    if collision or 'shared_context' in payload or 'context_encoding' in payload:
        return deepcopy(payload)
    refs = {value: f'ctx{i}' for i, (value, count) in enumerate(counts.items(), 1) if count > 1}
    if not refs:
        return deepcopy(payload)
    result = visit(payload)
    result['shared_context'] = {ref: value for value, ref in refs.items()}
    result['context_encoding'] = 'repeated_text_v1'
    return result if len(canonical(result).encode()) < len(canonical(payload).encode()) else deepcopy(payload)


def expand(payload):
    """Exact inverse used by diagnostics/tests; never trusts model references."""
    if payload.get('context_encoding') != 'repeated_text_v1':
        return deepcopy(payload)
    dictionary = payload.get('shared_context', {})
    def visit(value):
        if isinstance(value, dict):
            if set(value) == {'context_ref'} and value['context_ref'] in dictionary:
                return deepcopy(dictionary[value['context_ref']])
            return {k: visit(v) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value
    return visit({k: v for k, v in payload.items() if k not in {'shared_context', 'context_encoding'}})


def prepare(package, *, authorized, destination):
    parsed = urlsplit(destination)
    reasons = []
    if not authorized:
        reasons.append("本次任务未获外送授权。")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        reasons.append("外送目的地须为已配置的 HTTPS 模型基础地址。")
    parts = []
    for original in package["batching"]["parts"]:
        part = {k: deepcopy(original[k]) for k in ("task", "check", "facts", "conditions", "exceptions", "policy_evidence", "policy_leads", "gaps", "relationships", "part_number", "part_count")}
        if original.get("supporting_materials"):
            part["supporting_materials"] = deepcopy(original["supporting_materials"])
        if original.get('retrieval_lead_index'):
            part['retrieval_lead_index'] = deepcopy(original['retrieval_lead_index'])
        for key in ('batch_category', 'batch_category_name', 'batch_group', 'batch_group_name', 'required_clause_ids'):
            if key in original:
                part[key] = deepcopy(original[key])
        # Keep source/form selection and reviewer identity locally. The model
        # receives only the independently calculated fact and trace.
        part["fact_assessment"] = {k: deepcopy(v) for k, v in original.get("fact_assessment", package.get("fact_assessment", {})).items() if k != "mapping_review"}
        part["risk_assessment"] = deepcopy(original.get("risk_assessment", package.get("risk_assessment", {})))
        part["bundle_status"] = package["bundle_status"]
        part["analysis_mode"] = "review_only" if package["bundle_status"] != "ready" else "evidence_assessment"
        serialized = canonical(part)
        if re.search(r"(?<![A-Za-z0-9.])(?:[0-9A-Z]{18}|\d{20}|\d{15})(?![A-Za-z0-9.])", serialized):
            reasons.append("材料包含可能的完整税号或发票号，请先核对并最小化内容。")
        if re.search(r"(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S{8,}|-----BEGIN .*PRIVATE KEY)", serialized):
            reasons.append("材料中存在凭证或密钥特征。")
        parts.append(part)
    return {"outbound_gate": "blocked" if reasons else "allowed", "reasons": sorted(set(reasons)),
            "analysis_readiness": package["bundle_status"],
            "analysis_mode": "evidence_assessment" if package["bundle_status"] == "ready" else "review_only",
            "parts": parts}
