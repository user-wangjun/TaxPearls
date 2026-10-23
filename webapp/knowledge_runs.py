"""Bounded parallel checks over one authorized material snapshot."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
import json
from threading import BoundedSemaphore, Event, Lock, RLock
from time import monotonic

from anyio import CancelScope
from fastapi import Cookie, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.kg_retrieval import bundle, metrics, relation_search
from src.kg_retrieval.common import tax_type
from src.settings import AISettings
from webapp import ai_keys, core_inputs, core_record_index, material_batches as batches, members
from webapp.access import AccessDenied

WORKERS = 3
_LOCK, _ACTIVE = Lock(), set()
_AI_SLOTS = BoundedSemaphore(WORKERS)


class CheckRunBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)
    rule_ids: list[str] = Field(min_length=1, max_length=100)
    region_code: str = Field(default='CN', max_length=100)
    use_ai: bool = False


def require_idle(user, task_id):
    with _LOCK:
        if (user['org_id'], task_id) in _ACTIVE:
            raise HTTPException(409, '本任务正在检查，请等待结束后重新读取。')


def register(app, get_store, get_user, allow):
    @app.post('/api/knowledge/check-runs')
    def start(body: CheckRunBody, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        from webapp import knowledge_evidence as service
        user, store = get_user(session), get_store()
        allow(user, 'org_admin', 'accountant')
        if not body.use_ai:
            raise HTTPException(422, '请明确选择本轮 AI 检查。')
        if len(set(body.rule_ids)) != len(body.rule_ids) or any(not isinstance(r, str) or not 1 <= len(r) <= 200 for r in body.rule_ids):
            raise HTTPException(422, '检查项目无效或重复。')
        # Authorize before exposing whether a same-organization task is busy.
        with store.connect() as db:
            db.execute('BEGIN')
            batches._authorize(db, user, body.task_id)
        key = (user['org_id'], body.task_id)
        with service._LOCK:
            if any(active[:2] == key for active in service._ACTIVE):
                raise HTTPException(409, '本任务正在检查，请等待结束后重新读取。')
            with _LOCK:
                if key in _ACTIVE:
                    raise HTTPException(409, '本任务正在检查，请等待结束后重新读取。')
                if len(_ACTIVE) >= 4:
                    raise HTTPException(429, '检查任务繁忙，请稍后重试。')
                _ACTIVE.add(key)

        def release():
            with _LOCK:
                _ACTIVE.discard(key)

        try:
            view = batches.read_current(store, user, body.task_id)
            if view['revision'] != body.expected_revision:
                raise AccessDenied('任务版本已变化，请重新读取。', 409)
            payload = core_inputs.latest(view, writable=True)
            core_inputs.require_scope(payload)
            settings = ai_keys.apply(AISettings.from_env(), usage='knowledge')
            profile = ai_keys.context_budget(settings)
            packages = []
            # Material data is loaded once. Every check uses this same snapshot;
            # knowledge queries retain the existing evidence/exception closure.
            for rid in body.rule_ids:
                with store.connect() as db:
                    context = relation_search.rule_context(db, rid)
                    task = {'audit_task_id': body.task_id, 'tax_type': tax_type(context['rule']['tax_type']),
                            'region_code': body.region_code}
                    package = metrics.build_task_bundle(db, payload, task, rid,
                                                        token_budget=profile['input_budget'], offline=True)
                package['batching']['model_context'] = profile
                old = payload.get('tax_checks', {}).get(rid)
                same = old and not old.get('stale') and bundle.content_hash(old['bundle']) == bundle.content_hash(package)
                ai = (old or {}).get('ai') or {}
                cached = bool(same and ai.get('validated') and ai.get('validation_version') == service.AI_VALIDATION_VERSION
                              and ai.get('batch_profile') == service.analysis_profile(settings, profile['output_reserve']))
                packages.append((rid, package, deepcopy(old) if same else None, cached))
        except Exception as exc:
            release()
            if isinstance(exc, ValueError):
                raise HTTPException(422, str(exc)) from None
            raise

        stopped = Event()

        def events():
            state = {'revision': view['revision']}
            guard = RLock()
            snapshot = {k: v for k, v in payload.items() if k not in {'tax_checks', 'tax_check_summary'}}
            completed, errors = [], []
            started = monotonic()
            pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='core-check')
            pending = {}
            todo = iter(packages)

            def authorize():
                with guard:
                    if stopped.is_set():
                        raise AccessDenied('本轮检查已停止，请重新读取任务。', 409)
                    with store.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        current = batches._authorize(db, user, body.task_id)
                        if current['revision'] != state['revision']:
                            raise AccessDenied('资料已变化，请重新核对后再分析。', 409)
                        core_record_index.require_current(db, current, user, snapshot)

            def work(item):
                rid, package, old, cached = item
                if cached:
                    authorize()
                    return {**old, 'cached': True}
                # Bound external concurrency across runs as well as within one run.
                while not _AI_SLOTS.acquire(timeout=.1):
                    if stopped.is_set():
                        raise AccessDenied('本轮检查已停止。', 409)
                token = service._AUTHORIZE_BATCH.set(authorize)
                try:
                    authorize()
                    prior = (old or {}).get('ai')
                    result = service.analyze(package, prior=prior) if prior else service.analyze(package)
                    return {'bundle': package, 'ai': result, 'cached': False, 'stale': False,
                            'analyzed_at': members.now(), 'input_revision': view['revision'],
                            'input_scope': {k: payload['task'][k] for k in core_inputs.core_materials.SCOPE_FIELDS}}
                finally:
                    service._AUTHORIZE_BATCH.reset(token)
                    _AI_SLOTS.release()

            def submit():
                item = next(todo, None)
                if item:
                    pending[pool.submit(work, item)] = item[0]

            def encode(value):
                return json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n'

            try:
                yield encode({'type': 'started', 'revision': state['revision'], 'total': len(packages), 'concurrency': WORKERS})
                for _ in range(WORKERS):
                    submit()
                while pending:
                    ready, _ = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
                    if not ready:
                        authorize()
                        yield encode({'type': 'heartbeat', 'completed': len(completed), 'revision': state['revision']})
                        continue
                    for future in ready:
                        rid = pending.pop(future)
                        try:
                            slot = future.result()
                            # A single coordinator saves results in completion order.
                            # Sibling analyses advance our version; user edits never do.
                            with guard:
                                authorize()
                                if not slot['cached']:
                                    payload.setdefault('tax_checks', {})[rid] = slot
                                    with store.connect() as db:
                                        db.execute('BEGIN IMMEDIATE')
                                        current = batches._authorize(db, user, body.task_id)
                                        if current['revision'] != state['revision']:
                                            raise AccessDenied('资料已变化，请重新读取。', 409)
                                        core_record_index.require_current(db, current, user, snapshot)
                                        state['revision'] = batches._append(db, current, user, 'analysis', payload)
                                failed = any(b.get('status') in {'failed', 'invalid'} for b in (slot.get('ai') or {}).get('batches', []))
                                if failed:
                                    errors.append({'status': 502, 'message': slot['ai'].get('reason', 'AI 批次未完成，请重新开始检查。')})
                                else:
                                    completed.append(rid)
                                event = {'type': 'result', 'rule_id': rid, 'completed': not failed,
                                         'result': {**slot, 'revision': state['revision'], 'saved': True}}
                            yield encode(event)
                        except (HTTPException, AccessDenied) as exc:
                            errors.append({'status': exc.status_code if isinstance(exc, HTTPException) else exc.status,
                                           'message': str(exc.detail) if isinstance(exc, HTTPException) else str(exc)})
                            # A stale/unauthorized snapshot must stop pending paid work.
                            if errors[-1]['status'] in {403, 404, 409}:
                                stopped.set()
                        if not errors:
                            submit()
                if errors:
                    yield encode({'type': 'error', **errors[0], 'revision': state['revision'], 'completed_rule_ids': completed})
                else:
                    yield encode({'type': 'complete', 'revision': state['revision'], 'completed_rule_ids': completed,
                                  'elapsed_seconds': round(monotonic() - started, 3)})
            except (HTTPException, AccessDenied) as exc:
                yield encode({'type': 'error', 'status': exc.status_code if isinstance(exc, HTTPException) else exc.status,
                              'message': str(exc.detail) if isinstance(exc, HTTPException) else str(exc)})
            except Exception:
                yield encode({'type': 'error', 'status': 500, 'message': '本轮检查未完成，请重新读取已保存结果后重试。'})
            finally:
                stopped.set()
                for future in pending:
                    future.cancel()
                pool.shutdown(wait=True, cancel_futures=True)
                release()

        async def stream():
            iterator = events()
            try:
                while (event := await run_in_threadpool(next, iterator, None)) is not None:
                    yield event
            finally:
                # Disconnect/reset stops queued requests. Shield cleanup until
                # already running calls return; no detached scheduler survives.
                stopped.set()
                with CancelScope(shield=True):
                    await run_in_threadpool(iterator.close)
                release()

        return StreamingResponse(stream(), media_type='application/x-ndjson',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
