"""Deterministic P1 training scorer built from the same Finding objects as audits."""
from __future__ import annotations

from typing import Any

from .models import Finding


def score_submission(
    findings: list[Finding],
    selected_rule_ids: list[str],
    weights: dict[str, float] | None = None,
    false_positive_penalty: float = 5.0,
) -> dict[str, Any]:
    """Score a student's selected risk rules with traceable details.

    Correct hits earn their configured share, misses earn zero for that risk,
    and every false positive is deducted explicitly with the engine's reason.
    Skipped rules are never treated as safe: selecting one is explained as
    "insufficient evidence" rather than as a normal false-positive pass.
    """
    by_id = {finding.rule.id: finding for finding in findings}
    chosen = list(dict.fromkeys(selected_rule_ids))
    unknown = sorted(set(chosen) - set(by_id))
    if unknown:
        raise ValueError(f"答案含未知规则：{', '.join(unknown)}")

    hits = {rule_id for rule_id, finding in by_id.items() if finding.status == "hit"}
    if not hits:
        raise ValueError("该实训案例没有可评分的命中风险")
    configured = weights or {}
    raw_weights = {rule_id: float(configured.get(rule_id, 1.0)) for rule_id in hits}
    if any(value <= 0 for value in raw_weights.values()):
        raise ValueError("每个风险点权重必须大于零")
    total_weight = sum(raw_weights.values())
    points = {rule_id: 100.0 * value / total_weight for rule_id, value in raw_weights.items()}

    correct_ids = sorted(hits & set(chosen))
    missed_ids = sorted(hits - set(chosen))
    false_ids = sorted(set(chosen) - hits)
    earned = sum(points[rule_id] for rule_id in correct_ids)
    deduction = false_positive_penalty * len(false_ids)
    score = max(0.0, min(100.0, earned - deduction))

    def detail(rule_id: str, kind: str, point_value: float) -> dict[str, Any]:
        finding = by_id[rule_id]
        if kind == "correct":
            explanation = f"已识别：{finding.conclusion}"
        elif kind == "missed":
            explanation = f"漏检：{finding.calculation}；{finding.threshold_desc}"
        elif finding.status == "skipped":
            explanation = f"不能判为风险：材料不足，{finding.skip_reason}"
        else:
            explanation = f"误报：{finding.conclusion} {finding.calculation}".strip()
        return {
            "rule_id": rule_id,
            "name": finding.rule.name,
            "kind": kind,
            "points": round(point_value, 2),
            "explanation": explanation,
            "legal_basis": finding.rule.legal_basis,
        }

    return {
        "score": round(score, 2),
        "earned_before_deduction": round(earned, 2),
        "false_positive_deduction": round(deduction, 2),
        "correct": [detail(rule_id, "correct", points[rule_id]) for rule_id in correct_ids],
        "missed": [detail(rule_id, "missed", 0.0) for rule_id in missed_ids],
        "false_positives": [detail(rule_id, "false_positive", -false_positive_penalty) for rule_id in false_ids],
        "standard_answer": sorted(hits),
    }

