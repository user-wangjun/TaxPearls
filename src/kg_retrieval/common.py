"""Shared data contracts; importing this package never connects to a model provider."""
from datetime import date
import hashlib
import json
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def implementation_fingerprint():
    root = Path(__file__).resolve().parents[2]
    files = [*Path(__file__).parent.glob("*.py"), root / "src/tax_conditions.py", root / "src/vat_reconciliation.py",
             root / "webapp/policy_graph.py", root / "webapp/knowledge_evidence.py",
             *[root/'src'/name for name in ['rule_conversion.py','rule_bindings.py','policy_corpus.py','engine.py','config.py',
                                           'periods.py','reference_scope.py','models.py','snapshots.py',
                                           'policy_paths.py','policy_metadata.py','public_policy_package.py']]]
    # Git may check the same source out with LF or CRLF on Windows.
    return digest({str(path.relative_to(root)): hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                   for path in sorted(files)})


def tax_type(value):
    from src.policy_metadata import tax_code
    return tax_code(value)


def unit(value):
    return {"CNY": "元", "人民币元": "元", "RMB": "元"}.get(value, value)


def period_basis(value):
    return {"invoice_date": "issue_date", "vat_tax_period": "tax_period"}.get(value, value)


def checked_task(task):
    result = dict(task)
    for key in ("period_start", "period_end"):
        value = result.get(key)
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("检查期间须为 YYYY-MM-DD。")
    if result["period_start"] > result["period_end"]:
        raise ValueError("检查期间起止倒置。")
    result["tax_type"] = tax_type(result.get("tax_type", "VAT"))
    if result["tax_type"] not in {'VAT','PIT','EIT','ACCOUNTING','TAX_ADMIN','SOCIAL_INSURANCE'}:
        raise ValueError('未注册的核查税种或会计核对范围。')
    if result.get("data_scope", "enterprise") not in {"synthetic", "enterprise"}:
        raise ValueError("数据范围无效。")
    result.setdefault("data_scope", "enterprise")
    return result


def rows(db, table, where="", args=()):
    # Only internal callers supply table/where; all external values are bound parameters.
    return [dict(r) for r in db.execute(f'SELECT * FROM "{table}"' + (" WHERE " + where if where else ""), args)]


def by_id(db, table, key, value):
    found = rows(db, table, f'{key}=?', (value,))
    if not found:
        raise ValueError(f"{table} 不存在：{value}")
    return found[0]


def clause_record(db, clause_id):
    clause = by_id(db, "Clause", "clause_id", clause_id)
    return clause, by_id(db, "Policy", "policy_id", clause["policy_id"])


def candidate(clause, policy, route, *, score=0, reason=None, path=None):
    return {"clause_id": clause["clause_id"], "policy_id": policy["policy_id"],
            "version": clause.get("version", "1"), "effective_from": clause.get("effective_from") or policy.get("effective_from"),
            "effective_to": clause.get("effective_to") or policy.get("effective_to"),
            "route": route, "score": score, "reason": reason, "path": path or [],
            "source_location": clause.get("source_location")}
