"""Allow only a minimal evidence payload; readiness is independent of permission."""
from copy import deepcopy
import re
from urllib.parse import urlsplit

from .common import canonical


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
