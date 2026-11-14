"""Business identity and exact-value comparison, independent of upload/task IDs."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata


VERSION = "core-record-v1"
DIMENSIONS = ("ledger_id", "auxiliary_code", "department_code", "customer_code", "project_code", "currency")
FIELDS = {
    "invoice_detail": ("issue_date", "amount_excluding_tax", "tax_amount", "amount_including_tax",
                       "red_letter_status", "void_status", "direction", "unit"),
    "account_balance": ("account_name", "opening_balance", "opening_balance_debit", "opening_balance_credit",
                        "current_debit", "current_credit", "closing_balance", "closing_balance_debit",
                        "closing_balance_credit", "balance_direction", "unit"),
    "vat_declaration": ("item_label", "declared_value", "unit"),
}
NUMERIC = {"amount_excluding_tax", "tax_amount", "amount_including_tax", "opening_balance",
           "opening_balance_debit", "opening_balance_credit", "current_debit", "current_credit",
           "closing_balance", "closing_balance_debit", "closing_balance_credit", "declared_value"}
ISSUES = {"record_version_conflict", "record_identity_incomplete"}


def token(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", "" if value is None else str(value))).upper()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def number(value):
    if isinstance(value, dict):
        return {key: number(item) for key, item in sorted(value.items())}
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("数值无效")
        # Avoid Decimal.normalize(): its ambient precision can round evidence.
        text = format(result, "f")
        return (text.rstrip("0").rstrip(".") if "." in text else text) if result else "0"
    except (InvalidOperation, TypeError):
        raise ValueError("数值无效") from None


def day(value):
    return date.fromisoformat(str(value)).isoformat()


def describe(record, task):
    """Return (business identity, exact semantic content), or insufficient identity.

    Amounts/states never enter identity. Missing, zero and unreviewed are distinct.
    A short legacy invoice number requires its code; digital numbers retain all digits.
    """
    kind = record.get("material_type")
    if kind not in FIELDS or not token(task.get("tax_no")) or record.get("identity_incomplete"):
        return None
    if record.get("requires_source_review"):
        return None
    try:
        if kind == "invoice_detail":
            seller, buyer, invoice = (token(record.get(key)) for key in ("seller_tax_no", "buyer_tax_no", "invoice_no"))
            code = token(record.get("invoice_code"))
            if (not seller or not buyer or not invoice or token(task["tax_no"]) not in (seller, buyer)
                    or (len(invoice) != 20 and not code)):
                return None
            identity = [kind, seller, buyer, code if len(invoice) != 20 else "", invoice,
                        token(record.get("invoice_line_no"))]
            day(record.get("issue_date"))
        else:
            start, end = day(record.get("period_start")), day(record.get("period_end"))
            if start > end:
                return None
            if kind == "account_balance":
                if not token(record.get("account_code")):
                    return None
                identity = [kind, start, end, token(record["account_code"]),
                            *[token(record.get(key)) for key in DIMENSIONS]]
            else:
                column = record.get("column_scope") or record.get("field_sources", {}).get("declared_value", {}).get("source_label")
                if (record.get("form_type") not in {"main", "supplement"} or not record.get("form_name")
                        or not token(record.get("item_key")) or not token(column)):
                    return None
                identity = [kind, start, end, record["form_type"], token(record.get("form_identity") or record["form_name"]),
                            token(record["item_key"]), token(column)]
        content = {key: number(record.get(key)) if key in NUMERIC else token(record.get(key)) or None
                   for key in FIELDS[kind]}
        if kind == "invoice_detail":
            content["issue_date"] = day(record["issue_date"])
        invalid = {key: value for key, value in record.get("value_states", {}).items()
                   if key in {*FIELDS[kind], "invoice_no", "invoice_code", "invoice_line_no", "account_code", "item_key"}
                   and value not in {"present", "missing"}}
        if invalid:
            return None
        return identity, content
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def classify(records, task, *, key_for=None, history=None, decisions=None):
    """Annotate every source; history never removes a current task's valid evidence.

    key_for is a tenant/company keyed HMAC in persistence, canonical JSON locally.
    history/decisions contain only authorized sources. Choices cover an exact set
    of observed versions, so an unseen later version becomes a conflict again.
    """
    key_for = key_for or (lambda domain, value: canonical([domain, value]))
    history, decisions = history or {}, decisions or {}
    groups, rows, issues = defaultdict(list), [], []
    counts = dict(total=0, new=0, historical_duplicate=0, task_duplicate=0, conflict=0,
                  unverified=0, superseded=0, effective=0)
    for row in records:
        if row.get("material_type") not in FIELDS:
            continue
        counts["total"] += 1
        previous_duplicate = row.get("duplicate_of") if "dedup" not in row else None
        for key in ("duplicate_of", "dedup_conflict", "dedup_superseded", "dedup"):
            row.pop(key, None)
        description = describe(row, task)
        entry = {"source_record_id": row["id"], "file_id": row.get("source_file_id"),
                 "position": row.get("source_position"), "material_type": row["material_type"]}
        rows.append(entry)
        if description is None:
            if previous_duplicate:
                row["duplicate_of"] = previous_duplicate
            entry["status"] = "unverified"
            counts["unverified"] += 1
            continue
        identity, content = description
        business = key_for("identity", identity)
        version = key_for("value", [business, content])
        entry.update(record_key=business, version_key=version)
        row["dedup"] = {"record_key": business, "version_key": version}
        groups[business].append((row, entry))
    for business, group in groups.items():
        past = history.get(business, [])
        versions = {e["version_key"] for _, e in group} | {p["version_key"] for p in past}
        choice = decisions.get(business, {})
        chosen = choice.get("selected") if versions <= set(choice.get("versions", [])) else None
        conflict = len(versions) > 1 and chosen is None
        seen = {}
        for row, entry in sorted(group, key=lambda item: not item[0].get("in_scope", True)):
            version = entry["version_key"]
            matches = [p for p in past if p["version_key"] == version]
            entry["historical_sources"] = matches
            if conflict:
                row["dedup_conflict"] = True
                entry.update(status="conflict", conflict_versions=sorted(versions),
                             historical_sources=past)
            elif chosen and version != chosen:
                row["dedup_superseded"] = True
                entry["status"] = "superseded"
            elif version in seen:
                row["duplicate_of"] = seen[version]
                entry["status"] = "task_duplicate"
            else:
                seen[version] = row["id"]
                entry["status"] = "historical_duplicate" if matches else "new"
                if row.get("in_scope", True):
                    counts["effective"] += 1
            counts[entry["status"]] += 1
            row["dedup"]["status"] = entry["status"]
        if conflict:
            first = group[0][0]
            issues.append({"code": "record_version_conflict", "message": "同一业务记录存在不同值，请对照原件核对并采用版本；冲突未解决前不计入指标。",
                           "source_file_id": first.get("source_file_id"), "source_position": first.get("source_position")})
        elif chosen and not any(e["version_key"] == chosen for _, e in group):
            first = group[0][0]
            issues.append({"code": "record_version_conflict", "message": "本任务仅有未采用的旧版本，请补充已核实版本或排除旧材料。",
                           "source_file_id": first.get("source_file_id"), "source_position": first.get("source_position")})
    return {"algorithm": VERSION, "counts": counts, "records": rows}, issues


def usable(row):
    return not any(row.get(key) for key in ("duplicate_of", "dedup_conflict", "dedup_superseded"))


def refresh(processing):
    """Rebuild aggregates/links after classification, with exact Decimal arithmetic."""
    from decimal import localcontext
    summaries = {}
    records = processing.get("records", [])
    for row in records:
        if row.get("material_type") != "invoice_detail" or not row.get("in_scope") or not usable(row):
            continue
        key = tuple(row.get(k) for k in ("direction", "unit", "red_letter_status", "void_status"))
        group = summaries.setdefault(key, dict(zip(("direction", "unit", "red_letter_status", "void_status"), key)) |
                                     {"count": 0, "amount_excluding_tax": Decimal(0), "tax_amount": Decimal(0),
                                      "source_record_ids": [], "incomplete": False})
        group["count"] += 1
        group["source_record_ids"].append(row["id"])
        for field in ("amount_excluding_tax", "tax_amount"):
            if row.get(field) is None:
                group["incomplete"] = True
            else:
                with localcontext() as context:
                    context.prec = 100
                    group[field] += Decimal(row[field])
    processing["invoice_summaries"] = [{**g, **{k: None if g["incomplete"] else str(g[k])
                                              for k in ("amount_excluding_tax", "tax_amount")}}
                                       for g in summaries.values()]
    processing.setdefault("evidence_links", {})["source_record_ids"] = [r["id"] for r in records if r.get("in_scope") and usable(r)]
    if processing.get("status") not in {"parsing", "needs_scope"}:
        processing["status"] = "needs_completion" if processing.get("issues") else "ready_for_analysis"


def apply(processing, task, **options):
    processing["issues"] = [i for i in processing.get("issues", []) if i["code"] not in ISSUES]
    result, issues = classify(processing.get("records", []), task, **options)
    identified = {entry["file_id"] for entry in result["records"] if entry.get("record_key")}
    uncertain = {entry["file_id"] for entry in result["records"] if entry["status"] == "unverified"}
    conflicting = {entry["file_id"] for entry in result["records"] if entry["status"] == "conflict"}
    processing["issues"] = [i for i in processing["issues"]
                            if not (i["code"] in {"duplicate_period", "invoice_conflict"}
                                    and i.get("source_file_id") in identified - uncertain
                                    and (i["code"] != "invoice_conflict" or i.get("source_file_id") not in conflicting))]
    processing["issues"].extend(issues)
    processing["deduplication"] = result
    refresh(processing)
    from .core_materials import scope_errors
    if (processing.get("status") not in {"parsing", "processing"}
            and any(key != "company_name" for key in scope_errors(task))):
        processing["status"] = "needs_completion"
    return result
