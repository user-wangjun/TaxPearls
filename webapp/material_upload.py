"""Authenticated preview/review/batch import routes; raw uploads remain in RAM."""
from __future__ import annotations

import json
import secrets
import time
from threading import BoundedSemaphore, RLock

from fastapi import BackgroundTasks, Cookie, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.datastructures import UploadFile

from src import engine, materials
from src.ai_extraction import AIExtractor
from src.settings import AISettings


class MemoryMultipart(MultiPartParser):
    spool_max_size = materials.MAX_FILE + 1

    def on_part_begin(self):
        self.part_bytes = 0
        super().on_part_begin()

    def on_part_data(self, data, start, end):
        self.part_bytes += end - start
        if self._current_part.file is not None and self.part_bytes > materials.MAX_FILE:
            raise MultiPartException("单个文件不能超过 10MB。")
        super().on_part_data(data, start, end)


async def bounded_stream(request, limit):
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise MultiPartException("请求体过大。")
        yield chunk


def register(app, get_user, allow, save_audit, rules_dir):
    drafts = {}
    jobs = {}
    ai_slots = BoundedSemaphore(2)
    lock = RLock()

    def purge():
        for token in list(drafts):
            if drafts[token]["expires"] <= time.monotonic():
                del drafts[token]
        for key in list(jobs):
            if jobs[key]["state"] != "processing" and jobs[key]["expires"] <= time.monotonic():
                del jobs[key]

    def field_catalog():
        return {key: detail for rule in engine.load_rules(rules_dir) for key, detail in rule.inputs.items()}

    def ai_settings():
        try:
            return AISettings.from_env()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/materials/config")
    def material_config(session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = get_user(session)
        allow(user, "org_admin", "accountant", "teacher", "platform_admin")
        return ai_settings().public_status()

    def save_preview(docs, catalog, user):
        with lock:
            purge()
            own = [key for key, draft in drafts.items() if draft["owner"] == user["id"]]
            if len(own) >= 3:
                del drafts[own[0]]
            if len(drafts) >= 20:
                raise HTTPException(429, "待核对任务较多，请稍后再试。")
            token = secrets.token_urlsafe(32)
            drafts[token] = {"owner": user["id"], "expires": time.monotonic() + 600,
                             "docs": docs, "catalog": catalog, "result": None}
        public = [{k: v for k, v in doc.items() if k not in {"accounts", "declarations"}} for doc in docs]
        return {"token": token, "documents": public, "fields": catalog, "expires_in": 600}

    def run_ai(job_id, uploads, catalog, user, settings):
        try:
            docs = materials.preview(uploads, set(catalog), AIExtractor(settings, catalog))
            result = save_preview(docs, catalog, user)
            with lock:
                jobs[job_id].update(state="done", result=result, expires=time.monotonic() + 600)
        except (materials.InputError, HTTPException) as exc:
            with lock:
                jobs[job_id].update(state="failed", detail=exc.detail if isinstance(exc, HTTPException) else str(exc), expires=time.monotonic() + 600)
        except Exception:
            with lock:
                jobs[job_id].update(state="failed", detail="AI 提取任务失败，请检查文件及模型配置后重新上传。", expires=time.monotonic() + 600)
        finally:
            uploads.clear()
            ai_slots.release()

    @app.get("/api/materials/jobs/{job_id}")
    def job_status(job_id: str, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = get_user(session)
        allow(user, "org_admin", "accountant", "teacher", "platform_admin")
        with lock:
            purge()
            job = jobs.get(job_id)
            if not job or job["owner"] != user["id"]:
                raise HTTPException(404, "提取任务不存在或已过期，请重新上传。")
            return {k: v for k, v in job.items() if k not in {"owner", "expires"}}

    @app.post("/api/materials/preview")
    async def preview(request: Request, background: BackgroundTasks, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = get_user(session)
        allow(user, "org_admin", "accountant", "teacher", "platform_admin")
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise HTTPException(422, "请以多文件表单上传材料。")
        form = None
        try:
            parser = MemoryMultipart(request.headers, bounded_stream(request, materials.MAX_TOTAL + 1024 * 1024),
                                     max_files=20, max_fields=5, max_part_size=65536)
            form = await parser.parse()
            uploads = [(value.filename or "未命名", await value.read()) for key, value in form.multi_items()
                       if isinstance(value, UploadFile)]
            mode = form.get("extraction", "local")
            if mode not in {"local", "auto", "ai"}:
                raise HTTPException(422, "提取方式无效。")
            catalog = field_catalog()
            settings = ai_settings()
            if mode == "ai" and settings.problem():
                raise HTTPException(422, settings.problem())
            if mode != "local" and not settings.problem():
                with lock:
                    purge()
                    if len(jobs) >= 20 or not ai_slots.acquire(blocking=False):
                        raise HTTPException(429, "AI 正在处理其他材料，请稍后再试。")
                    job_id = secrets.token_urlsafe(24)
                    jobs[job_id] = {"owner": user["id"], "state": "processing", "expires": time.monotonic() + 900}
                background.add_task(run_ai, job_id, uploads, catalog, user, settings)
                from fastapi.responses import JSONResponse
                return JSONResponse({"job_id": job_id, "state": "processing"}, status_code=202)
            docs = await run_in_threadpool(materials.preview, uploads, set(catalog))
        except (materials.InputError, MultiPartException) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            if form is not None:
                await form.close()
        return save_preview(docs, catalog, user)

    def commit(body, user):
        with lock:
            purge()
            draft = drafts.get(body.get("token"))
            if not draft or draft["owner"] != user["id"]:
                raise HTTPException(422, "核对任务不存在或已过期，请重新上传（有效期 10 分钟）。")
            # A repeated submission returns exactly the existing result, never another audit.
            if draft["result"] is not None:
                return draft["result"]
            items = body.get("selections")
            if not isinstance(items, list) or not 1 <= len(items) <= 20 or any(not isinstance(i, dict) for i in items):
                raise HTTPException(422, "请选择 1–20 份有效材料。")
            selections = {i.get("id"): i for i in items if isinstance(i.get("id"), str)}
            if len(selections) != len(items):
                raise HTTPException(422, "材料编号重复或无效。")
            docs = [d for d in draft["docs"] if d["id"] in selections]
            if len(docs) != len(selections):
                raise HTTPException(422, "材料编号无效。")
            mode = body.get("mode")
            if mode not in {"separate", "merge"}:
                raise HTTPException(422, "请选择分别审计或合并审计。")
            company = body.get("company", {})
            if not isinstance(company, dict):
                raise HTTPException(422, "企业信息格式错误。")
            if mode == "merge" and body.get("same_scope") is not True:
                raise HTTPException(422, "合并前请确认所有材料属于同一企业、同一核对期间。")
            groups = [docs] if mode == "merge" else [[doc] for doc in docs]
            results, errors = [], []
            for group in groups:
                names = [d["name"] for d in group]
                try:
                    dataset = materials.build_dataset(group, selections, company if mode == "merge" else {}, set(draft["catalog"]))
                    response = save_audit(dataset, user)
                    results.append({"files": names, "audit": response})
                except (materials.InputError, HTTPException) as exc:
                    errors.append({"files": names, "detail": exc.detail if isinstance(exc, HTTPException) else str(exc)})
            result = {"results": results, "errors": errors}
            # All-validation-failed requests can be corrected in the same review screen.
            if results:
                draft["result"] = result
                draft["docs"] = []
            return result

    @app.post("/api/materials/audit")
    async def audit(request: Request, session: str | None = Cookie(default=None, alias="taxpearls_session")):
        user = get_user(session)
        allow(user, "org_admin", "accountant", "teacher", "platform_admin")
        try:
            chunks = [chunk async for chunk in bounded_stream(request, 2 * 1024 * 1024)]
            body = json.loads(b"".join(chunks))
            if not isinstance(body, dict) or not isinstance(body.get("token"), str):
                raise ValueError()
        except (ValueError, MultiPartException):
            raise HTTPException(422, "核对数据格式错误或超过 2MB。") from None
        return await run_in_threadpool(commit, body, user)
