"""Core input file jobs on the shared SQLite material scheduler."""
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_futures
from contextlib import contextmanager, nullcontext
from contextvars import copy_context
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import secrets
import re
import time

from src import core_materials, core_mapping
from src.ai_transport import REQUEST_GUARD
from src.input_errors import InputError
from src.settings import AISettings, integer
from webapp import ai_keys, material_batches as batches, material_jobs as queue
from webapp.access import AccessDenied


CORRECTION_FIELDS = {
    'vat_declaration': {'item_label', 'item_key', 'declared_value', 'column_scope'},
    'account_balance': {'account_code', 'account_name', 'opening_balance', 'current_debit', 'current_credit', 'closing_balance'},
    'invoice_detail': {'invoice_no', 'issue_date', 'seller_tax_no', 'buyer_tax_no',
                       'amount_excluding_tax', 'tax_amount', 'amount_including_tax'},
}


def correction_fingerprint(document):
    """Bind pre-queue corrections to the exact retained revision, not a new parse."""
    return document.get('parse_fingerprint') or 'legacy:' + hashlib.sha256(batches._json(document)).hexdigest()


def validate_corrections(document, edits):
    """Validate only existing records; users cannot change source/record identity."""
    if not isinstance(edits, dict) or not document:
        raise InputError('只能修改当前文件已识别的记录。')
    records = {r['id']: r for r in document['records']}
    allowed = CORRECTION_FIELDS.get(document['material_type'], set())
    if set(edits) - set(records):
        raise InputError('只能修改当前文件已识别的记录。')
    result = {}
    for record_id, fields in edits.items():
        if not isinstance(fields, dict) or set(fields) - allowed:
            raise InputError('识别修正包含不允许修改的字段。')
        clean = {}
        for key, value in fields.items():
            if key in {'opening_balance', 'closing_balance'} and isinstance(records[record_id].get(key), dict):
                if not isinstance(value, dict) or set(value) != {'debit', 'credit'}:
                    raise InputError('借贷余额须分别核对，不能合并为单一金额。')
                amounts = {}
                for side, raw in value.items():
                    if not isinstance(raw, str) or len(raw) > 100:
                        raise InputError('修正金额须为文本。')
                    amount, state = core_materials.number(raw)
                    if state == 'invalid':
                        raise InputError('修正金额无效。')
                    amounts[side] = amount
                clean[key] = amounts
                continue
            if not isinstance(value, str) or len(value) > 300:
                raise InputError('修正值须为不超过300字的文本。')
            value = value.strip()
            if key in core_materials.NUMBERS:
                amount, state = core_materials.number(value)
                if state == 'invalid':
                    raise InputError('修正金额无效，请保留原单位并填写十进制金额。')
                clean[key] = amount
            elif key in {'account_name', 'item_label'} and not value:
                raise InputError('科目或申报项目名称不能为空。')
            elif key == 'issue_date':
                clean[key] = core_materials.iso_date(value) if value else None
            elif key in {'seller_tax_no', 'buyer_tax_no', 'invoice_no'}:
                if value and not re.fullmatch(r'[A-Za-z0-9]{8,30}', value):
                    raise InputError('票号或税号格式无效。')
                if value and key != 'invoice_no' and not 8 <= len(value) <= 20:
                    raise InputError('税号须为8至20位字母或数字。')
                clean[key] = core_materials.identity(value) if value else None
            else:
                clean[key] = value or None
        if clean:
            result[record_id] = clean
    return result


def apply_corrections(options, doc):
    edits = options.get('record_corrections', {})
    if not edits:
        return
    if options.get('corrections_fingerprint') != doc.get('parse_fingerprint'):
        options['source_reviewed'] = False
        doc['issues'].append(core_materials.issue('correction_stale', '文件识别版本已变化，旧人工修正未套用，请重新核对或恢复识别值。', doc['id']))
        return
    # Values are already normalized at admission. Revalidate record/field binding.
    records = {r['id']: r for r in doc['records']}
    if set(edits) - set(records) or any(set(fields) - CORRECTION_FIELDS.get(doc['material_type'], set()) for fields in edits.values()):
        options['source_reviewed'] = False
        doc['issues'].append(core_materials.issue('correction_stale', '识别记录已变化，人工修正待重新核对。', doc['id']))
        return
    for record_id, fields in edits.items():
        record = records[record_id]
        record['parsed_values'] = {key: deepcopy(record.get(key)) for key in fields}
        record['manual_corrections'] = deepcopy(fields)
        for key, value in fields.items():
            original = deepcopy(record.get('field_sources', {}).get(key, {}))
            record.setdefault('field_sources', {})[key] = {
                'origin': 'user_correction', 'position': record['source_position'],
                'raw_value': deepcopy(value), 'extracted_source': original,
            }
            record[key] = deepcopy(value)
            present = all(v is not None for v in value.values()) if isinstance(value, dict) else value is not None
            record.setdefault('value_states', {})[key] = 'present' if present else 'missing'
            if isinstance(value, dict):
                for side, amount in value.items():
                    subkey = key + '_' + side
                    record['parsed_values'][subkey] = record.get(subkey)
                    record[subkey] = amount
                    record['value_states'][subkey] = 'present' if amount is not None else 'missing'
                    sources = record['field_sources']
                    sources[subkey] = {'origin': 'user_correction', 'position': record['source_position'],
                                      'raw_value': amount, 'extracted_source': deepcopy(sources.get(subkey, {}))}
        required = core_materials.REQUIRED[doc['material_type']]
        missing = [k for k in required if record.get(k) is None or record.get('value_states', {}).get(k) in {'missing', 'invalid'}]
        pos = record['source_position']
        doc['issues'] = [i for i in doc['issues'] if not (i['code'] == 'record_fields' and i['source_position'] == pos)]
        if missing and set(missing) - {'red_letter_status', 'void_status'}:
            doc['issues'].append(core_materials.issue('record_fields', '记录字段仍待核对：' + '、'.join(missing), doc['id'], pos))
        if doc['material_type'] == 'invoice_detail' and set(fields) & {'amount_excluding_tax', 'tax_amount', 'amount_including_tax'}:
            doc['issues'] = [i for i in doc['issues'] if not (i['code'] == 'invoice_total_conflict' and i['source_position'] == pos)]
            amounts = [record.get(k) for k in ('amount_excluding_tax', 'tax_amount', 'amount_including_tax')]
            if all(v is not None for v in amounts) and Decimal(amounts[0]) + Decimal(amounts[1]) != Decimal(amounts[2]):
                doc['issues'].append(core_materials.issue('invoice_total_conflict', '人工修正后金额加税额与价税合计不一致，请核对。', doc['id'], pos))


def fingerprint(meta, options, snapshot):
    if meta['id'] in snapshot.get('legacy_review_bases', {}):
        return snapshot['legacy_review_bases'][meta['id']]
    return hashlib.sha256(batches._json({
        'sha256': meta['sha256'], 'name': options['original_name'],
        'kind': options['material_type'], 'ocr': options.get('ocr', False),
        'parser': core_materials.PARSER_VERSION, 'mapping': snapshot['mapping'],
        'mode': snapshot['mode'], 'settings': snapshot['settings'],
    })).hexdigest()


def submit(store, user, task_id, revision, *, ai=False, payload=None, uploads=None, retry_job=None):
    """Retained originals, core snapshot and queue admission commit atomically."""
    from webapp.core_inputs import latest
    view = batches.read(store, user, task_id, current_only=True)
    if view['revision'] != revision:
        raise AccessDenied('任务版本已变化，请重新读取。', 409)
    saved = latest(view, writable=True)
    payload = deepcopy(payload if payload is not None else saved)
    prepared = batches.prepare_files(user['org_id'], task_id, uploads, batches.validate_uploads(uploads)) if uploads else []
    if sum(not f['deleted_at'] for f in view['files']) + len(prepared) > 20:
        raise InputError('一个任务最多保留20份资料，请拆分检查任务。')
    settings = asdict(ai_keys.apply(AISettings.from_env(), store, usage='extraction'))
    settings.pop('api_key', None)
    settings.pop('backup_api_keys', None)
    with store.connect() as db:
        mappings = [tuple(row) for row in db.execute(
            'SELECT document_type,context,source_label,target_field FROM core_mapping_observations '
            'WHERE org_id=? GROUP BY document_type,context,source_label,target_field HAVING COUNT(*)>=5 '
            'ORDER BY document_type,context,source_label,target_field', (user['org_id'],))]
    snapshot = {'kind': 'core', 'mode': 'ai' if ai else 'local', 'settings': settings, 'parser': core_materials.PARSER_VERSION,
                'mapping': hashlib.sha256(Path(core_mapping.__file__).read_bytes() + batches._json(mappings)).hexdigest()}
    legacy = {doc['id']: correction_fingerprint(doc) for doc in payload['documents']
              if doc.get('records') and not failed(doc) and correction_fingerprint(doc).startswith('legacy:')
              and payload['source_options'].get(doc['id'], {}).get('corrections_fingerprint') == correction_fingerprint(doc)}
    if legacy:
        snapshot['legacy_review_bases'] = legacy
    job_id = secrets.token_hex(16)
    budget_id = job_id
    if retry_job:
        with store.connect() as db:
            old = db.execute('SELECT * FROM material_jobs WHERE id=? AND batch_id=?', (retry_job, task_id)).fetchone()
        if not old or old['state'] != 'failed' or revision != (old['result_revision'] or old['input_revision']):
            raise AccessDenied('任务或材料版本已变化，请重新读取。', 409)
        old_snapshot = json.loads(batches._open(old['input_cipher'], batches._context(user['org_id'], task_id, 'job_input', retry_job)))
        if old_snapshot.get('kind') != 'core' or old_snapshot != snapshot:
            # A different parser profile needs an explicit new prepare, with a new budget.
            raise InputError('解析或模型配置已变化，请使用重新整理资料。')
        budget_id = old['budget_id'] or old['id']
    signature_cipher = batches._seal(batches._json(snapshot), batches._context(user['org_id'], task_id, 'job_input', job_id))
    # All byte hashing, encryption and large payload normalization precede the write lock.
    incoming = payload.pop('_incoming_source_options', {})
    for file_id, meta, _, _ in prepared:
        payload['source_options'][file_id] = incoming[meta['name']]
    payload['processing'] = {**payload['processing'], 'status': 'parsing'}
    batches.prepare_revision(payload)
    cipher = batches._seal(batches._json(payload), batches._context(user['org_id'], task_id, 'revision:analysis', revision + 1))
    metas = [f for f in view['files'] if not f['deleted_at']] + [{'id': fid, **meta} for fid, meta, _, _ in prepared]
    config = queue.QueueSettings.from_env()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = batches._authorize(db, user, task_id)
        batches._require_active_core(db, batch)
        if batch['revision'] != revision:
            raise AccessDenied('任务版本已变化，请重新读取。', 409)
        active = db.execute("SELECT COUNT(*) FROM material_jobs WHERE state IN ('queued','running') AND batch_id<>?", (task_id,)).fetchone()[0]
        if active >= config.capacity:
            raise AccessDenied('处理队列已满，请稍后提交。', 429)
        if prepared:
            files = batches._files(db, batch, user, uploads, prepared=prepared)
            batches._event(db, batch, user, 'supplement', {'files': files, 'previous_revision': revision})
        input_revision = batches._save_revision(db, batch, user, 'analysis', payload, cipher=cipher)
        db.execute("UPDATE material_jobs SET state='superseded',finished_at=? WHERE batch_id=? AND state='queued'", (batches.members.now(), task_id))
        db.execute('''INSERT INTO material_jobs
            (id,org_id,batch_id,input_revision,actor_id,actor_role,state,available_at,created_at,input_cipher,budget_id,kind)
            VALUES (?,?,?,?,?,?,'queued',?,?,?,?,'core')''',
            (job_id,user['org_id'],task_id,input_revision,user['id'],user['role'],time.time(),batches.members.now(),signature_cipher,budget_id))
        for meta in metas:
            options = payload['source_options'].get(meta['id'], {})
            if options.get('material_type') and not options.get('excluded'):
                db.execute("INSERT INTO material_job_files (job_id,file_id,fingerprint,state) VALUES (?,?,?,'queued')",
                           (job_id,meta['id'],fingerprint(meta, options, snapshot)))
        queue.event(db, db.execute('SELECT * FROM material_jobs WHERE id=?', (job_id,)).fetchone(), 'job_queued')
    return job_id


def wait(store, user, task_id, job_id, worker):
    while True:
        status = queue.status(store, user, task_id)
        job = next(j for j in status['jobs'] if j['id'] == job_id)
        if job['state'] not in {'queued', 'running'}:
            if job['state'] == 'superseded':
                raise AccessDenied('任务版本已变化，请重新读取。', 409)
            return
        if not worker.run_once():
            time.sleep(.05)


def reserve_call(store, job):
    """One atomic eight-call budget shared by files and explicit retries."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        queue.check_claim(db, job)
        root = job.get('budget_id') or job['id']
        return db.execute('UPDATE material_jobs SET ai_calls=ai_calls+1 WHERE id=? AND ai_calls<8', (root,)).rowcount == 1


def failed(doc):
    return any(i['code'] in {'parse_failed','ocr_failed','ocr_unavailable','supporting_ocr_failed','supporting_unreadable'}
               for i in doc.get('issues', []))


def parse_file(store, job, snapshot, payload, meta, settings, same_source=None):
    # Identical tabular originals must observe the first validated mapping before
    # resolving their own columns; otherwise parallel files pay for it twice.
    with same_source or nullcontext(), queue.resource_slot(store, job, 'parse'):
        return parse_file_work(store, job, snapshot, payload, meta, settings)


def dispatch_files(store, job, snapshot, payload, metas, settings, file_limit):
    """Acquire a shared slot before creating work, and refill only free channels."""
    def source_key(meta):
        name = payload['source_options'][meta['id']]['original_name']
        return meta['sha256'] if snapshot['mode']=='ai' and name.rsplit('.',1)[-1].lower() in {'xlsx','xls','xml'} else None

    def reserved_parse(meta, token):
        try:
            return parse_file_work(store, job, snapshot, payload, meta, settings)
        finally:
            queue.release_resource(store, token)

    documents = [None] * len(metas)
    pending, running, sources = list(range(len(metas))), {}, set()
    with ThreadPoolExecutor(max_workers=file_limit, thread_name_prefix='core-file') as executor:
        while pending or running:
            for future in [f for f in running if f.done()]:
                index, key = running.pop(future)
                sources.discard(key)
                documents[index] = future.result()
            while pending and len(running)<file_limit:
                index = next((i for i in pending if source_key(metas[i]) not in sources), None)
                if index is None:
                    break
                token = queue.reserve_resource(store, queue.actor(job), 'parse', job=job, duration=660)
                if token is None:
                    break
                pending.remove(index)
                key = source_key(metas[index])
                try:
                    future = executor.submit(copy_context().run, reserved_parse, metas[index], token)
                except BaseException:
                    queue.release_resource(store, token)
                    raise
                running[future] = index, key
                if key is not None:
                    sources.add(key)
            if running:
                wait_futures(running, timeout=.05, return_when=FIRST_COMPLETED)
            elif pending:
                time.sleep(.05)
    return documents


def parse_file_work(store, job, snapshot, payload, meta, settings):
    """Every executor thread owns its connection, mapper and document."""
    file_id, user = meta['id'], queue.actor(job)
    options = payload['source_options'][file_id]
    _, raw = batches.original(store, user, job['batch_id'], file_id, purpose='read_for_reanalysis')
    try:
        core_materials.validate_upload_content(raw, options['original_name'], options['material_type'])
    except InputError as exc:
        doc = {'id':file_id,'name':options['original_name'],'sha256':meta['sha256'],
               'material_type':options['material_type'],'records':[],'parts':[],
               'issues':[core_materials.issue('parse_failed',str(exc)[:300],file_id)]}
        save_file(store, job, file_id, doc)
        return doc
    key = fingerprint(meta, options, snapshot)
    with store.connect() as db:
        queue.check_claim(db, job)
        cached = db.execute('''SELECT f.* FROM material_job_files f JOIN material_jobs j ON j.id=f.job_id
            WHERE j.batch_id=? AND f.file_id=? AND f.fingerprint=? AND f.state='done'
            AND f.result_cipher IS NOT NULL ORDER BY j.rowid DESC LIMIT 1''', (job['batch_id'], file_id, key)).fetchone()
    if cached:
        doc = json.loads(batches._open(cached['result_cipher'], batches._context(job['org_id'],job['batch_id'],'file_result',cached['job_id'] + ':' + file_id)))
        save_file(store, job, file_id, doc, reused=True)
        return doc
    if file_id in snapshot.get('legacy_review_bases', {}):
        prior = next((d for d in payload['documents'] if d['id'] == file_id), None)
        if (not prior or prior.get('parse_fingerprint') or prior.get('sha256') != meta['sha256']
                or correction_fingerprint(prior) != key):
            raise InputError('旧资料核对来源版本已变化，请重新读取。')
        # Keep the old parser provenance. This is a review baseline, never a
        # claim that the retained result was parsed with today's model/config.
        doc = deepcopy(prior)
        for part in doc.get('parts', []):
            part.update(part.pop('parsed_metadata', {}))
        for record in doc['records']:
            record.update(record.get('source_metadata', {}))
            record.update(record.get('parsed_invoice_statuses', {}))
            record.pop('confirmed_metadata', None)
        doc['parse_fingerprint'] = key
        save_file(store, job, file_id, doc, reused=True)
        return doc
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        queue.check_claim(db, job)
        db.execute("UPDATE material_job_files SET state='running',started_at=?,finished_at=NULL WHERE job_id=? AND file_id=?", (time.time(), job['id'], file_id))
    with store.connect() as db:
        @contextmanager
        def mapping_write():
            with db:
                db.execute('BEGIN IMMEDIATE')
                batch = queue.check_claim(db, job)
                start = len(mapper.promotions)
                yield batch
                for promotion in mapper.promotions[start:]:
                    batches._event(db, batch, user, 'mapping_promotion', {**promotion, 'source_file_id': file_id})

        mapper = core_mapping.Mapper(db,user['org_id'],job['batch_id'],meta['sha256'],
            ai=snapshot['mode']=='ai',settings=settings,observation_transaction=mapping_write,
            call_reservation=lambda: reserve_call(store, job),
            ocr_slot=lambda: queue.resource_slot(store, job, 'ocr'))
        guard = REQUEST_GUARD.set(lambda timeout: queue.ai_slot(store, job, queue.QueueSettings.from_env(), timeout))
        try:
            doc = core_materials.parse(raw, options['original_name'], options['material_type'], file_id, mapper, ocr=options.get('ocr', False))
        except AccessDenied:
            raise
        except Exception:
            # A single unexpected parser error must still preserve sibling results.
            doc = {'id':file_id,'name':options['original_name'],'sha256':meta['sha256'],
                   'material_type':options['material_type'],'records':[],'parts':[],
                   'issues':[core_materials.issue('parse_failed','文件解析未完成，原件已保存，可重试失败文件。',file_id)]}
        finally:
            REQUEST_GUARD.reset(guard)
    doc['parse_fingerprint'] = key
    save_file(store, job, file_id, doc)
    return doc

def save_file(store, job, file_id, doc, *, reused=False):
    cipher = batches._seal(batches._json(doc), batches._context(job['org_id'],job['batch_id'],'file_result',job['id'] + ':' + file_id))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        queue.check_claim(db, job)
        db.execute('UPDATE material_job_files SET state=?,result_cipher=?,failure_code=?,finished_at=?,reused=? WHERE job_id=? AND file_id=?',
            ('failed' if failed(doc) else 'done',cipher,'file_processing' if failed(doc) else None,time.time(),int(reused),job['id'],file_id))


def run(store, job, snapshot, config):
    from webapp.core_inputs import latest
    view = batches.read(store, queue.actor(job), job['batch_id'], current_only=True)
    if view['revision'] != job['input_revision']:
        raise AccessDenied('材料版本已变化。',409)
    payload = deepcopy(latest(view, writable=True))
    if snapshot.get('parser') != core_materials.PARSER_VERSION:
        raise InputError('解析程序已变化，请重新整理资料。')
    configured = ai_keys.apply(AISettings.from_env(), store, usage='extraction')
    frozen = AISettings(**snapshot['settings'], api_key=configured.api_key, backup_api_keys=configured.backup_api_keys)
    if (snapshot['mode'] == 'ai' or any(o.get('ocr') for o in payload['source_options'].values())) and not ai_keys.same_profile(configured, frozen):
        raise InputError('模型服务配置已变化，请重新整理。')
    metas = [f for f in view['files'] if not f['deleted_at'] and payload['source_options'].get(f['id'], {}).get('material_type')
             and not payload['source_options'][f['id']].get('excluded')]
    # Independent contexts propagate the AI HTTP guard into executor threads.
    file_limit = min(len(metas), integer('TAXPEARLS_MATERIAL_FILES_PER_TASK', 2, 1, 4))
    if file_limit <= 1:
        # The existing material worker already owns a thread. A single file or
        # serial configuration needs no additional executor or handoff.
        documents = [parse_file(store, job, snapshot, payload, meta, frozen) for meta in metas]
    else:
        documents = dispatch_files(store, job, snapshot, payload, metas, frozen, file_limit)
    documents = [apply_review(payload, meta, deepcopy(doc)) for meta, doc in zip(metas, documents)]
    result = aggregate(payload, documents)
    batches.prepare_revision(result)
    cipher = batches._seal(batches._json(result), batches._context(job['org_id'],job['batch_id'],'revision:analysis',job['input_revision'] + 1))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = queue.check_claim(db, job)
        batches._require_active_core(db, batch)
        result_revision = batches._save_revision(db,batch,queue.actor(job),'analysis',result,job_id=job['id'],cipher=cipher)
        partial = any(failed(d) for d in documents)
        db.execute("UPDATE material_jobs SET state=?,failure_code=?,result_revision=?,lease_token=NULL,lease_until=NULL,finished_at=? WHERE id=?",
                   ('failed' if partial else 'done','partial_files' if partial else None,result_revision,batches.members.now(),job['id']))
        queue.event(db, job, 'job_failed' if partial else 'job_done', {'result_revision':result_revision})


def apply_review(payload, meta, doc):
    options = payload["source_options"][meta["id"]]
    prior = next((item for item in payload["documents"] if item["id"] == meta["id"]), None)
    if options.get('source_reviewed') and prior and correction_fingerprint(prior) != doc.get('parse_fingerprint'):
        options['source_reviewed'] = False
    if doc["material_type"] == "supporting_material":
        prior = next((item for item in payload["documents"] if item["id"] == meta["id"]), None)
        if options.get("source_reviewed") and (
            prior is None
            or prior.get("excerpts", []) != doc.get("excerpts", [])
            or prior.get("parser_version") != doc.get("parser_version")
        ):
            options["source_reviewed"] = False
        doc["source_reviewed"] = bool(options.get("source_reviewed"))
        return doc
    apply_corrections(options, doc)
    override = options.get("confirmed_metadata", {})
    if "direction" in override:
        # Old file-wide overrides must not replace per-invoice evidence on reprocessing.
        if override["direction"]:
            options["source_reviewed"] = False
        override = {key: value for key, value in override.items() if key != "direction"}
        options["confirmed_metadata"] = override
    needs_review = doc.get("ocr_candidate") or doc.get("pdf_candidate") or bool(options.get('record_corrections'))
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
        doc["issues"] = [i for i in doc["issues"] if i["code"] not in {"ocr_review", "pdf_source_review", "ocr_identity_missing"}]
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
    return doc


def aggregate(payload, documents):
    core_materials.autofill_scope(payload["task"], documents)
    for doc in documents:
        if doc["material_type"] == "invoice_detail":
            for record in doc["records"]:
                extracted = {**record, "direction": record["source_metadata"].get("direction")}
                record["automatic_direction"] = core_materials.invoice_direction(extracted, payload["task"].get("tax_no"))
    payload["documents"] = documents
    payload["processing"] = core_materials.prepare(payload["task"], documents)
    if payload.get('policy_input'):
        from webapp import core_policies
        core_policies.update(payload)
    return payload
