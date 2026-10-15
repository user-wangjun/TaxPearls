"""Clause-level period/region/source checks and explicit modification boundaries."""
from datetime import date, timedelta
import hashlib
from pathlib import Path
import re

from .common import tax_type


def region_matches(policy_region, enterprise_region):
    if not enterprise_region:
        return None
    if policy_region == "CN":
        return True
    if enterprise_region == "CN":
        return None  # Country is known; a local jurisdiction is still missing.
    if not policy_region or not re.fullmatch(r"CN(?:-[A-Za-z0-9]+)+", policy_region):
        return None
    return enterprise_region == policy_region or enterprise_region.startswith(policy_region + "-")


def source_check(policy, clause, task, cache):
    reasons = []
    if policy["validity_status"] == "unverified":
        reasons.append("政策效力尚未复核。")
    if clause["extraction_status"] != "verified":
        reasons.append("原文条款尚未复核。")
    if not clause.get("source_location") or not policy.get("source_url") or not policy.get("issuer"):
        reasons.append("原文定位、发布机关或来源缺失。")
    if policy.get("source_url", "").startswith("local-fixture://") and task["data_scope"] != "synthetic":
        reasons.append("合成测试政策不能作为真实企业的政策依据。")
    pid = policy["policy_id"]
    if pid not in cache:
        try:
            expected = policy.get("file_sha256")
            cache[pid] = bool(expected and hashlib.sha256(Path(policy["raw_file_path"]).read_bytes()).hexdigest() == expected)
        except (OSError, KeyError, TypeError):
            cache[pid] = False
    if not cache[pid]:
        reasons.append("政策原件缺失或哈希不一致。")
    return reasons


def check(clause, policy, task, changes, *, source_cache=None):
    source_cache = {} if source_cache is None else source_cache
    result = {"clause_id": clause["clause_id"], "status": "applicable", "reasons": [], "segments": [],
              "effective_from": clause.get("effective_from") or policy.get("effective_from"),
              "effective_to": clause.get("effective_to") or policy.get("effective_to")}
    if tax_type(policy["tax_type"]) != task["tax_type"]:
        result.update(status="excluded", reasons=["税种不属于本次检查范围。"])
        return result
    match = region_matches(policy.get("region_code"), task.get("region_code"))
    if match is False:
        result.update(status="excluded", reasons=["政策地区与企业适用地区不符。"])
        return result
    if match is None:
        result["reasons"].append("企业地区或政策地区尚未明确。")
    result["reasons"].extend(source_check(policy, clause, task, source_cache))
    start, end = task["period_start"], task["period_end"]
    a, b = result["effective_from"], result["effective_to"]
    try:
        for value in (a, b):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError("非 ISO 日期")
        if a and b and a > b:
            raise ValueError("有效期倒置")
    except (ValueError, TypeError):
        result.update(status="needs_review", reasons=[*result["reasons"], "条款有效期格式无效。"])
        return result
    if not a:
        result["reasons"].append("条款施行日期未知。")
    for edge in changes:
        target = ((edge["to_type"] == "Clause" and edge["to_id"] == clause["clause_id"])
                  or (edge["to_type"] == "Policy" and edge["to_id"] == policy["policy_id"]))
        if not target or edge["relation_type"] not in {"AMENDS", "REPEALS"}:
            continue
        on = edge.get("effective_from")
        if edge["review_status"] != "approved" or not on or not edge.get("evidence_clause_id"):
            result["reasons"].append("存在待核实的修改/废止关系。")
            continue
        if on > end:
            continue
        if edge["to_type"] == "Policy" and policy["validity_status"] == "partially_modified":
            result["reasons"].append("政策部分修改尚未精确定位至条款，不推断整份失效。")
            continue
        before = (date.fromisoformat(on) - timedelta(days=1)).isoformat()
        b = min(b, before) if b else before
        result["effective_to"] = b
        if start < on <= end:
            result["segments"] = [{"period_start": start, "period_end": before, "status": "old_version"},
                                  {"period_start": on, "period_end": end, "status": "changed_version"}]
        if start >= on:
            result.update(status="needs_review" if result["reasons"] else "context_only" if edge["relation_type"] == "AMENDS" else "excluded")
            result["reasons"].append("已审核条款修改，旧文仅作上下文。" if edge["relation_type"] == "AMENDS" else "本期已超出条款废止生效日期。")
            return result
    if a and a > end or b and b < start:
        result.update(status="excluded", reasons=[*result["reasons"], "条款有效区间与被检查期间不重叠。"])
        return result
    if not b:
        result["reasons"].append("终止日期为空，尚未证明覆盖本次完整期间；不视为永久有效。")
    if a and a > start or b and b < end:
        boundaries = {start, (date.fromisoformat(end) + timedelta(days=1)).isoformat()}
        if a and start < a <= end:
            boundaries.add(a)
        if b and start <= b < end:
            boundaries.add((date.fromisoformat(b) + timedelta(days=1)).isoformat())
        points = sorted(boundaries)
        result["segments"] = [{"period_start": x, "period_end": (date.fromisoformat(y) - timedelta(days=1)).isoformat(),
                               "status": "within_version" if (not a or x >= a) and (not b or x <= b) else "other_version_required"}
                              for x, y in zip(points, points[1:])]
        result["reasons"].append("审查跨越条款有效期边界，须按期间分段核对。")
    if policy["validity_status"] in {"expired", "repealed", "partially_modified"} and not b:
        result["reasons"].append("政策当前失效或部分修改，条款历史适用区间仍待核实。")
    if result["reasons"]:
        result["status"] = "needs_review"
    return result
