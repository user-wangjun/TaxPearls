"""执行人工标注的 YAML 样例；失败返回非零退出码，结果包含完整证据。"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.engine import RuleError, UniqueLoader, evaluate, load_rules  # noqa: E402
from src.models import Company, Dataset, Metric  # noqa: E402
import yaml  # noqa: E402

CASES = ROOT / "samples" / "规则核对样例.yaml"


def read_cases(path=CASES):
    document = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueLoader)
    return document["groups"]


def case_values(group, case):
    values = {**group["baseline"], **case.get("set", {})}
    for name in case.get("remove", []):
        values.pop(name, None)
    return values


def dataset_for(rule, values):
    return Dataset(
        Company("规则回归企业（全部仿真）", "SYNTHETIC-ONLY", "批发和零售业", "2026年度"),
        [], {},
        {k: Metric(k, Decimal(str(v)), f"人工仿真底稿—{k}", rule.inputs.get(k, "样例数据"))
         for k, v in values.items()},
    )


def check_finding(finding, case):
    errors = []
    if finding.status != case["expected"]:
        errors.append(f"状态期望 {case['expected']}，实际 {finding.status}")
    if "measured" in case:
        expected = case["measured"]
        if expected is None:
            if finding.measured is not None:
                errors.append("组合规则不应产生混合单位测算值")
        elif finding.measured is None or abs(finding.measured - expected) > 1e-9:
            errors.append(f"测算期望 {expected}，实际 {finding.measured}")
    if finding.executed:
        names = {e.label for e in finding.evidence}
        if not set(finding.rule.evidence) <= names:
            errors.append("证据缺少公式依赖")
        if not finding.calculation or any(not e.source for e in finding.evidence):
            errors.append("缺少公式或证据来源")
    elif not finding.skip_reason:
        errors.append("未执行未说明原因")
    return errors


def verify(rules_dir=ROOT / "rules", cases_path=CASES):
    rules = {r.id: r for r in load_rules(rules_dir)}
    groups = read_cases(cases_path)
    group_ids = [g["rule_id"] for g in groups]
    if len(group_ids) != len(set(group_ids)) or set(group_ids) != set(rules):
        raise ValueError("样例必须逐条且唯一覆盖规则库，不得遗漏规则或引用不存在的规则")
    results = []
    for group in groups:
        rule = rules[group["rule_id"]]
        if {c["expected"] for c in group["cases"]} != {"hit", "pass", "skipped"}:
            raise ValueError(f"{rule.id} 必须覆盖命中/通过/未执行")
        for case in group["cases"]:
            finding = evaluate(rule, dataset_for(rule, case_values(group, case)))
            errors = check_finding(finding, case)
            results.append({
                "rule_id": rule.id, "rule_name": rule.name, "case": case["name"],
                "expected": case["expected"], "actual": finding.status,
                "ok": not errors, "errors": errors, "measured": finding.measured,
                "calculation": finding.calculation, "skip_reason": finding.skip_reason,
                "scope": rule.scope, "threshold_basis": rule.threshold_basis,
                "evidence": [asdict(e) for e in finding.evidence],
            })
    return {
        "rules": len(rules), "cases": len(results),
        "passed": sum(r["ok"] for r in results),
        "failed": sum(not r["ok"] for r in results),
        "statuses": dict(Counter(r["actual"] for r in results)), "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", type=Path, default=ROOT / "rules")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "规则核对结果.json")
    args = parser.parse_args()
    try:
        report = verify(args.rules, args.cases)
    except (RuleError, ValueError, OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        print(f"样例核对失败：{exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"{report['rules']} 条规则，{report['cases']} 个样例：通过 {report['passed']}，失败 {report['failed']}")
    print(f"结果：{args.output}")
    for item in report["results"]:
        if not item["ok"]:
            print(item["rule_id"], item["case"], "；".join(item["errors"]))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
