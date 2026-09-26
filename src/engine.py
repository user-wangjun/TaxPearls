"""YAML 规则引擎。

规则的三种求值类型：
    deviation        相对偏离度：|left − right| / |right|  >  threshold
    amount_mismatch  绝对差额：  |left − right|           >  tolerance
    ratio_range      比率区间：  numerator / denominator 落在 [min, max] 之外

设计原则：LLM 不参与判定。每条规则的命中与否完全由确定性计算决定，
并同时产出「测算值 / 阈值 / 计算过程 / 证据行」，使结论可复核、可举证。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .models import Dataset, EvidenceItem, Finding, Rule


class RuleError(Exception):
    """规则定义不合法。"""


class MissingMetric(Exception):
    """审计材料中缺少规则所需的指标，该规则应标记为「未执行」而非直接失败。"""


REQUIRED_FIELDS = [
    "id", "name", "category", "tax_type", "severity",
    "logic", "evidence", "legal_basis", "suggestion",
]
VALID_SEVERITY = {"high", "medium", "low"}


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def load_rules(rules_dir: str | Path) -> list[Rule]:
    """加载目录下全部 .yaml 规则。"""
    rules_dir = Path(rules_dir)
    if not rules_dir.exists():
        raise RuleError(f"规则目录不存在：{rules_dir}")

    rules: list[Rule] = []
    seen: set[str] = set()

    for f in sorted(rules_dir.glob("*.y*ml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise RuleError(f"{f.name} YAML 解析失败：{e}") from e

        if not isinstance(doc, dict):
            raise RuleError(f"{f.name} 顶层必须是映射（key-value）。")

        missing = [k for k in REQUIRED_FIELDS if k not in doc]
        if missing:
            raise RuleError(f"{f.name} 缺少必填字段：{missing}")
        if doc["severity"] not in VALID_SEVERITY:
            raise RuleError(
                f"{f.name} severity 必须是 {sorted(VALID_SEVERITY)}，实际为 {doc['severity']}"
            )
        if doc["id"] in seen:
            raise RuleError(f"规则 ID 重复：{doc['id']}")
        seen.add(doc["id"])

        rules.append(
            Rule(
                id=str(doc["id"]),
                name=str(doc["name"]),
                category=str(doc["category"]),
                tax_type=str(doc["tax_type"]),
                severity=str(doc["severity"]),
                logic=doc["logic"],
                evidence=[str(x) for x in doc["evidence"]],
                legal_basis=[str(x) for x in doc["legal_basis"]],
                suggestion=str(doc["suggestion"]),
                description=str(doc.get("description", "")),
                source_file=f.name,
            )
        )

    if not rules:
        raise RuleError(f"{rules_dir} 下没有找到任何规则文件。")
    return rules


def _need(dataset: Dataset, key: str, rule: Rule) -> float:
    v = dataset.get(key)
    if v is None:
        raise MissingMetric(
            f"规则 {rule.id} 需要指标「{key}」，但审计材料中未取到，"
            f"请补充对应科目或申报表项目。"
        )
    return v


def evaluate(rule: Rule, dataset: Dataset) -> Finding:
    """对单条规则求值。材料不足时返回 skipped，绝不因缺数据而中断整批检查。"""
    try:
        return _evaluate_strict(rule, dataset)
    except MissingMetric as e:
        return Finding(
            rule=rule,
            status="skipped",
            measured=None,
            threshold_desc="—",
            conclusion=f"本项未执行：{e}",
            evidence=[],
            calculation="",
            skip_reason=str(e),
        )


def _evaluate_strict(rule: Rule, dataset: Dataset) -> Finding:
    """对单条规则求值，返回带证据的判定结果。"""
    logic = rule.logic
    ltype = logic.get("type")

    evidence: list[EvidenceItem] = []

    if ltype == "deviation":
        left_key, right_key = logic["left"], logic["right"]
        threshold = float(logic["threshold"])
        left, right = _need(dataset, left_key, rule), _need(dataset, right_key, rule)
        gap = abs(left - right)
        measured = gap / abs(right) if right else float("inf")
        hit = measured > threshold
        calc = (
            f"|{_money(left)} − {_money(right)}| ÷ |{_money(right)}| = {_pct(measured)}"
            f"，阈值 {_pct(threshold)}"
        )
        threshold_desc = f"相对偏离度 > {_pct(threshold)}"
        conclusion = (
            f"{left_key} 与 {right_key} 偏离 {_pct(measured)}"
            if hit
            else f"{left_key} 与 {right_key} 偏离 {_pct(measured)}，在阈值内"
        )
        evidence = [
            EvidenceItem(left_key, _money(left), dataset.source_of(left_key), True),
            EvidenceItem(right_key, _money(right), dataset.source_of(right_key), True),
            EvidenceItem("绝对差额", _money(gap), "计算值"),
            EvidenceItem("相对偏离度", _pct(measured), f"阈值 {_pct(threshold)}", True),
        ]

    elif ltype == "amount_mismatch":
        left_key, right_key = logic["left"], logic["right"]
        tolerance = float(logic["tolerance"])
        left, right = _need(dataset, left_key, rule), _need(dataset, right_key, rule)
        gap = abs(left - right)
        measured = gap
        hit = gap > tolerance
        calc = f"|{_money(left)} − {_money(right)}| = {_money(gap)}，容差 {_money(tolerance)}"
        threshold_desc = f"绝对差额 > {_money(tolerance)}"
        conclusion = (
            f"{left_key} 与 {right_key} 相差 {_money(gap)}"
            if hit
            else f"{left_key} 与 {right_key} 相差 {_money(gap)}，在容差内"
        )
        evidence = [
            EvidenceItem(left_key, _money(left), dataset.source_of(left_key), True),
            EvidenceItem(right_key, _money(right), dataset.source_of(right_key), True),
            EvidenceItem("差额", _money(gap), f"容差 {_money(tolerance)}", True),
        ]

    elif ltype == "ratio_range":
        num_key, den_key = logic["numerator"], logic["denominator"]
        lo, hi = float(logic["min"]), float(logic["max"])
        num, den = _need(dataset, num_key, rule), _need(dataset, den_key, rule)
        measured = num / den if den else 0.0
        hit = not (lo <= measured <= hi)
        calc = f"{_money(num)} ÷ {_money(den)} = {_pct(measured)}，参考区间 {_pct(lo)} ~ {_pct(hi)}"
        threshold_desc = f"参考区间 {_pct(lo)} ~ {_pct(hi)}"
        conclusion = (
            f"比率 {_pct(measured)} 落在参考区间之外"
            if hit
            else f"比率 {_pct(measured)} 位于参考区间内"
        )
        evidence = [
            EvidenceItem(num_key, _money(num), dataset.source_of(num_key), True),
            EvidenceItem(den_key, _money(den), dataset.source_of(den_key), True),
            EvidenceItem("比率", _pct(measured), f"参考区间 {_pct(lo)} ~ {_pct(hi)}", True),
        ]

    else:
        raise RuleError(
            f"规则 {rule.id} 的 logic.type 不支持：{ltype!r}。"
            f"目前支持 deviation / amount_mismatch / ratio_range。"
        )

    return Finding(
        rule=rule,
        status="hit" if hit else "pass",
        measured=measured,
        threshold_desc=threshold_desc,
        conclusion=conclusion,
        evidence=evidence,
        calculation=calc,
    )


def run(rules: list[Rule], dataset: Dataset) -> list[Finding]:
    """执行全部规则。排序：命中优先 → 风险等级降序 → 规则 ID 升序；未执行项排最后。"""
    findings = [evaluate(r, dataset) for r in rules]
    findings.sort(
        key=lambda f: (
            0 if f.status == "hit" else (2 if f.status == "skipped" else 1),
            -f.severity_rank,
            f.rule.id,
        )
    )
    return findings
