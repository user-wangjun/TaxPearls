"""Encrypted cross-task record identity, versions and currently active sources.

All callers hold a write transaction and live business permission. The index is
rebuildable from retained task revisions; it never edits completed task snapshots.
Opaque keyed identifiers, not taxpayer IDs/amounts/positions, enter SQL indexes.
"""
from copy import deepcopy
import hashlib
import hmac
import json

from src import core_dedup, core_materials
from webapp import material_batches as batches
from webapp.access import AccessDenied, current_actor


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS core_business_records (
            record_key TEXT PRIMARY KEY, scope_key TEXT NOT NULL, decision_cipher BLOB
        );
        CREATE INDEX IF NOT EXISTS core_business_scope ON core_business_records(scope_key);
        CREATE TABLE IF NOT EXISTS core_business_versions (
            version_key TEXT PRIMARY KEY, record_key TEXT NOT NULL REFERENCES core_business_records(record_key),
            value_cipher BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS core_business_version_record ON core_business_versions(record_key);
        CREATE TABLE IF NOT EXISTS core_business_sources (
            batch_id TEXT NOT NULL REFERENCES material_batches(id), file_id TEXT NOT NULL,
            source_record_id TEXT NOT NULL, record_key TEXT NOT NULL REFERENCES core_business_records(record_key),
            version_key TEXT NOT NULL REFERENCES core_business_versions(version_key),
            active INTEGER NOT NULL, source_cipher BLOB NOT NULL,
            PRIMARY KEY(batch_id,file_id,source_record_id,version_key)
        );
        CREATE INDEX IF NOT EXISTS core_business_source_lookup ON core_business_sources(record_key,active,batch_id);
        CREATE TABLE IF NOT EXISTS core_business_index_tasks (
            batch_id TEXT PRIMARY KEY REFERENCES material_batches(id), revision INTEGER NOT NULL,
            algorithm TEXT NOT NULL
        );
    """)


def keyed(domain, value):
    message = core_dedup.canonical([core_dedup.VERSION, domain, value]).encode()
    return hmac.new(batches._key(), message, hashlib.sha256).hexdigest()


def keys(org, task):
    scope = keyed("scope", [org, core_dedup.token(task.get("tax_no"))])
    return scope, lambda domain, value: keyed(domain, [scope, value])


def context(record, version, kind):
    return batches._json(["core-business-record-v1", record, version, kind])


def authorized_tasks(db, actor):
    current_actor(db, actor, batches.ROLES)
    sql = ("SELECT b.* FROM material_batches b JOIN core_input_tasks ci ON ci.id=b.id "
           "LEFT JOIN clients c ON c.id=b.client_id AND c.org_id=b.org_id WHERE b.org_id=? "
           "AND (b.client_id IS NULL OR c.id IS NOT NULL)")
    args = [actor["org_id"]]
    if actor["role"] == "accountant":
        sql += " AND ((b.client_id IS NULL AND b.owner_id=?) OR c.accountant_id=?)"
        args.extend([actor["id"], actor["id"]])
    return [dict(row) for row in db.execute(sql, args)]


def index_payload(db, batch, payload, revision):
    """Replace only this task's current source memberships; versions stay immutable."""
    indexed = db.execute("SELECT revision,algorithm FROM core_business_index_tasks WHERE batch_id=?", (batch["id"],)).fetchone()
    if indexed and indexed["revision"] == revision and indexed["algorithm"] == core_dedup.VERSION:
        return
    task = payload.get("task", {})
    scope, key_for = keys(batch["org_id"], task)
    originals = {r["id"] for r in db.execute("SELECT id FROM material_originals WHERE batch_id=? AND deleted_at IS NULL", (batch["id"],))}
    docs = {d["id"]: d for d in payload.get("documents", [])}
    blocked_files = {i.get("source_file_id") for i in payload.get("processing", {}).get("issues", [])
                     if i["code"] in {"company_conflict", "invoice_company_conflict", "company_name_conflict"}}
    db.execute("UPDATE core_business_sources SET active=0 WHERE batch_id=?", (batch["id"],))
    # Recompute only for old payloads which predate complete duplicate-source retention.
    processing = payload.get("processing", {})
    if not processing.get("deduplication") and docs:
        processing = core_materials.prepare(task, list(docs.values()))
    for record in processing.get("records", []):
        file_id = record.get("source_file_id")
        document = docs.get(file_id, {})
        if (file_id not in originals or file_id in blocked_files or None in blocked_files
                or (document.get("ocr_candidate") or document.get("pdf_candidate")) and not document.get("source_reviewed")
                or payload.get("source_options", {}).get(file_id, {}).get("excluded")):
            continue
        description = core_dedup.describe(record, task)
        if description is None:
            continue
        identity, value = description
        record_key = key_for("identity", identity)
        version_key = key_for("value", [record_key, value])
        db.execute("INSERT OR IGNORE INTO core_business_records(record_key,scope_key) VALUES (?,?)", (record_key, scope))
        value_cipher = batches._seal(batches._json(value), context(record_key, version_key, "value"))
        db.execute("INSERT OR IGNORE INTO core_business_versions VALUES (?,?,?)", (version_key, record_key, value_cipher))
        source = {"task_id": batch["id"], "file_id": file_id, "source_record_id": record["id"],
                  "position": record.get("source_position"), "file_name": docs.get(file_id, {}).get("name"),
                  "completed": bool(payload.get("completion")), "version_key": version_key,
                  "period": [task.get("start_date"), task.get("end_date")], "values": value}
        source_cipher = batches._seal(batches._json(source), context(record_key, version_key, [batch["id"], file_id, record["id"]]))
        db.execute("INSERT INTO core_business_sources VALUES (?,?,?,?,?,1,?) "
                   "ON CONFLICT(batch_id,file_id,source_record_id,version_key) DO UPDATE SET active=1,source_cipher=excluded.source_cipher",
                   (batch["id"], file_id, record["id"], record_key, version_key, source_cipher))
    db.execute("INSERT INTO core_business_index_tasks VALUES (?,?,?) ON CONFLICT(batch_id) "
               "DO UPDATE SET revision=excluded.revision,algorithm=excluded.algorithm",
               (batch["id"], revision, core_dedup.VERSION))


def backfill(db, actor, *, skip=None):
    """Index all authorized legacy tasks, without the history UI's 100-row limit."""
    candidates = authorized_tasks(db, actor)
    for batch in candidates:
        if batch["id"] == skip:
            continue
        indexed = db.execute("SELECT revision,algorithm FROM core_business_index_tasks WHERE batch_id=?", (batch["id"],)).fetchone()
        if indexed and indexed["revision"] == batch["revision"] and indexed["algorithm"] == core_dedup.VERSION:
            continue
        row = db.execute("SELECT kind,payload_cipher FROM material_revisions WHERE batch_id=? AND revision=?", (batch["id"], batch["revision"])).fetchone()
        if row is None:
            continue
        payload = json.loads(batches._open(row["payload_cipher"], batches._context(
            batch["org_id"], batch["id"], "revision:" + row["kind"], batch["revision"])))
        if payload.get("core_task"):
            index_payload(db, batch, payload, batch["revision"])
    return {b["id"] for b in candidates}


def compare(db, batch, actor, payload, *, revision=None, persist=True):
    """Classify against a single serializable snapshot, including completed tasks."""
    allowed = backfill(db, actor, skip=batch["id"])
    if persist:
        index_payload(db, batch, payload, revision if revision is not None else batch["revision"])
    scope, key_for = keys(batch["org_id"], payload["task"])
    history, decisions = {}, {}
    for entity in db.execute("SELECT * FROM core_business_records WHERE scope_key=?", (scope,)):
        business = entity["record_key"]
        if entity["decision_cipher"]:
            decision = json.loads(batches._open(entity["decision_cipher"], context(business, "", "decision")))
            if decision.get("chosen_in") in allowed:
                decisions[business] = decision
        for row in db.execute("SELECT * FROM core_business_sources WHERE record_key=? AND active=1 AND batch_id<>?",
                              (business, batch["id"])):
            if row["batch_id"] not in allowed:
                continue
            source = json.loads(batches._open(row["source_cipher"], context(business, row["version_key"],
                                [row["batch_id"], row["file_id"], row["source_record_id"]])))
            history.setdefault(business, []).append(source)
    result = core_dedup.apply(payload["processing"], payload["task"], key_for=key_for, history=history, decisions=decisions)
    # New tasks never inherit unselected historical records. Readbacks can expose
    # later conflicts without rewriting an earlier revision or frozen report.
    if payload.get("tax_checks"):
        from src.kg_retrieval.metrics import mark_stale
        mark_stale(payload)
    return result


def observe(db, batch, actor, payload):
    """Read-time overlay for active tasks; completion snapshots remain exact."""
    if payload.get("completion"):
        backfill(db, actor)
        return
    compare(db, batch, actor, payload)


def signature(payload):
    return core_dedup.canonical([{key: row.get(key) for key in ("id", "duplicate_of", "dedup_conflict", "dedup_superseded")}
                                 for row in payload.get("processing", {}).get("records", [])])


def require_current(db, batch, actor, payload):
    checked = deepcopy(payload)
    compare(db, batch, actor, checked)
    if signature(checked) != signature(payload):
        raise AccessDenied("历史账目或去重版本已变化，请重新读取并核对后再分析。", 409)


def choose(db, batch, actor, payload, record_id, expected_versions):
    """Human choice is bound to current task revision AND the observed versions."""
    result = compare(db, batch, actor, payload)
    entry = next((e for e in result["records"] if e["source_record_id"] == record_id), None)
    row = next((r for r in payload["processing"]["records"] if r["id"] == record_id), None)
    if not entry or entry["status"] != "conflict" or sorted(expected_versions) != entry["conflict_versions"]:
        raise AccessDenied("重复核对依据已变化，请重新读取后再确认。", 409)
    if row.get("requires_source_review"):
        raise AccessDenied("请先对照原件完成来源核对。", 409)
    decision = {"selected": entry["version_key"], "versions": entry["conflict_versions"], "chosen_in": batch["id"]}
    cipher = batches._seal(batches._json(decision), context(entry["record_key"], "", "decision"))
    db.execute("UPDATE core_business_records SET decision_cipher=? WHERE record_key=?", (cipher, entry["record_key"]))
    batches._event(db, batch, actor, "dedup_version_choice", {"record_key": entry["record_key"],
                                                            "source_record_id": record_id, **decision})
    compare(db, batch, actor, payload)


def union(db, actor, task_ids):
    """A source-level union, never addition of task report totals."""
    selected = []
    task_ids = list(dict.fromkeys(task_ids))
    if not 1 <= len(task_ids) <= 100:
        raise ValueError("请选择1至100个任务。")
    for task_id in task_ids:
        batch = batches._authorize(db, actor, task_id)
        row = db.execute("SELECT kind,payload_cipher FROM material_revisions WHERE batch_id=? AND revision=?", (task_id, batch["revision"])).fetchone()
        if row is None:
            raise AccessDenied()
        payload = json.loads(batches._open(row["payload_cipher"], batches._context(batch["org_id"], task_id, "revision:" + row["kind"], batch["revision"])))
        if not payload.get("core_task"):
            raise AccessDenied()
        selected.append((dict(batch), payload))
    if len({core_dedup.token(p["task"].get("tax_no")) for _, p in selected}) != 1:
        raise ValueError("只能合并同一企业的记录。")
    backfill(db, actor)
    rows = []
    for batch, payload in selected:
        active = {r["id"] for r in db.execute("SELECT id FROM material_originals WHERE batch_id=? AND deleted_at IS NULL", (batch["id"],))}
        for record in payload["processing"].get("records", []):
            if record.get("material_type") not in core_dedup.FIELDS or record.get("source_file_id") not in active:
                continue
            copy = deepcopy(record)
            copy["id"] = batch["id"] + ":" + record["id"]
            rows.append(copy)
    processing = {"records": rows, "issues": [], "status": "ready_for_analysis"}
    # Classify the union as its own set, retaining each task's original scope flag.
    aggregate = {"core_task": True, "task": deepcopy(selected[0][1]["task"]), "processing": processing}
    synthetic = {**selected[0][0], "id": "union"}
    compare(db, synthetic, actor, aggregate, persist=False)
    if processing["deduplication"]["counts"]["unverified"]:
        processing["issues"].append(core_materials.issue("record_identity_incomplete", "部分记录身份信息不足，跨任务合计尚不能确认。"))
        processing["status"] = "needs_completion"
        for summary in processing["invoice_summaries"]:
            summary.update(amount_excluding_tax=None, tax_amount=None, incomplete=True)
    return {"task_ids": task_ids, "processing": processing,
            "boundary": "按所选任务业务记录去重；不同覆盖期间的余额、申报表不能直接相加。"}
