"""Project extracted material facts without exposing the internal task envelope."""
from copy import deepcopy

from .core_mapping import ALIASES


CONTEXT_FIELDS = ("unit", "period_start", "period_end", "form_type", "form_name", "column_scope")
SOURCE_FIELDS = ("position", "positions", "raw_value", "source_label", "origin", "rule_id", "quote")


def build(payload):
    """Read-only export. Keep decimals as strings and unknown values as null."""
    task, processing = payload["task"], payload["processing"]
    documents = {doc["id"]: doc for doc in payload.get("documents", [])}

    records = []
    for row in processing.get("records", []):
        kind = row.get("material_type")
        if kind not in ALIASES:
            continue  # Linked audit metrics are not extracted material records.
        fields = {key: deepcopy(row.get(key)) for key in ALIASES[kind]}
        states = {key: row.get("value_states", {}).get(key, "missing" if value is None or value == "" else "present")
                  for key, value in fields.items()}
        document = documents.get(row.get("source_file_id"), {})
        needs_review = bool(row.get("requires_source_review") or
                            ((document.get("pdf_candidate") or document.get("ocr_candidate"))
                             and not document.get("source_reviewed")))
        records.append({
            "id": row["id"], "material_type": kind, "fields": fields,
            "context": {key: deepcopy(row.get(key)) for key in CONTEXT_FIELDS if key in row},
            "source": {"file_id": row.get("source_file_id"), "position": row.get("source_position"),
                       "fields": {key: {name: deepcopy(value) for name, value in source.items() if name in SOURCE_FIELDS}
                                  for key, source in row.get("field_sources", {}).items() if key in fields},
                       "context": {"extracted": {key: deepcopy(value) for key, value in row.get("source_metadata", {}).items() if key in CONTEXT_FIELDS},
                                   "confirmed": {key: deepcopy(value) for key, value in row.get("confirmed_metadata", {}).items() if key in CONTEXT_FIELDS}},
                       "confirmed_fields": {**{key: value for key, value in row.get("confirmed_invoice_statuses", {}).items()
                                            if key in {"red_letter_status", "void_status"} and value},
                                            **({"direction": row["confirmed_metadata"]["direction"]}
                                               if row.get("confirmed_metadata", {}).get("direction") in {"input", "output"} else {})}},
            "quality": {"field_states": states,
                        "requires_source_review": needs_review, "in_scope": row.get("in_scope"),
                        "duplicate_of": row.get("duplicate_of"),
                        **({"business_record": deepcopy(row["dedup"])} if row.get("dedup") else {})},
        })
    return {
        "schema_version": "taxpearls.material-data.v1",
        "task": {key: deepcopy(task.get(key)) for key in ("company_name", "tax_no", "start_date", "end_date")},
        "records": records,
        "sources": [{"file_id": doc["id"], "name": doc["name"], "sha256": doc["sha256"],
                     "material_type": doc["material_type"]} for doc in documents.values()],
    }
