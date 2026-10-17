"""Task -> encrypted inputs -> standardized evidence; stops before risk evaluation."""

from copy import deepcopy
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

from src import core_materials, core_mapping
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


def latest(view):
    if not view["versions"] or not view["versions"][-1]["payload"].get("core_task"):
        raise AccessDenied("这不是基础资料处理任务。", 404)
    return view["versions"][-1]["payload"]


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
        "id": view["id"],
        "revision": view["revision"],
        "files": view["files"],
        "events": view["events"],
        "versions": [
            {"revision": v["revision"], "kind": v["kind"], "created_at": v["created_at"]} for v in view["versions"]
        ],
    }


def scope_errors(payload):
    errors = core_materials.scope_errors(payload["task"])
    for item in payload.get("processing", {}).get("issues", []):
        if item["code"] == "company_conflict":
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
        return project(batches.read(get_store(), user, task_id))

    @app.get("/core-inputs.js")
    def script():
        return FileResponse(static_dir / "core-inputs.js", media_type="application/javascript")

    @app.get("/api/enterprise/input-tasks")
    def listing(session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = actor(session)
        result = []
        for row in batches.listing(get_store(), user, workflow="core"):
            view = batches.read(get_store(), user, row["id"])
            if view["versions"] and view["versions"][-1]["payload"].get("core_task"):
                value = project(view)
                result.append(
                    {
                        "id": value["id"],
                        "revision": value["revision"],
                        "task": value["task"],
                        "status": value["processing"]["status"],
                        "created_at": row["created_at"],
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

    def process(user, task_id, revision, *, ai=False):
        if not PROCESS_SLOTS.acquire(blocking=False):
            raise AccessDenied("资料整理繁忙，原件已保存，请稍后重新整理。", 429)
        try:
            return process_work(user, task_id, revision, ai=ai)
        finally:
            PROCESS_SLOTS.release()

    def process_work(user, task_id, revision, *, ai=False):
        store = get_store()
        view = batches.read(store, user, task_id)
        payload = deepcopy(latest(view))
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
                db.execute("BEGIN IMMEDIATE")
                batches._authorize(db, user, task_id)
                mapper = core_mapping.Mapper(db, user["org_id"], task_id, meta["sha256"], ai=ai, settings=settings)
                mapper.call_limit = calls_remaining
                try:
                    previous = next(
                        (
                            d
                            for d in payload["documents"]
                            if d["id"] == meta["id"]
                            and d["sha256"] == meta["sha256"]
                            and d.get("ocr_candidate")
                            and d.get("records")
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
                for promotion in mapper.promotions:
                    batches._event(
                        db,
                        batches._authorize(db, user, task_id),
                        user,
                        "mapping_promotion",
                        {**promotion, "source_file_id": meta["id"]},
                    )
            override = options.get("confirmed_metadata", {})
            needs_review = doc.get("ocr_candidate") or doc.get("pdf_candidate")
            doc["issues"] = [i for i in doc["issues"] if i["code"] != "invoice_status_missing"]
            for record in doc["records"]:
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
                    record["confirmed_metadata"] = override
                    for key in ["unit", "form_type"]:
                        if override.get(key):
                            record[key] = override[key]
                    if override.get("period_start") and override.get("period_end"):
                        record["period_start"], record["period_end"] = override["period_start"], override["period_end"]
                    if override.get("direction"):
                        record["direction"] = override["direction"]
            documents.append(doc)
        core_materials.autofill_scope(payload["task"], documents)
        payload["documents"] = documents
        payload["processing"] = core_materials.prepare(payload["task"], documents)
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
        view = batches.read(get_store(), user, task_id)
        payload = deepcopy(latest(view))
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
                    or kind not in core_materials.KINDS
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
                if len(view["files"]) + len(uploads) > 20:
                    raise InputError("一个任务最多保留20份资料，请拆分检查任务。")
                allowed = core_materials.ALLOWED_FORMATS
                if any(name.rsplit(".", 1)[-1].lower() not in allowed[kind] for name in names.values()):
                    raise InputError("该槽位不支持此文件格式。")
                revision = int(raw_revision)
                if revision != view["revision"]:
                    raise AccessDenied("任务版本已变化，请重新读取。", 409)
                # Preserve raw inputs first. A parser failure can never erase a received original.
                payload["processing"] = {**payload["processing"], "status": "parsing"}
                payload["_incoming_source_options"] = {
                    key: {"material_type": kind, "original_name": name, "ocr": ocr == "1"}
                    for key, name in names.items()
                }
                received_revision = batches.supplement(get_store(), user, task_id, revision, uploads, payload)
                return await run_in_threadpool(process, user, task_id, received_revision, ai=mode == "ai")
        except (InputError, batches.MaterialStorageError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/enterprise/input-tasks/{task_id}/prepare")
    async def prepare(
        task_id: str, request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")
    ):
        user = actor(session)
        value = await body(request)
        try:
            if set(value) - {"expected_revision", "applicable_forms", "source_options", "mapping_mode", "policy_input"}:
                raise InputError("请求包含未知字段。")
            view = batches.read(get_store(), user, task_id)
            payload = deepcopy(latest(view))
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
                    or meta.get("direction") not in {None, "", "input", "output"}
                ):
                    raise InputError("核对单位、表单或方向无效。")
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
                    (meta != options.get("confirmed_metadata", {}) or statuses != options.get("invoice_statuses", {}))
                    and "source_reviewed" not in change
                ):
                    change["source_reviewed"] = False
                payload["source_options"][file_id].update(change)
            mode = value.get("mapping_mode", "local")
            if not isinstance(mode, str) or mode not in {"local", "ai"}:
                raise InputError("映射方式无效。")
            revision = batches.append_revision(get_store(), user, task_id, revision, "edit", payload)
            return await run_in_threadpool(process, user, task_id, revision, ai=mode == "ai")
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
            view = batches.read(store, user, task_id)
            if value.get("expected_revision") != view["revision"]:
                raise AccessDenied("任务版本已变化，请重新读取。", 409)
            payload = deepcopy(latest(view))
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
    def original(task_id: str, file_id: str, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = actor(session)
        value = read(user, task_id)
        _, raw = batches.original(get_store(), user, task_id, file_id)
        name = value["source_options"][file_id]["original_name"]
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
