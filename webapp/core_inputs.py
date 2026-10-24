"""Task -> encrypted inputs -> standardized evidence; stops before risk evaluation."""

from copy import deepcopy
from contextlib import contextmanager
from datetime import date
import json
import secrets
from threading import BoundedSemaphore
from urllib.parse import quote

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from src import core_materials, core_mapping, material_format_guard
from src.input_errors import InputError
from src.settings import AISettings
from webapp import ai_keys
from webapp import material_batches as batches
from webapp.access import AccessDenied, current_actor
from webapp import members
from webapp.uploads import multipart

PROCESS_SLOTS = BoundedSemaphore(2)


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS core_input_tasks (id TEXT PRIMARY KEY REFERENCES material_batches(id))")
    from webapp import core_reports
    core_reports.migrate(db)
    from webapp import core_record_index
    core_record_index.migrate(db)


def latest(view, *, writable=False):
    if not view["versions"] or not view["versions"][-1]["payload"].get("core_task"):
        raise AccessDenied("这不是基础资料处理任务。", 404)
    payload = view["versions"][-1]["payload"]
    if writable and payload.get("completion"):
        raise AccessDenied("任务已完成并归档，请先重新打开任务。", 409)
    return payload


def delete_task(store, user, task_id, expected_revision):
    """Purge one core task atomically; enterprise/client and independent audits remain."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = batches._authorize(db, user, task_id)
        current_actor(db, user, {"org_admin"})
        if not db.execute("SELECT 1 FROM core_input_tasks WHERE id=?", (task_id,)).fetchone():
            raise AccessDenied()
        if batch["revision"] != expected_revision:
            raise AccessDenied("任务版本已变化，请重新读取后再删除。", 409)
        if db.execute("SELECT 1 FROM material_jobs WHERE batch_id=? AND state='running'", (task_id,)).fetchone():
            raise AccessDenied("任务正在处理，请处理结束后再删除。", 409)
        db.execute("DELETE FROM material_ai_slots WHERE job_id IN (SELECT id FROM material_jobs WHERE batch_id=?)", (task_id,))
        for table in ("material_jobs", "material_operations", "core_tax_reports", "material_executions",
                      "material_revisions", "material_events", "material_originals"):
            db.execute(f"DELETE FROM {table} WHERE batch_id=?", (task_id,))
        db.execute("DELETE FROM core_mapping_observations WHERE org_id=? AND task_id=?", (user["org_id"], task_id))
        db.execute("DELETE FROM core_business_sources WHERE batch_id=?", (task_id,))
        db.execute("DELETE FROM core_business_index_tasks WHERE batch_id=?", (task_id,))
        db.execute("DELETE FROM core_business_versions WHERE NOT EXISTS (SELECT 1 FROM core_business_sources s "
                   "WHERE s.version_key=core_business_versions.version_key)")
        db.execute("DELETE FROM core_business_records WHERE NOT EXISTS (SELECT 1 FROM core_business_versions v "
                   "WHERE v.record_key=core_business_records.record_key)")
        db.execute("DELETE FROM core_input_tasks WHERE id=?", (task_id,))
        db.execute("DELETE FROM material_batches WHERE id=?", (task_id,))
        members.log(db, user, "core_task_delete", "material_batch", task_id, "permanent")
    return {"id": task_id, "deleted": True}


def delete_file(store, user, task_id, file_id, expected_revision):
    """Remove one original and its current evidence in the same short transaction."""
    view = batches.read(store, user, task_id)
    payload = deepcopy(latest(view, writable=True))
    if view["revision"] != expected_revision:
        raise AccessDenied("任务版本已变化，请重新读取后再删除材料。", 409)
    file = next((item for item in view["files"] if item["id"] == file_id), None)
    if file is None:
        raise AccessDenied()
    if file["deleted_at"]:
        raise AccessDenied("该材料已删除，请重新读取任务。", 410)
    payload["documents"] = [doc for doc in payload["documents"] if doc["id"] != file_id]
    payload["source_options"].pop(file_id, None)
    core_materials.autofill_scope(payload["task"], payload["documents"])
    payload["processing"] = core_materials.prepare(payload["task"], payload["documents"])
    if payload.get("policy_input"):
        from webapp import core_policies
        core_policies.update(payload)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = batches._authorize(db, user, task_id)
        current_actor(db, user, {"org_admin"})
        if batch["revision"] != expected_revision:
            raise AccessDenied("任务版本已变化，请重新读取后再删除材料。", 409)
        batches._require_active_core(db, batch)
        if db.execute("SELECT 1 FROM material_jobs WHERE batch_id=? AND state='running'", (task_id,)).fetchone():
            raise AccessDenied("任务正在处理，请处理结束后再删除材料。", 409)
        db.execute("UPDATE material_originals SET bytes_cipher=NULL,deleted_at=?,deleted_by=? WHERE id=? AND batch_id=?",
                   (members.now(), user["id"], file_id, task_id))
        batches._append(db, batch, user, "edit", payload)
        batches._event(db, batch, user, "delete", {"file_id": file_id, "backup_copies_may_remain": True})


def project(view):
    value = deepcopy(latest(view))
    # Older drafts may predate range modes. Expose a descriptive kind at the
    # projection level without mutating the signed task payload; the range
    # itself remains the user's selection.
    value["range_kind"] = value["task"].get("range_kind") or core_materials.effective_range_kind(value["task"])
    from src import vat_reconciliation as vat
    value["vat_review_current"] = bool(vat.review(value))
    from webapp import core_policies
    core_policies.project(value)
    return {
        **value,
        "scope_errors": scope_errors(value),
        "scope_identity_candidates": core_materials.scope_identity_candidates(value.get("documents", [])),
        "scope_period_candidates": core_materials.scope_period_candidates(value["task"]),
        "id": view["id"],
        "revision": view["revision"],
        "files": view["files"],
    }


def scope_errors(payload):
    errors = core_materials.scope_errors(payload["task"])
    # Document-level identity conflicts block the check; invoice-level ones are
    # counterparties and belong to the rule layer as findings to review.
    errors.update(core_materials.scope_identity_errors(
        payload["task"], payload.get("documents", []), invoices=False))
    for item in payload.get("processing", {}).get("issues", []):
        if item["code"] == "company_conflict" and "tax_no" not in errors:
            errors["tax_no"] = "文件税号与任务企业冲突，请核对原件或排除不属于本企业的材料。"
    return errors


def require_scope(payload):
    errors = scope_errors(payload)
    if errors or not payload["task"].get("scope_confirmed", True):
        raise ValueError("请先核对企业与检查区间：" + "；".join(list(errors.values()) or ["文件读取的字段须确认后才能开始检查。 "]))


def scope_values(value):
    result = {}
    for key in core_materials.SCOPE_FIELDS:
        raw = value.get(key, "")
        if not isinstance(raw, str) or len(raw) > 200:
            raise InputError("企业与检查区间须为不超过200字的文本。")
        result[key] = core_materials.identity(raw) if key == "tax_no" else raw.strip()
    if "range_kind" in value:
        kind = value.get("range_kind")
        if not isinstance(kind, str) or len(kind) > 20:
            raise InputError("检查范围方式无效。")
        result["range_kind"] = kind.strip()
    return result


def company_for(db, store, user, task, client_id=None, *, create=False):
    if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 100):
        raise InputError("企业选择无效。")
    company = db.execute("SELECT * FROM clients WHERE org_id=? AND id=?", (user["org_id"], client_id)).fetchone() if client_id else None
    if client_id:
        if not company or (user["role"] == "accountant" and company["accountant_id"] != user["id"]):
            raise AccessDenied("选定企业不存在或无权访问。", 404)
        if core_materials.identity(company["taxpayer_id"]) != task["tax_no"]:
            raise InputError("所选企业与填写税号不一致，请重新选择或改为新企业。")
    if not company and task["tax_no"]:
        company = next((c for c in db.execute("SELECT * FROM clients WHERE org_id=?", (user["org_id"],))
                        if core_materials.identity(c["taxpayer_id"]) == task["tax_no"]), None)
        if company and user["role"] == "accountant" and company["accountant_id"] != user["id"]:
            raise AccessDenied()
    if company is None and create:
        company = store._upsert_client(db, user, task["company_name"], task["tax_no"])
    return company


def forms(value):
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) > 30
        or any(not isinstance(v, str) or not 0 < len(v.strip()) <= 200 for v in value)
    ):
        raise InputError("适用附表清单须为不超过30项的名称列表。")
    return list(dict.fromkeys(v.strip() for v in value))


def register(app, get_store, get_user, allow, static_dir):
    def actor(session):
        user = get_user(session)
        allow(user, "org_admin", "accountant")
        return user

    async def body(request):
        from webapp.uploads import bounded_stream

        try:
            raw = b"".join([chunk async for chunk in bounded_stream(request, limit=64 * 1024)])
            value = json.loads(raw)
        except (ValueError, UnicodeError, MultiPartException):
            raise HTTPException(422, "请求须为有效JSON。") from None
        if not isinstance(value, dict):
            raise HTTPException(422, "请求须为对象。")
        return value

    def read(user, task_id):
        return project(batches.read_current(get_store(), user, task_id))

    @app.post("/api/enterprise/input-tasks/{task_id}/deduplication/resolve")
    async def resolve_duplicate(task_id: str, request: Request,
                                session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user, store = actor(session), get_store()
        value = await body(request)
        if (set(value) != {"expected_revision", "source_record_id", "expected_versions", "confirm"}
                or type(value.get("expected_revision")) is not int or value["expected_revision"] < 0
                or not isinstance(value.get("source_record_id"), str)
                or not isinstance(value.get("expected_versions"), list)
                or not 1 <= len(value["expected_versions"]) <= 100
                or any(not isinstance(v, str) or len(v) != 64 for v in value["expected_versions"])
                or value.get("confirm") is not True):
            raise HTTPException(422, "请核对冲突版本后明确确认，并提供当前任务版本。")
        view = batches.read_current(store, user, task_id)
        payload = deepcopy(latest(view, writable=True))
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = batches._authorize(db, user, task_id)
            if batch["revision"] != value["expected_revision"] or view["revision"] != batch["revision"]:
                raise AccessDenied("任务版本已变化，请重新读取。", 409)
            from webapp import core_record_index
            core_record_index.choose(db, batch, user, payload, value["source_record_id"], value["expected_versions"])
            batches._append(db, batch, user, "edit", payload)
        return read(user, task_id)

    @app.post("/api/enterprise/input-tasks/record-union")
    async def record_union(request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user, value = actor(session), await body(request)
        if (set(value) != {"task_ids"} or not isinstance(value.get("task_ids"), list)
                or not 1 <= len(value["task_ids"]) <= 100
                or any(not isinstance(v, str) or not 0 < len(v) <= 100 for v in value["task_ids"])):
            raise HTTPException(422, "请选择1至100个任务。")
        with get_store().connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from webapp import core_record_index
            try:
                return core_record_index.union(db, user, value["task_ids"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None

    @app.get("/core-inputs.js")
    def script():
        return FileResponse(static_dir / "core-inputs.js", media_type="application/javascript")

    @app.get("/api/enterprise/input-tasks")
    def listing(session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = actor(session)
        result = []
        for row in batches.listing(get_store(), user, workflow="core", include_latest=True):
            value = row["payload"]
            if value and value.get("core_task"):
                result.append(
                    {
                        "id": row["id"],
                        "revision": row["revision"],
                        "task": value["task"],
                        "status": "archived" if value.get("completion") else value["processing"]["status"],
                        "created_at": row["created_at"],
                        **({"completion": value["completion"]} if value.get("completion") else {}),
                    }
                )
        return result

    @app.post("/api/enterprise/input-tasks")
    async def create(request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = actor(session)
        value = await body(request)
        if set(value) - {
            "company_name",
            "tax_no",
            "start_date",
            "end_date",
            "range_kind",
            "client_id",
            "applicable_forms",
        }:
            raise HTTPException(422, "任务包含未知字段。")
        try:
            fields = scope_values(value)
            applicable = forms(value.get("applicable_forms"))
            store = get_store()
            batches._key()
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current_actor(db, user, batches.ROLES)
                company = company_for(db, store, user, fields, value.get("client_id"),
                                      create=not core_materials.scope_errors(fields))
                client_id = company["id"] if company else None
                task_id = secrets.token_hex(16)
                task = {
                    "id": task_id,
                    "company_id": client_id,
                    "tenant_id": user["org_id"],
                    **fields,
                    "scope_confirmed": not core_materials.scope_errors(fields),
                    "scope_origins": {key: {"kind": "manual"} for key, val in fields.items()
                                      if val and key != "range_kind"},
                    "scope_history": [],
                    "scope_revision": 0,
                    "applicable_forms": applicable,
                }
                payload = {
                    "core_task": True,
                    "task": task,
                    "documents": [],
                    "source_options": {},
                    "processing": core_materials.prepare(task, []),
                }
                db.execute(
                    "INSERT INTO material_batches VALUES (?,?,?,?,0,?)",
                    (task_id, user["org_id"], user["id"], client_id, members.now()),
                )
                db.execute("INSERT INTO core_input_tasks VALUES (?)", (task_id,))
                batch = batches._authorize(db, user, task_id)
                batches._append(db, batch, user, "analysis", payload)
                batches._event(db, batch, user, "create_task", {"company_id": client_id})
            return read(user, task_id)
        except (ValueError, InputError, batches.MaterialStorageError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/enterprise/input-tasks/{task_id}")
    def detail(task_id: str, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        return read(actor(session), task_id)

    @app.delete("/api/enterprise/input-tasks/{task_id}")
    async def remove(task_id: str, request: Request,
                     session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = actor(session)
        value = await body(request)
        if (set(value) != {"expected_revision", "confirm"}
                or type(value.get("expected_revision")) is not int or value["expected_revision"] < 0
                or value.get("confirm") is not True):
            raise HTTPException(422, "请明确确认删除，并提供当前任务版本。")
        return await run_in_threadpool(delete_task, get_store(), user, task_id, value["expected_revision"])

    @app.get("/api/enterprise/input-tasks/{task_id}/data")
    def data(task_id: str, expected_revision: int,
             session: str | None = Cookie(default=None, alias="taxpearls_session")):
        from src import material_export

        view = batches.read_current(get_store(), actor(session), task_id)
        if view["revision"] != expected_revision:
            raise AccessDenied("任务版本已变化，请重新读取后下载。", 409)
        return material_export.build(latest(view))

    @app.delete("/api/enterprise/input-tasks/{task_id}/files/{file_id}")
    async def remove_file(task_id: str, file_id: str, request: Request,
                          session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = actor(session)
        value = await body(request)
        if (set(value) != {"expected_revision", "confirm"}
                or type(value.get("expected_revision")) is not int or value["expected_revision"] < 0
                or value.get("confirm") is not True):
            raise HTTPException(422, "请明确确认删除材料，并提供当前任务版本。")
        await run_in_threadpool(delete_file, get_store(), user, task_id, file_id, value["expected_revision"])
        return read(user, task_id)

    def process(user, task_id, revision, *, ai=False, working_payload=None, vat_review=None):
        if not PROCESS_SLOTS.acquire(blocking=False):
            raise AccessDenied("资料整理繁忙，原件已保存，请稍后重新整理。", 429)
        try:
            return process_work(user, task_id, revision, ai=ai, working_payload=working_payload, vat_review=vat_review)
        finally:
            PROCESS_SLOTS.release()

    def process_work(user, task_id, revision, *, ai=False, working_payload=None, vat_review=None):
        store = get_store()
        view = batches.read_current(store, user, task_id)
        existing = latest(view, writable=True)
        payload = deepcopy(existing if working_payload is None else working_payload)
        if view["revision"] != revision:
            raise AccessDenied("任务版本已变化，请重新读取。", 409)
        documents = []
        calls_remaining = 8
        settings = ai_keys.apply(AISettings.from_env(), store, usage="extraction")
        for meta in view["files"]:
            options = payload["source_options"].get(meta["id"], {})
            if meta["deleted_at"] or options.get("excluded"):
                continue
            if not options.get("material_type"):
                continue
            _, raw = batches.original(store, user, task_id, meta["id"], purpose="read_for_reanalysis")
            with store.connect() as db:
                @contextmanager
                def mapping_write():
                    # Parsing/OCR/model waits run outside transactions. Recheck
                    # live permission and the source revision at every write.
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        batch = batches._authorize(db, user, task_id)
                        if batch["revision"] != revision:
                            raise AccessDenied("任务版本已变化，请重新读取。", 409)
                        start = len(mapper.promotions)
                        yield batch
                        for promotion in mapper.promotions[start:]:
                            batches._event(db, batch, user, "mapping_promotion",
                                           {**promotion, "source_file_id": meta["id"]})

                with db:
                    db.execute("BEGIN")
                    batch = batches._authorize(db, user, task_id)
                    if batch["revision"] != revision:
                        raise AccessDenied("任务版本已变化，请重新读取。", 409)
                mapper = core_mapping.Mapper(db, user["org_id"], task_id, meta["sha256"], ai=ai, settings=settings,
                                             observation_transaction=mapping_write)
                mapper.call_limit = calls_remaining
                try:
                    core_materials.validate_upload_content(raw, options["original_name"], options["material_type"])
                    previous = next(
                        (
                            d
                            for d in payload["documents"]
                            if d["id"] == meta["id"]
                            and d["sha256"] == meta["sha256"]
                            and d.get("ocr_candidate")
                            and (d.get("records") or d.get("excerpts"))
                            and not (d.get("material_type") == "supporting_material" and d.get("issues"))
                            and d.get("parser_version") == core_materials.PARSER_VERSION
                        ),
                        None,
                    )
                    if previous:
                        doc = deepcopy(previous)
                        for part in doc["parts"]:
                            part.update(part.get("parsed_metadata", {}))
                        for record in doc["records"]:
                            record.update(record.get("source_metadata", {}))
                    else:
                        doc = core_materials.parse(
                            raw,
                            options["original_name"],
                            options["material_type"],
                            meta["id"],
                            mapper,
                            ocr=options.get("ocr", False),
                        )
                except (InputError, ValueError, OSError) as exc:
                    doc = {
                        "id": meta["id"],
                        "name": options["original_name"],
                        "sha256": meta["sha256"],
                        "material_type": options["material_type"],
                        "records": [],
                        "parts": [],
                        "issues": [core_materials.issue("parse_failed", str(exc)[:300], meta["id"])],
                    }
                calls_remaining -= mapper.calls
            if doc["material_type"] == "supporting_material":
                prior = next((item for item in payload["documents"] if item["id"] == meta["id"]), None)
                if options.get("source_reviewed") and (
                    prior is None
                    or prior.get("excerpts", []) != doc.get("excerpts", [])
                    or prior.get("parser_version") != doc.get("parser_version")
                ):
                    options["source_reviewed"] = False
                doc["source_reviewed"] = bool(options.get("source_reviewed"))
                documents.append(doc)
                continue
            override = options.get("confirmed_metadata", {})
            if "direction" in override:
                # Old file-wide overrides must not replace per-invoice evidence on reprocessing.
                if override["direction"]:
                    options["source_reviewed"] = False
                override = {key: value for key, value in override.items() if key != "direction"}
                options["confirmed_metadata"] = override
            needs_review = doc.get("ocr_candidate") or doc.get("pdf_candidate")
            doc["issues"] = [i for i in doc["issues"] if i["code"] != "invoice_status_missing"]
            for record in doc["records"]:
                record.pop("confirmed_metadata", None)
                record["source_metadata"] = {
                    k: record.get(k) for k in ["unit", "form_type", "period_start", "period_end", "direction"]
                }
                if needs_review:
                    record["requires_source_review"] = not options.get("source_reviewed", False)
                if doc["material_type"] == "invoice_detail" and options["original_name"].lower().endswith(".pdf"):
                    original = record.setdefault("parsed_invoice_statuses", {k: record.get(k) for k in ["red_letter_status", "void_status"]})
                    record.update(original)
                    confirmed = options.get("invoice_statuses", {}).get(record["id"], {})
                    record["confirmed_invoice_statuses"] = confirmed
                    for key, value in confirmed.items():
                        if value:
                            record[key] = value
                    for key in ["red_letter_status", "void_status"]:
                        record["value_states"][key] = "present" if record.get(key) else "missing"
                    if not all(record.get(k) for k in ["red_letter_status", "void_status"]):
                        doc["issues"].append(core_materials.issue("invoice_status_missing", "票面未证明红蓝字或作废状态，请逐票核实。", meta["id"], record["source_position"]))
            if needs_review and options.get("source_reviewed"):
                doc["issues"] = [i for i in doc["issues"] if i["code"] not in {"ocr_review", "pdf_source_review"}]
                doc["source_reviewed"] = True
            elif needs_review and not any(i["code"] in {"ocr_review", "pdf_source_review"} for i in doc["issues"]):
                doc["issues"].append(core_materials.issue("ocr_review", "OCR候选仍须对照原页核对。", meta["id"]))
                doc["source_reviewed"] = False
            if override:
                for part in doc["parts"]:
                    part["parsed_metadata"] = {k: deepcopy(part[k]) for k in ["unit", "period", "form_type"]}
                    for field in ["unit", "form_type"]:
                        if (
                            override.get(field)
                            and part.get(field) not in {None, "", "unknown", override[field]}
                            and not doc.get("ocr_candidate")
                        ):
                            doc["issues"].append(
                                core_materials.issue(
                                    "metadata_conflict",
                                    "人工核对与文件识别的" + field + "不一致，请核对原件。",
                                    meta["id"],
                                    part["name"],
                                )
                            )
                    if (
                        override.get("period_start")
                        and part.get("period")
                        and part["period"] != [override["period_start"], override["period_end"]]
                        and not doc.get("ocr_candidate")
                    ):
                        doc["issues"].append(
                            core_materials.issue(
                                "metadata_conflict",
                                "人工期间与文件记载期间冲突，请核对原件。",
                                meta["id"],
                                part["name"],
                            )
                        )
                    part["metadata_origin"] = "user_confirmation"
                    if override.get("unit"):
                        part["unit"] = override["unit"]
                    if override.get("period_start") and override.get("period_end"):
                        part["period"] = [override["period_start"], override["period_end"]]
                    if override.get("form_type"):
                        part["form_type"] = override["form_type"]
                for record in doc["records"]:
                    record["confirmed_metadata"] = dict(override)
                    for key in ["unit", "form_type"]:
                        if override.get(key):
                            record[key] = override[key]
                    if override.get("period_start") and override.get("period_end"):
                        record["period_start"], record["period_end"] = override["period_start"], override["period_end"]
            for record in doc["records"]:
                direction = options.get("invoice_directions", {}).get(record["id"])
                if direction and doc["material_type"] == "invoice_detail":
                    record.setdefault("confirmed_metadata", {})["direction"] = direction
                    record["direction"] = direction
            documents.append(doc)
        core_materials.autofill_scope(payload["task"], documents)
        for doc in documents:
            if doc["material_type"] == "invoice_detail":
                for record in doc["records"]:
                    extracted = {**record, "direction": record["source_metadata"].get("direction")}
                    record["automatic_direction"] = core_materials.invoice_direction(extracted, payload["task"].get("tax_no"))
        payload["documents"] = documents
        payload["processing"] = core_materials.prepare(payload["task"], documents)
        if vat_review is not None:
            from src import vat_reconciliation as vat
            checked = vat.make_review(payload, vat_review, user['id'], members.now())
            payload.setdefault('check_reviews', {})[vat.RULE] = checked
            if vat.RULE in payload.get('tax_checks', {}):
                payload['tax_checks'][vat.RULE]['stale'] = True
            from webapp.core_reports import summary
            payload['tax_check_summary'] = summary(payload.get('tax_checks', {}))
        if payload.get('policy_input'):
            from webapp import core_policies
            core_policies.update(payload)
        batches.append_revision(store, user, task_id, revision, "edit", payload)
        return read(user, task_id)

    @app.post("/api/enterprise/input-tasks/{task_id}/files")
    async def upload(
        task_id: str, request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")
    ):
        user = actor(session)
        view = batches.read_current(get_store(), user, task_id)
        payload = deepcopy(latest(view, writable=True))
        try:
            async with multipart(
                request,
                total_limit=batches.MAX_TOTAL + 1024 * 1024,
                max_files=20,
                max_fields=4,
                material_scope=user["org_id"],
            ) as form:
                kind, raw_revision, mode = (
                    form.get("material_type"),
                    form.get("expected_revision"),
                    form.get("mapping_mode", "local"),
                )
                ocr = form.get("ocr", "0")
                if (
                    not isinstance(kind, str)
                    or kind not in core_materials.ALLOWED_FORMATS
                    or not isinstance(raw_revision, str)
                    or not raw_revision.isdecimal()
                    or not isinstance(mode, str)
                    or mode not in {"local", "ai"}
                ):
                    raise InputError("上传类型、版本或映射方式无效。")
                if ocr not in {"0", "1"}:
                    raise InputError("OCR授权须为0或1。")
                uploads = []
                names = {}
                for _, file in form.multi_items():
                    if isinstance(file, UploadFile):
                        name = file.filename or "未命名"
                        stored_name = secrets.token_hex(8) + "__" + name
                        names[stored_name] = name
                        uploads.append((stored_name, await file.read()))
                if not uploads:
                    raise InputError("请选择至少一个文件。")
                if sum(not file["deleted_at"] for file in view["files"]) + len(uploads) > 20:
                    raise InputError("一个任务最多保留20份资料，请拆分检查任务。")
                allowed = core_materials.ALLOWED_FORMATS
                if any(name.rsplit(".", 1)[-1].lower() not in allowed[kind] for name in names.values()):
                    raise InputError("该槽位不支持此文件格式。")
                revision = int(raw_revision)
                if revision != view["revision"]:
                    raise AccessDenied("任务版本已变化，请重新读取。", 409)
                def validate_batch():
                    material_format_guard.check(uploads, wait=True)
                    for stored_name, raw in uploads:
                        core_materials.validate_upload_content(raw, names[stored_name], kind)
                await run_in_threadpool(validate_batch)
                # Preserve raw inputs first. A parser failure can never erase a received original.
                payload["processing"] = {**payload["processing"], "status": "parsing"}
                payload["_incoming_source_options"] = {
                    key: {"material_type": kind, "original_name": name, "ocr": ocr == "1"}
                    for key, name in names.items()
                }
                received_revision = batches.supplement(get_store(), user, task_id, revision, uploads, payload)
                result = await run_in_threadpool(process, user, task_id, received_revision, ai=mode == "ai")
                previous_ids = {file["id"] for file in view["files"]}
                incoming_ids = {file["id"] for file in result["files"]} - previous_ids
                result["upload_review"] = {"pending_files": [doc["name"] for doc in result["documents"]
                    if doc["id"] in incoming_ids and (
                        (doc["material_type"] in core_materials.KINDS and (not doc["records"]
                            or any(record.get("requires_source_review") for record in doc["records"])))
                        or (doc["material_type"] == "supporting_material" and doc.get("issues")))]}
                return result
        except (InputError, batches.MaterialStorageError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/enterprise/input-tasks/{task_id}/prepare")
    async def prepare(
        task_id: str, request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")
    ):
        user = actor(session)
        value = await body(request)
        try:
            if set(value) - {"expected_revision", "applicable_forms", "source_options", "mapping_mode", "policy_input", "vat_review"}:
                raise InputError("请求包含未知字段。")
            view = batches.read_current(get_store(), user, task_id)
            payload = deepcopy(latest(view, writable=True))
            revision = value.get("expected_revision")
            if type(revision) is not int or revision != view["revision"]:
                raise AccessDenied("任务版本已变化，请重新读取。", 409)
            if 'policy_input' in value:
                from src import policies
                payload['policy_input'] = policies.normalize(value['policy_input'])
            if "applicable_forms" in value:
                payload["task"]["applicable_forms"] = forms(value["applicable_forms"])
            changes = value.get("source_options", {})
            if not isinstance(changes, dict) or set(changes) - set(payload["source_options"]):
                raise InputError("只能核对本任务已保存的文件。")
            for file_id, change in changes.items():
                if not isinstance(change, dict) or set(change) - {
                    "confirmed_metadata",
                    "excluded",
                    "source_reviewed",
                    "ocr",
                    "invoice_statuses",
                    "invoice_directions",
                }:
                    raise InputError("资料核对项无效。")
                meta = change.get("confirmed_metadata", {})
                if isinstance(meta, dict) and any(not isinstance(v, str) for v in meta.values()):
                    raise InputError("核对信息须为文本。")
                if not isinstance(meta, dict) or set(meta) - {
                    "unit",
                    "period_start",
                    "period_end",
                    "form_type",
                    "direction",
                }:
                    raise InputError("核对信息含未知字段。")
                if (
                    meta.get("unit") not in {None, "", "元", "千元", "万元", "亿元"}
                    or meta.get("form_type") not in {None, "", "main", "supplement"}
                ):
                    raise InputError("核对单位或表单无效。")
                if meta.get("direction"):
                    raise InputError("发票进销项方向由系统逐票判断，不支持整份文件统一设置。")
                if "direction" in meta:
                    meta = {key: item for key, item in meta.items() if key != "direction"}
                    change["confirmed_metadata"] = meta
                if meta.get("period_start") or meta.get("period_end"):
                    if not all(isinstance(meta.get(k), str) for k in ["period_start", "period_end"]):
                        raise InputError("核对期间须为起止日期文本。")
                    if date.fromisoformat(meta.get("period_start", "")) > date.fromisoformat(
                        meta.get("period_end", "")
                    ):
                        raise InputError("核对期间起止倒置。")
                if "excluded" in change and type(change["excluded"]) is not bool:
                    raise InputError("排除标记须为布尔值。")
                if any(k in change and type(change[k]) is not bool for k in ["source_reviewed", "ocr"]):
                    raise InputError("来源核对与OCR授权须为布尔值。")
                statuses = change.get("invoice_statuses", {})
                if not isinstance(statuses, dict):
                    raise InputError("发票状态核对无效。")
                document = next((d for d in payload["documents"] if d["id"] == file_id), None)
                records = {r["id"]: r for r in document["records"]} if document else {}
                options = payload["source_options"][file_id]
                directions = change.get("invoice_directions", options.get("invoice_directions", {}))
                if "invoice_directions" in change and (not isinstance(directions, dict) or (directions and options["material_type"] != "invoice_detail") or set(directions) - set(records)):
                    raise InputError("只能核对当前文件已识别的逐票进销项方向。")
                if any(not isinstance(item, str) or item not in {"", "input", "output"} for item in directions.values()):
                    raise InputError("逐票进销项方向无效。")
                if statuses and (options["material_type"] != "invoice_detail" or not options["original_name"].lower().endswith(".pdf") or set(statuses) - set(records)):
                    raise InputError("只能核对当前文件已识别的PDF发票状态。")
                for record_id, status in statuses.items():
                    if not isinstance(status, dict) or set(status) - {"red_letter_status", "void_status"} or any(
                        not isinstance(v, str) for v in status.values()
                    ) or status.get("red_letter_status", "") not in {"", "红字", "蓝字"} or status.get("void_status", "") not in {"", "正常", "未作废", "作废", "已作废"}:
                        raise InputError("发票红蓝字或作废状态无效。")
                    for key, new_value in status.items():
                        original = records[record_id].get("parsed_invoice_statuses", records[record_id]).get(key)
                        if original and new_value and original != new_value:
                            raise InputError("核对状态与票面识别值冲突，请核对或排除此原件。")
                if (
                    (meta != options.get("confirmed_metadata", {}) or statuses != options.get("invoice_statuses", {})
                     or directions != options.get("invoice_directions", {}))
                    and "source_reviewed" not in change
                ):
                    change["source_reviewed"] = False
                payload["source_options"][file_id].update(change)
            mode = value.get("mapping_mode", "local")
            if not isinstance(mode, str) or mode not in {"local", "ai"}:
                raise InputError("映射方式无效。")
            if 'vat_review' in value:
                from webapp.knowledge_evidence import VATReviewBody
                from src import vat_reconciliation as vat
                if not isinstance(value['vat_review'], dict):
                    raise InputError('增值税取数核对须为结构化字段。')
                checked = VATReviewBody.model_validate({**value['vat_review'], 'expected_revision': revision})
                review_values = checked.model_dump(exclude={'expected_revision'})
                vat.make_review(payload, review_values, user['id'], members.now())
                # Reparse, bind the reviewed row to the final sources, and save
                # once. Invalid/foreign mappings cannot partly persist options.
                return await run_in_threadpool(process, user, task_id, revision, ai=mode == 'ai',
                                               working_payload=payload, vat_review=review_values)
            revision = batches.append_revision(get_store(), user, task_id, revision, "edit", payload)
            return await run_in_threadpool(process, user, task_id, revision, ai=mode == "ai")
        except (InputError, ValueError, batches.MaterialStorageError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/enterprise/input-tasks/{task_id}/identity-validation")
    async def validate_identity(task_id: str, request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user, store = actor(session), get_store()
        value = await body(request)
        try:
            if set(value) - {*core_materials.SCOPE_FIELDS, "range_kind", "fields", "expected_revision"}:
                raise InputError("请求包含未知字段。")
            fields = value.get("fields")
            if (not isinstance(fields, list) or not fields or len(fields) > 4
                    or any(not isinstance(key, str) or key not in core_materials.SCOPE_FIELDS for key in fields)
                    or len(set(fields)) != len(fields)):
                raise InputError("请选择需要校验的企业信息字段。")
            period_selected = "start_date" in fields or "end_date" in fields
            if period_selected and not {"start_date", "end_date"}.issubset(fields):
                raise InputError("请同时选择检查起始日和终止日。")
            with store.connect() as db:
                db.execute("BEGIN")
                batch = batches._authorize(db, user, task_id)
                if not db.execute("SELECT 1 FROM core_input_tasks WHERE id=?", (task_id,)).fetchone():
                    raise AccessDenied("这不是基础资料处理任务。", 404)
                if type(value.get("expected_revision")) is not int or value["expected_revision"] != batch["revision"]:
                    raise AccessDenied("任务版本已变化，请重新读取。", 409)
                row = db.execute("SELECT * FROM material_revisions WHERE batch_id=? AND revision=?",
                                 (task_id, batch["revision"])).fetchone()
                if not row:
                    raise AccessDenied("当前任务版本不存在，请重新读取。", 409)
                payload = json.loads(batches._open(row["payload_cipher"], batches._context(
                    batch["org_id"], task_id, "revision:" + row["kind"], row["revision"])))
            task = scope_values(value)
            errors = core_materials.scope_identity_errors(task, payload.get("documents", []), invoices=False)
            errors.update(core_materials.identity_field_errors(task))
            if period_selected:
                errors.update({key: message for key, message in core_materials.scope_errors(task).items()
                               if key in {"start_date", "end_date", "range_kind"}})
                if not any(p["start_date"] == task["start_date"] and p["end_date"] == task["end_date"]
                           for p in core_materials.scope_period_candidates(payload["task"])):
                    errors.setdefault("end_date", "所选区间不是当前材料中的完整期间候选，请重新读取并核对。")
            errors = {key: message for key, message in errors.items() if key in fields}
            if period_selected and core_materials.range_error(task):
                errors["range_kind"] = core_materials.range_error(task)
            return {"valid": not errors, "errors": errors}
        except (InputError, ValueError, batches.MaterialStorageError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/enterprise/input-tasks/{task_id}/scope")
    async def update_scope(task_id: str, request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user, store = actor(session), get_store()
        value = await body(request)
        try:
            if set(value) - {
                *core_materials.SCOPE_FIELDS,
                "range_kind",
                "client_id",
                "expected_revision",
                "confirm",
            }:
                raise InputError("请求包含未知字段。")
            if "confirm" in value and type(value["confirm"]) is not bool:
                raise InputError("确认标记须为布尔值。")
            view = batches.read_current(store, user, task_id)
            if value.get("expected_revision") != view["revision"]:
                raise AccessDenied("任务版本已变化，请重新读取。", 409)
            payload = deepcopy(latest(view, writable=True))
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                batch = batches._authorize(db, user, task_id)
                if type(value.get("expected_revision")) is not int or value["expected_revision"] != batch["revision"]:
                    raise AccessDenied("任务版本已变化，请重新读取。", 409)
                task = payload["task"]
                previous_task = deepcopy(task)
                fields = scope_values(value)
                for key, val in fields.items():
                    if key != "range_kind" and val != task.get(key):
                        task.setdefault("scope_origins", {})[key] = {"kind": "manual"}
                task.update(fields)
                range_changed = (
                    previous_task.get("start_date") != task.get("start_date")
                    or previous_task.get("end_date") != task.get("end_date")
                    or core_materials.effective_range_kind(previous_task)
                    != core_materials.effective_range_kind(task)
                )
                if range_changed:
                    # Keep a bounded user-visible trail. The previous revisions
                    # (originals, old processing and frozen reports) are retained
                    # separately and are not used to confirm the new range.
                    history = task.setdefault("scope_history", [])
                    history.append({
                        "start_date": previous_task.get("start_date", ""),
                        "end_date": previous_task.get("end_date", ""),
                        "range_kind": core_materials.effective_range_kind(previous_task),
                        "recorded_at": members.now(),
                    })
                    del history[:-20]
                    task["scope_revision"] = int(task.get("scope_revision") or 0) + 1
                task["scope_confirmed"] = False
                payload["processing"] = core_materials.prepare(task, payload["documents"])
                errors = scope_errors(payload)
                confirm = value.get("confirm", False) and not errors
                company = company_for(db, store, user, task, value.get("client_id"), create=confirm)
                task["company_id"] = company["id"] if company else batch["client_id"]
                task["scope_confirmed"] = confirm
                payload["processing"] = core_materials.prepare(task, payload["documents"])
                if any(previous_task[key] != task[key] for key in core_materials.SCOPE_FIELDS):
                    for slot in payload.get("tax_checks", {}).values():
                        slot.setdefault("input_scope", {key: previous_task[key] for key in core_materials.SCOPE_FIELDS})
                        slot["stale"] = True
                if task != previous_task:
                    db.execute("UPDATE material_batches SET client_id=? WHERE id=?", (task["company_id"], task_id))
                    batches._append(db, batch, user, "edit", payload)
            return read(user, task_id)
        except (InputError, ValueError, batches.MaterialStorageError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/enterprise/input-tasks/{task_id}/originals/{file_id}")
    def original(task_id: str, file_id: str, preview: bool = False,
                 session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = actor(session)
        value = read(user, task_id)
        _, raw = batches.original(get_store(), user, task_id, file_id)
        name = value["source_options"][file_id]["original_name"]
        media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
        inline_type = media.get(name.rsplit(".", 1)[-1].lower()) if preview else None
        return Response(
            raw,
            media_type=inline_type or "application/octet-stream",
            headers={
                "Content-Disposition": ("inline" if inline_type else "attachment") + "; filename*=UTF-8''" + quote(name, safe=""),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
