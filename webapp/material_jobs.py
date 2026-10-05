"""SQLite material queue: encrypted inputs, short claims, fenced completion.

Uses the claim/ack idea reviewed in github.com/litements/litequeue, with our
existing business transaction and encryption boundary. No external broker.
Only processes sharing this local database participate in these limits.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from copy import deepcopy
import json
import secrets
import threading
import time

from src.settings import AISettings, flag, integer
from src.ai_transport import REQUEST_GUARD
from src.input_errors import InputError
from src import material_security, periods, material_provenance
from webapp import material_batches as batches, material_operations as operations
from webapp.access import AccessDenied, current_actor


@dataclass(frozen=True)
class QueueSettings:
    enabled: bool = True
    workers: int = 4
    ai_concurrency: int = 4
    capacity: int = 100
    per_org: int = 2
    attempts: int = 2
    lease: int = 60

    @classmethod
    def from_env(cls):
        return cls(flag('TAXPEARLS_MATERIAL_QUEUE_ENABLED', True),
                   integer('TAXPEARLS_MATERIAL_WORKERS', 4, 1, 32),
                   integer('TAXPEARLS_AI_CONCURRENCY', 4, 1, 64),
                   integer('TAXPEARLS_MATERIAL_QUEUE_CAPACITY', 100, 1, 1000),
                   integer('TAXPEARLS_MATERIAL_ORG_CONCURRENCY', 2, 1, 32),
                   integer('TAXPEARLS_MATERIAL_JOB_ATTEMPTS', 2, 1, 3))


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS material_jobs (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
            batch_id TEXT NOT NULL REFERENCES material_batches(id),
            input_revision INTEGER NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
            actor_role TEXT NOT NULL, operation_id TEXT REFERENCES material_operations(id),
            state TEXT NOT NULL CHECK(state IN ('queued','running','done','failed','superseded')),
            attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL,
            available_at REAL NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
            finished_at TEXT, failure_code TEXT, result_revision INTEGER,
            input_cipher BLOB NOT NULL, result_cipher BLOB
        );
        CREATE INDEX IF NOT EXISTS material_jobs_batch ON material_jobs(batch_id,created_at,id);
        CREATE INDEX IF NOT EXISTS material_jobs_queue ON material_jobs(state,available_at);
        CREATE TABLE IF NOT EXISTS material_ai_slots (
            token TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES material_jobs(id),
            lease_until REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS material_queue_limits (
            id INTEGER PRIMARY KEY CHECK(id=1), workers INTEGER NOT NULL,
            ai_concurrency INTEGER NOT NULL, per_org INTEGER NOT NULL
        );
    ''')


def limits(db, config, *, configure=False):
    if configure:
        db.execute('INSERT INTO material_queue_limits VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET workers=excluded.workers,ai_concurrency=excluded.ai_concurrency,per_org=excluded.per_org',
                   (config.workers,config.ai_concurrency,config.per_org))
    else:
        db.execute('INSERT OR IGNORE INTO material_queue_limits VALUES (1,?,?,?)',
                   (config.workers,config.ai_concurrency,config.per_org))
    return db.execute('SELECT * FROM material_queue_limits WHERE id=1').fetchone()


def actor(job):
    return {'id': job['actor_id'], 'org_id': job['org_id'], 'role': job['actor_role']}


def event(db, job, action, detail=None):
    batch = db.execute('SELECT * FROM material_batches WHERE id=?', (job['batch_id'],)).fetchone()
    batches._event(db, batch, actor(job), action, {'attempt': job['attempts'], **(detail or {})},
                   job_id=job['id'], revision=job['input_revision'], system=True)


def submit(store, user, uploads, mode, rules, *, client_id=None, batch_id=None, revision=None, scope_context=None):
    """Persist originals, version, queue item and request commit in one transaction."""
    format_checks = batches.validate_uploads(uploads)
    names = [name for name, _ in uploads]
    if len(set(names)) != len(names) or any('/' in name or '\\' in name for name in names):
        raise batches.MaterialStorageError('同批文件名须唯一且不能包含目录路径。')
    settings = AISettings.from_env()
    if mode == 'ai' and settings.problem():
        raise batches.MaterialStorageError(settings.problem())
    config = QueueSettings.from_env()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT COUNT(*) FROM material_jobs WHERE state IN ('queued','running')").fetchone()[0]
        replaceable = db.execute("SELECT COUNT(*) FROM material_jobs WHERE batch_id=? AND state='queued'",(batch_id,)).fetchone()[0] if batch_id else 0
        if active-replaceable >= config.capacity:
            raise AccessDenied('处理队列已满，请稍后再提交。', 429)
        if batch_id:
            batch = batches._authorize(db, user, batch_id)
            if batch['revision'] != revision:
                raise AccessDenied('材料已变化，请重新读取后补传。', 409)
        else:
            current_actor(db,user,batches.ROLES)
            if client_id:
                client = db.execute('SELECT * FROM clients WHERE id=? AND org_id=?',(client_id,user['org_id'])).fetchone()
                if not client or user['role']=='accountant' and client['accountant_id']!=user['id']:
                    raise AccessDenied()
            batch_id = secrets.token_hex(16)
            db.execute('INSERT INTO material_batches VALUES (?,?,?,?,0,?)',
                       (batch_id,user['org_id'],user['id'],client_id,batches.members.now()))
            batch = batches._authorize(db, user, batch_id)
            files = batches._files(db,batch,user,uploads,format_checks=format_checks)
            batches._event(db,batch,user,'upload',{'files':files})
        previous_files = db.execute('SELECT COUNT(*) FROM material_originals WHERE batch_id=?', (batch_id,)).fetchone()[0]
        if batch and revision is not None:
            if previous_files + len(uploads) > 20:
                raise batches.MaterialStorageError('单批次累计最多 20 份原件。')
            files = batches._files(db, batch, user, uploads, format_checks=format_checks)
            batches._event(db, batch, user, 'supplement', {'files': files, 'previous_revision': revision})
        files = db.execute('SELECT id FROM material_originals WHERE batch_id=? AND deleted_at IS NULL ORDER BY rowid', (batch_id,)).fetchall()
        new_revision = batches._append(db, batch, user, 'material_change', {'reason': 'queued_upload', 'files': [f['id'] for f in files]})
        job_id = secrets.token_hex(16)
        options = asdict(settings)
        options.pop('api_key')
        options.pop('backup_api_keys', None)
        payload = {'files': [f['id'] for f in files], 'mode': mode, 'settings': options,
                   'rules': [asdict(rule) for rule in rules]}
        if scope_context is not None:
            payload['scope_context'] = deepcopy(scope_context)
        operation = operations.ACTIVE.get()
        db.execute('''INSERT INTO material_jobs
            (id,org_id,batch_id,input_revision,actor_id,actor_role,operation_id,state,available_at,created_at,input_cipher)
            VALUES (?,?,?,?,?,?,?,'queued',?,?,?)''',
            (job_id,user['org_id'],batch_id,new_revision,user['id'],user['role'],
             operation['id'] if operation else None,time.time(),batches.members.now(),
             batches._seal(batches._json(payload),batches._context(user['org_id'],batch_id,'job_input',job_id))))
        for old in db.execute("SELECT * FROM material_jobs WHERE batch_id=? AND state='queued' AND id<>?", (batch_id,job_id)).fetchall():
            db.execute("UPDATE material_jobs SET state='superseded',finished_at=? WHERE id=?", (batches.members.now(),old['id']))
            event(db,old,'job_superseded',{'successor_job':job_id})
        job = dict(db.execute('SELECT * FROM material_jobs WHERE id=?', (job_id,)).fetchone())
        event(db,job,'job_queued', {'operation_id': job['operation_id']})
    return {'id': batch_id, 'revision': new_revision, 'job_id': job_id, 'state': 'queued'}


def public(job):
    return {key:job[key] for key in ('id','batch_id','input_revision','state','attempts','created_at',
                                    'started_at','finished_at','failure_code','result_revision')}


def submit_pdf(store, user, batch_id, revision, request, rules):
    """Reserve an explicit, saved PDF extraction before any external request."""
    settings, config = AISettings.from_env(), QueueSettings.from_env()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = batches._authorize(db, user, batch_id)
        if batch['revision'] != revision:
            raise AccessDenied('材料已变化，请重新读取后授权提取。', 409)
        if not config.enabled:
            raise InputError('PDF 片段 AI 提取须启用持久化材料队列。')
        if settings.problem():
            raise InputError(settings.problem())
        if request.get('consent') is not True:
            raise InputError('须明确同意将所选 PDF 片段发送给配置的模型服务。')
        if db.execute("SELECT 1 FROM material_jobs WHERE batch_id=? AND state IN ('queued','running')", (batch_id,)).fetchone():
            raise AccessDenied('请等待当前材料任务完成后再授权提取。', 409)
        if db.execute("SELECT COUNT(*) FROM material_jobs WHERE state IN ('queued','running')").fetchone()[0] >= config.capacity:
            raise AccessDenied('处理队列已满，请稍后提交。', 429)
        record = db.execute("SELECT * FROM material_revisions WHERE batch_id=? AND kind IN ('analysis','edit') ORDER BY revision DESC LIMIT 1", (batch_id,)).fetchone()
        if not record:
            raise AccessDenied('材料尚未完成本地分析。', 409)
        previous = json.loads(batches._open(record['payload_cipher'], batches._context(
            user['org_id'], batch_id, 'revision:' + record['kind'], record['revision'])))
        document_id = request.get('document_id')
        if not isinstance(document_id, str):
            raise InputError('PDF 材料编号无效。')
        doc = next((d for d in previous['documents'] if d['id'] == document_id), None)
        source = doc.get('pdf_selection') if doc else None
        if not doc or doc['kind'] != 'pdf' or doc.get('error') or not source or source.get('pending'):
            raise InputError('请先保存有效 PDF 原始页码片段，再授权 AI 提取。')
        if material_provenance.pdf_reanalysis_required(doc):
            raise InputError('PDF 适配已更新，请先保存并重新分析原件，再单独授权 AI；未入队或发送。')
        if source['last'] - source['first'] + 1 > settings.max_pages:
            raise InputError(f'当前 AI 单文件最多 {settings.max_pages} 页，请先保存更小的原件片段。')
        selected = previous['selections'].get(document_id, {})
        purpose = selected.get('purpose', 'current')
        if purpose == 'excluded':
            raise InputError('已移出的材料不能发送模型，请先保存新的用途。')
        scanned = deepcopy(doc)
        if material_security.scan_pages(scanned) or scanned.get('security', {}).get('findings'):
            raise InputError('材料含可疑指令，已隔离，不能发送模型。')
        start, end = request.get('period_start'), request.get('period_end')
        if not isinstance(start, str) or not isinstance(end, str) or len(start) > 10 or len(end) > 10:
            raise InputError('请明确本次 AI 核对期的起止日期。')
        period = periods.parse_period(start + '至' + end, 'AI 核对期')
        company = previous['company']
        main = periods.parse_period(company.get('period_start', '') + '至' + company.get('period_end', ''), '主期间')
        if purpose == 'current' and period.key != main.key or purpose == 'history' and period.end >= main.start:
            raise InputError('AI 核对期与本期/历史用途不符；本期须匹配已保存主期间，历史须早于主期间。')
        try:
            original_period = periods.parse_period(doc.get('company', {}).get('period'), '原件期间')
        except InputError:
            original_period = None
        if original_period and original_period.key != period.key:
            raise InputError('原件明确期间与 AI 核对期不同，请修改核对期；未入队或发送模型。')
        file_id = doc.get('original_id')
        original = db.execute('SELECT * FROM material_originals WHERE id=? AND batch_id=?', (file_id, batch_id)).fetchone()
        if not original or original['deleted_at']:
            raise AccessDenied('PDF 原件已删除或不可用。', 409)
        options = asdict(settings)
        options.pop('api_key')
        options.pop('backup_api_keys', None)
        job_id = secrets.token_hex(16)
        consent = {'document_id': document_id, 'source_sha256': doc['sha256'],
            'range': {'first': source['first'], 'last': source['last']},
            'period': period.label, 'model': settings.effective_model, 'vision': settings.vision,
            'service_fingerprint': material_provenance.digest(settings.base_url)}
        payload = {'kind': 'pdf_extraction', 'files': [file_id], 'document_id': document_id,
            'analysis_revision': record['revision'], 'consent': consent, 'mode': 'ai', 'settings': options,
            'rules': [asdict(rule) for rule in rules]}
        new_revision = batches._append(db, batch, user, 'material_change', {'reason': 'pdf_extraction', **consent})
        entry = operations.ACTIVE.get()
        db.execute('''INSERT INTO material_jobs
            (id,org_id,batch_id,input_revision,actor_id,actor_role,operation_id,state,available_at,created_at,input_cipher)
            VALUES (?,?,?,?,?,?,?,'queued',?,?,?)''',
            (job_id,user['org_id'],batch_id,new_revision,user['id'],user['role'],entry['id'] if entry else None,
             time.time(),batches.members.now(),batches._seal(batches._json(payload),batches._context(user['org_id'],batch_id,'job_input',job_id))))
        batches._event(db,batch,user,'pdf_extraction_requested',consent,job_id=job_id,revision=new_revision)
        job = db.execute('SELECT * FROM material_jobs WHERE id=?', (job_id,)).fetchone()
        event(db,job,'job_queued')
        return job_id


def listing(db, batch_id):
    return [public(row) for row in db.execute('SELECT * FROM material_jobs WHERE batch_id=? ORDER BY rowid DESC LIMIT 100', (batch_id,))]


def status(store, user, batch_id):
    """Small authorized poll without producing a business view event each second."""
    with store.connect() as db:
        db.execute('BEGIN')
        batch = batches._authorize(db,user,batch_id)
        return {'id':batch_id,'revision':batch['revision'],'jobs':listing(db,batch_id)}


def retry(store, user, batch_id, job_id, revision, *, consent=False):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = batches._authorize(db,user,batch_id)
        old = db.execute('SELECT * FROM material_jobs WHERE id=? AND batch_id=?',(job_id,batch_id)).fetchone()
        if not old or old['state']!='failed' or batch['revision']!=revision or old['input_revision']!=revision:
            raise AccessDenied('任务或材料版本已变化，请重新读取。',409)
        config = QueueSettings.from_env()
        if db.execute("SELECT COUNT(*) FROM material_jobs WHERE state IN ('queued','running')").fetchone()[0]>=config.capacity:
            raise AccessDenied('处理队列已满，请稍后重试。',429)
        snapshot = json.loads(batches._open(old['input_cipher'],batches._context(user['org_id'],batch_id,'job_input',old['id'])))
        if snapshot.get('kind') == 'pdf_extraction' and consent is not True:
            raise InputError('中断的 PDF 提取须重新明确授权，不能自动重复发送。')
        snapshot['settings'] = {key: value for key, value in snapshot['settings'].items()
                                if key not in {'api_key', 'backup_api_keys'}}
        new_revision = batches._append(db,batch,user,'material_change',{'reason':'retry','previous_job':old['id']})
        new_id = secrets.token_hex(16)
        entry = operations.ACTIVE.get()
        db.execute('''INSERT INTO material_jobs
            (id,org_id,batch_id,input_revision,actor_id,actor_role,operation_id,state,available_at,created_at,input_cipher)
            VALUES (?,?,?,?,?,?,?,'queued',?,?,?)''',
            (new_id,user['org_id'],batch_id,new_revision,user['id'],user['role'],entry['id'] if entry else None,
             time.time(),batches.members.now(),batches._seal(batches._json(snapshot),batches._context(user['org_id'],batch_id,'job_input',new_id))))
        batches._event(db,batch,user,'job_retry_requested',{'previous_job':old['id']},job_id=new_id,revision=new_revision)
        job = db.execute('SELECT * FROM material_jobs WHERE id=?',(new_id,)).fetchone()
        event(db,job,'job_queued')
        return new_id


def claim(store, config):
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        budget = limits(db,config)
        expired = db.execute("SELECT * FROM material_jobs WHERE state='running' AND lease_until<?", (now,)).fetchall()
        for row in expired:
            exhausted = row['attempts'] >= config.attempts
            try:
                snapshot = json.loads(batches._open(row['input_cipher'], batches._context(
                    row['org_id'], row['batch_id'], 'job_input', row['id'])))
                if not isinstance(snapshot, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                # A broken expired input must neither replay nor stall other batches.
                db.execute("UPDATE material_jobs SET state='failed',lease_token=NULL,lease_until=NULL,failure_code='input',finished_at=? WHERE id=?",
                           (batches.members.now(), row['id']))
                event(db, row, 'job_failed', {'code': 'input'})
                continue
            # An interrupted external send has an unknown outcome; require renewed action.
            exhausted = exhausted or snapshot.get('kind') == 'pdf_extraction'
            db.execute("UPDATE material_jobs SET state=?,lease_token=NULL,lease_until=NULL,failure_code='interrupted',finished_at=? WHERE id=?",
                       ('failed' if exhausted else 'queued',batches.members.now() if exhausted else None,row['id']))
            event(db,row,'job_interrupted', {'outcome': 'unknown', 'will_retry': not exhausted})
        db.execute('DELETE FROM material_ai_slots WHERE lease_until<?', (now,))
        if db.execute("SELECT COUNT(*) FROM material_jobs WHERE state='running'").fetchone()[0] >= budget['workers']:
            return None
        row = db.execute('''SELECT j.* FROM material_jobs j WHERE j.state='queued' AND j.available_at<=?
            AND NOT EXISTS (SELECT 1 FROM material_jobs r WHERE r.batch_id=j.batch_id AND r.state='running')
            AND (SELECT COUNT(*) FROM material_jobs r WHERE r.org_id=j.org_id AND r.state='running')<?
            ORDER BY j.available_at,j.rowid LIMIT 1''', (now,budget['per_org'])).fetchone()
        if not row:
            return None
        job = dict(row)
        try:
            batch = batches._authorize(db, actor(job),job['batch_id'])
        except AccessDenied:
            db.execute("UPDATE material_jobs SET state='failed',failure_code='permission',finished_at=? WHERE id=?", (batches.members.now(),job['id']))
            event(db,job,'job_failed', {'code':'permission'})
            return None
        if batch['revision'] != job['input_revision']:
            db.execute("UPDATE material_jobs SET state='superseded',finished_at=? WHERE id=?", (batches.members.now(),job['id']))
            event(db,job,'job_superseded')
            return None
        job.update(lease_token=secrets.token_hex(16),lease_until=now+config.lease,attempts=job['attempts']+1)
        db.execute("UPDATE material_jobs SET state='running',lease_token=?,lease_until=?,attempts=?,started_at=?,failure_code=NULL WHERE id=? AND state='queued'",
                   (job['lease_token'],job['lease_until'],job['attempts'],batches.members.now(),job['id']))
        event(db,job,'job_started')
        return job


def heartbeat(store, job, config):
    with store.connect() as db:
        updated = db.execute("UPDATE material_jobs SET lease_until=? WHERE id=? AND state='running' AND lease_token=? AND lease_until>?",
                             (time.time()+config.lease,job['id'],job['lease_token'],time.time()))
        if updated.rowcount:
            db.execute('UPDATE material_ai_slots SET lease_until=MAX(lease_until,?) WHERE job_id=?', (time.time()+config.lease,job['id']))


@contextmanager
def ai_slot(store, job, config, timeout=600):
    token = secrets.token_hex(16)
    deadline = time.monotonic()+timeout
    while True:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,lease_token,lease_until FROM material_jobs WHERE id=?', (job['id'],)).fetchone()
            if not row or row['state'] != 'running' or row['lease_token'] != job['lease_token'] or row['lease_until'] <= time.time():
                raise AccessDenied('任务领取已过期。',409)
            batch = batches._authorize(db, actor(job), job['batch_id'])
            if batch['revision'] != job['input_revision']:
                raise AccessDenied('材料已变化，未发送至模型。', 409)
            budget = limits(db,config)
            db.execute('DELETE FROM material_ai_slots WHERE lease_until<?',(time.time(),))
            if db.execute('SELECT COUNT(*) FROM material_ai_slots').fetchone()[0] < budget['ai_concurrency']:
                # Keep a crashed caller's reservation through its HTTP timeout.
                db.execute('INSERT INTO material_ai_slots VALUES (?,?,?)', (token,job['id'],time.time()+timeout+config.lease))
                break
        if time.monotonic()>=deadline:
            raise TimeoutError()
        time.sleep(.05)
    try:
        yield
    finally:
        with store.connect() as db:
            db.execute('DELETE FROM material_ai_slots WHERE token=?', (token,))


def inputs(store, job):
    """Only authenticated originals and reusable candidates from this batch."""
    with store.connect() as db:
        db.execute('BEGIN')
        batch = batches._authorize(db,actor(job),job['batch_id'])
        if batch['revision'] != job['input_revision']:
            raise AccessDenied('材料已变化。', 409)
        payload = json.loads(batches._open(job['input_cipher'], batches._context(job['org_id'],job['batch_id'],'job_input',job['id'])))
        # Credentials are runtime configuration, never a reusable task signature.
        # Older queued inputs may contain a key pool; do not carry it into results.
        payload['settings'] = {key: value for key, value in payload['settings'].items()
                               if key not in {'api_key', 'backup_api_keys'}}
        versions = db.execute("SELECT * FROM material_revisions WHERE batch_id=? AND revision<=? AND kind IN ('analysis','edit') ORDER BY revision DESC LIMIT 1",
                              (job['batch_id'],job['input_revision'])).fetchone()
        previous = json.loads(batches._open(versions['payload_cipher'],batches._context(job['org_id'],job['batch_id'],'revision:'+versions['kind'],versions['revision']))) if versions else None
        if payload.get('kind') == 'pdf_extraction':
            if not previous or versions['revision'] != payload['analysis_revision']:
                raise AccessDenied('PDF 分析版本已变化。', 409)
            doc = next((d for d in previous['documents'] if d['id'] == payload['document_id']), None)
            if not doc or material_provenance.pdf_reanalysis_required(doc):
                raise InputError('PDF 适配或解析状态已变化，须先重读原件并重新授权；未发送模型。')
            uploads = []
            for file_id in payload['files']:
                row = db.execute('SELECT * FROM material_originals WHERE id=? AND batch_id=?', (file_id, batch['id'])).fetchone()
                if not row or row['deleted_at']:
                    raise AccessDenied('PDF 原件已变化。', 409)
                meta = json.loads(batches._open(row['metadata_cipher'],batches._context(job['org_id'],job['batch_id'],'metadata',file_id)))
                raw = batches._open(row['bytes_cipher'],batches._context(job['org_id'],job['batch_id'],'original',file_id))
                uploads.append((file_id,meta['name'],raw))
            return payload, previous, deepcopy(previous['documents']), uploads
        groups = {}
        for row in db.execute('SELECT * FROM material_jobs WHERE batch_id=? AND result_cipher IS NOT NULL ORDER BY rowid', (job['batch_id'],)):
            cached = json.loads(batches._open(row['result_cipher'],batches._context(job['org_id'],job['batch_id'],'job_result',row['id'])))
            if cached.get('signature', {}).get('kind') == payload.get('kind') and cached.get('signature', {}).get('mode') == payload['mode'] and cached.get('signature', {}).get('settings') == payload['settings'] and cached.get('signature', {}).get('rules') == payload['rules']:
                for file_id in payload['files']:
                    group = [d for d in cached['documents'] if d.get('original_id')==file_id]
                    if group:
                        groups[file_id] = group
        # Published/user-reviewed documents take precedence over worker caches.
        deleted_documents = []
        if previous:
            for file_id in payload['files']:
                group = [deepcopy(d) for d in previous['documents'] if d.get('original_id')==file_id]
                if group:
                    groups[file_id] = group
            for document in previous['documents']:
                file_id = document.get('original_id')
                if file_id in payload['files']:
                    continue
                original = db.execute('SELECT deleted_at FROM material_originals WHERE id=? AND batch_id=?',
                                      (file_id,job['batch_id'])).fetchone()
                if original and original['deleted_at']:
                    marked = deepcopy(document)
                    marked['error'] = '原件已删除；须明确排除此材料后才能确认新的分析。'
                    deleted_documents.append(marked)
        # A ZIP is reused only when all of its parsed entries succeeded.
        reusable = []
        uploads = []
        for file_id in payload['files']:
            row = db.execute('SELECT * FROM material_originals WHERE id=? AND batch_id=?', (file_id,batch['id'])).fetchone()
            if not row or row['deleted_at']:
                raise AccessDenied('原件已变化。',409)
            meta = json.loads(batches._open(row['metadata_cipher'],batches._context(job['org_id'],job['batch_id'],'metadata',file_id)))
            group = groups.get(file_id, [])
            if group and not any(d.get('extraction',{}).get('method')=='ai_failed' for d in group):
                reusable.extend(group)
            else:
                raw = batches._open(row['bytes_cipher'],batches._context(job['org_id'],job['batch_id'],'original',file_id))
                uploads.append((file_id,meta['name'],raw))
        return payload, previous, reusable+deleted_documents, uploads


def finish(store, job, payload=None, signature=None, *, failure=None, retry=False, config=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM material_jobs WHERE id=?', (job['id'],)).fetchone()
        if row['state']!='running' or row['lease_token']!=job['lease_token'] or row['lease_until']<time.time():
            return False
        try:
            batch = batches._authorize(db,actor(job),job['batch_id'])
        except AccessDenied:
            batch = db.execute('SELECT * FROM material_batches WHERE id=?',(job['batch_id'],)).fetchone()
            failure = 'permission'
            payload = None
            retry = False
        stale = batch['revision'] != job['input_revision']
        result_revision = None
        if payload:
            cache = {'documents':payload['documents'],'signature':signature}
            db.execute('UPDATE material_jobs SET result_cipher=? WHERE id=?',
                       (batches._seal(batches._json(cache),batches._context(job['org_id'],job['batch_id'],'job_result',job['id'])),job['id']))
        if stale:
            state = 'superseded'
        elif failure:
            state = 'queued' if retry and job['attempts']<config.attempts else 'failed'
        else:
            result_revision = batches._append(db,batch,actor(job),'analysis',payload,job_id=job['id'])
            state = 'done'
        db.execute('''UPDATE material_jobs SET state=?,failure_code=?,result_revision=?,lease_token=NULL,
            lease_until=NULL,available_at=?,finished_at=? WHERE id=?''',
            (state,failure,result_revision,time.time()+min(5,job['attempts']),
             None if state=='queued' else batches.members.now(),job['id']))
        detail = {'result_revision':result_revision,'code':failure}
        if payload:
            # Immutable event keeps every attempt's extraction provenance even
            # when the encrypted reuse cache is replaced by a later attempt.
            detail['extractions'] = [{'document_id':d['id'],'original_id':d.get('original_id'),
                'fingerprint':d.get('fingerprint'),'extraction':d.get('extraction',{})} for d in payload['documents']]
        event(db,job,'job_'+('retry' if state=='queued' else state),detail)
        return True


class Worker:
    def __init__(self, get_store, process, *, poll=.2):
        self.get_store, self.process, self.poll = get_store, process, poll
        self.stop_event = threading.Event()
        self.threads = []

    def start(self):
        config = QueueSettings.from_env()
        if not config.enabled or any(thread.is_alive() for thread in self.threads):
            return
        with self.get_store().connect() as db:
            db.execute('BEGIN IMMEDIATE')
            limits(db,config,configure=True)
        self.stop_event.clear()
        for number in range(config.workers):
            thread = threading.Thread(target=self.loop,args=(config,),name=f'material-{number}',daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads.clear()

    def loop(self, config):
        while not self.stop_event.is_set():
            try:
                if not self.run_once(config):
                    self.stop_event.wait(self.poll)
            except Exception:
                # A database outage leaves the lease recoverable, no payload logs.
                self.stop_event.wait(1)

    def run_once(self, config=None):
        config = config or QueueSettings.from_env()
        store = self.get_store()
        job = claim(store,config)
        if not job:
            return False
        beat_stop = threading.Event()
        def beat():
            while not beat_stop.wait(config.lease/3):
                try:
                    heartbeat(store,job,config)
                except Exception:
                    # One transient database failure must not end renewal for
                    # the rest of a long model request.
                    continue
        thread = threading.Thread(target=beat,daemon=True)
        thread.start()
        guard = REQUEST_GUARD.set(lambda timeout:ai_slot(store,job,config,timeout))
        context = operations.ACTIVE.set(None)
        signature = {}
        try:
            signature, previous, documents, uploads = inputs(store,job)
            result = self.process(job,signature,previous,documents,uploads)
            failures = [d.get('extraction',{}) for d in result['documents'] if d.get('extraction',{}).get('failure_code') in {'timeout','network','http','configuration'}]
            transient = any(f.get('failure_code') in {'timeout','network'} or f.get('http_status')==429 or f.get('http_status',0)>=500 for f in failures)
            explicit_pdf = signature.get('kind') == 'pdf_extraction'
            finish(store,job,result,signature,failure=('provider_unavailable' if transient else 'provider_error') if failures and not explicit_pdf else None,
                   retry=transient and not explicit_pdf,config=config)
        except (AccessDenied,batches.MaterialStorageError,InputError):
            finish(store,job,failure='input_or_permission',config=config)
        except Exception:
            finish(store,job,failure='processing',retry=signature.get('kind') != 'pdf_extraction',config=config)
        finally:
            REQUEST_GUARD.reset(guard)
            operations.ACTIVE.reset(context)
            beat_stop.set()
            thread.join(timeout=1)
        return True
