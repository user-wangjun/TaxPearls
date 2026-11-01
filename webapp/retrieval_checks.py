"""Platform-only, durable offline retrieval checks; never evaluate in a business corpus."""
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import uuid

from fastapi import Cookie, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from src.kg_retrieval import evaluate, fixtures, semantic_search
from src.kg_retrieval.common import digest, implementation_fingerprint
from webapp.access import current_actor

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / 'tests/fixtures/kg_retrieval/gold_cases.json'
TIMEOUT = 300
_store_getter = None


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS retrieval_check_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
            interval_hours INTEGER NOT NULL DEFAULT 24, next_due REAL,
            revision INTEGER NOT NULL DEFAULT 0, updated_by TEXT, updated_at REAL
        );
        INSERT OR IGNORE INTO retrieval_check_settings(id) VALUES (1);
        CREATE TABLE IF NOT EXISTS retrieval_check_runs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, trigger TEXT NOT NULL, requested_by TEXT,
            created_at REAL NOT NULL, started_at REAL, finished_at REAL, lease_until REAL,
            owner TEXT, source_version TEXT, cases_version TEXT, report_json TEXT, error TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS retrieval_check_single_active
            ON retrieval_check_runs((1)) WHERE status IN ('queued','running');
    ''')


def cases_version():
    return digest(json.loads(CASES.read_text(encoding='utf-8-sig'))['cases'])


def current_versions():
    try:
        return implementation_fingerprint(), cases_version()
    except (OSError, ValueError, KeyError):
        return None, None


def stamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def run_view(row, *, details=False):
    if row is None:
        return None
    value = dict(row)
    report = json.loads(value.pop('report_json') or 'null')
    for key in ('owner', 'lease_until'):
        value.pop(key, None)
    for key in ('created_at', 'started_at', 'finished_at'):
        value[key] = stamp(value[key])
    value.update(case_count=report.get('case_count') if report else None,
                 passed_count=report.get('passed_count') if report else None,
                 metrics=report.get('metrics') if report else None)
    if details:
        value['report'] = report
    return value


def report_gate(report):
    metrics = report.get('metrics', {})
    cases = report.get('cases', [])
    try:
        return (report.get('gate_passed') is True and metrics['recall'] >= .95 and metrics['precision'] >= .8
                and metrics['exception_coverage'] == metrics['change_relation_coverage'] == metrics['source_traceability'] == 1
                and metrics['wrong_period_region_uses'] == metrics['invented_references'] == metrics['duplicate_clauses'] == 0
                and metrics['semantic_ready'] is True and metrics['external_api_calls'] == 0
                and {f'K{i:02}' for i in range(1, 14)} <= {c['id'] for c in cases if c['passed']}
                and all(c['passed'] for c in cases))
    except (KeyError, TypeError):
        return False


def current_gate(store=None):
    store = store or (_store_getter() if _store_getter else None)
    if store is None:
        return None
    with store.connect() as db:
        row = db.execute("SELECT * FROM retrieval_check_runs WHERE status NOT IN ('queued','running') ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
    if row is None:
        return None  # Preserve the existing CLI report until an administrator runs the new checker.
    report = json.loads(row['report_json'] or '{}')
    model, _ = semantic_search.available()
    version, gold = current_versions()
    code, reason = 'passed', '检索检测通过，当前版本可以使用。'
    if not version or not gold:
        code, reason = 'test_files_missing', '检测代码或固定测试案例缺失、损坏，请核对部署文件。'
    elif row['source_version'] != version or row['cases_version'] != gold:
        code, reason = 'version_changed', '检索检测版本已过期，请平台管理员重新运行检测。'
    elif row['status'] != 'passed' or not report_gate(report):
        code, reason = 'failed', row['error'] or '最近一次检索检测未通过，请平台管理员查看检测结果。'
    elif model is None:
        code, reason = 'model_unavailable', '本地语义检索模型未就绪，请平台管理员检查模型配置。'
    elif model.fingerprint not in report.get('model_fingerprints', []):
        code, reason = 'model_changed', '检索模型版本已变化，请平台管理员重新运行检测。'
    return {'passed': code == 'passed', 'code': code, 'reason': reason,
            'report_hash': digest(report), 'data_scope': report.get('data_scope', 'synthetic'),
            'formal_policy_acceptance': False, 'model_fingerprints': report.get('model_fingerprints', []),
            'run_id': row['id']}


def _recover(db, now):
    db.execute("UPDATE retrieval_check_runs SET status='interrupted',finished_at=?,error=? WHERE status='running' AND lease_until<?",
               (now, '上次检测进程中断，请重新运行检测。', now))


def enqueue(store, actor):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, actor, {'platform_admin'})
        _recover(db, time.time())
        if db.execute("SELECT 1 FROM retrieval_check_runs WHERE status IN ('queued','running')").fetchone():
            raise HTTPException(409, '已有检索检测正在排队或运行，请等待结果。')
        identity = uuid.uuid4().hex
        db.execute("INSERT INTO retrieval_check_runs(id,status,trigger,requested_by,created_at) VALUES (?,'queued','manual',?,?)",
                   (identity, actor['id'], time.time()))
        store._log(db, actor, 'retrieval_check_requested', 'retrieval_check', identity)
    return {'id': identity, 'status': 'queued'}


class Schedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)
    interval_hours: int = Field(strict=True, ge=1, le=720)
    revision: int = Field(strict=True, ge=0)


def save_schedule(store, actor, body):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, actor, {'platform_admin'})
        now = time.time()
        changed = db.execute('''UPDATE retrieval_check_settings SET enabled=?,interval_hours=?,next_due=?,
            revision=revision+1,updated_by=?,updated_at=? WHERE id=1 AND revision=?''',
            (int(body.enabled), body.interval_hours, now + body.interval_hours * 3600 if body.enabled else None,
             actor['id'], now, body.revision)).rowcount
        if changed != 1:
            raise HTTPException(409, '定时检测设置已变化，请刷新后再保存。')
        store._log(db, actor, 'retrieval_check_schedule_updated', 'retrieval_check', 'schedule',
                   json.dumps({'enabled': body.enabled, 'interval_hours': body.interval_hours}))
    return {'saved': True}


def status(store, actor):
    with store.connect() as db:
        current_actor(db, actor, {'platform_admin'})
        config = dict(db.execute('SELECT * FROM retrieval_check_settings WHERE id=1').fetchone())
        rows = db.execute('SELECT * FROM retrieval_check_runs ORDER BY created_at DESC,rowid DESC LIMIT 20').fetchall()
    config['enabled'] = bool(config['enabled'])
    config['next_due'] = stamp(config['next_due'])
    config['updated_at'] = stamp(config['updated_at'])
    model, _ = semantic_search.available()
    from webapp.knowledge_evidence import acceptance_gate
    version, gold = current_versions()
    return {'schedule': config, 'gate': current_gate(store) or acceptance_gate(),
            'current_version': version, 'cases_version': gold,
            'model': model.manifest['model_id'] if model else None,
            'model_fingerprint': model.fingerprint if model else None,
            'history': [run_view(row) for row in rows],
            'active': next((run_view(row) for row in rows if row['status'] in {'queued', 'running'}), None)}


def run_suite(stop_event):
    """Bounded child process: isolated fixtures, real local retrieval, no external LLM."""
    with tempfile.TemporaryDirectory(prefix='taxpearls-retrieval-check-') as directory:
        target = Path(directory) / 'result.json'
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        process = subprocess.Popen([sys.executable, '-m', 'webapp.retrieval_checks', '--evaluate', str(target)],
                                   cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        deadline = time.monotonic() + TIMEOUT
        try:
            while process.poll() is None:
                if stop_event.wait(.2):
                    raise RuntimeError('服务已停止，本次检测已中断。')
                if time.monotonic() >= deadline:
                    raise RuntimeError('检测超过五分钟，已停止，请检查本地模型运行环境。')
            if not target.is_file():
                raise RuntimeError('检测进程异常结束，请检查本地检索运行环境。')
            result = json.loads(target.read_text(encoding='utf-8'))
            if result.get('error'):
                raise RuntimeError(result['error'])
            return result
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class Worker:
    def __init__(self, get_store, runner=run_suite):
        self.get_store, self.runner = get_store, runner
        self.stop_event = Event()
        self.thread = Thread(target=self.loop, name='retrieval-checks', daemon=True)
        self.owner = uuid.uuid4().hex

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)

    def tick(self):
        store, now = self.get_store(), time.time()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _recover(db, now)
            if db.execute("SELECT 1 FROM retrieval_check_runs WHERE status='running'").fetchone():
                return
            job = db.execute("SELECT * FROM retrieval_check_runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            config = db.execute('SELECT * FROM retrieval_check_settings WHERE id=1').fetchone()
            if job is None and config['enabled'] and config['next_due'] is not None and config['next_due'] <= now:
                identity = uuid.uuid4().hex
                db.execute("INSERT INTO retrieval_check_runs(id,status,trigger,created_at) VALUES (?,'queued','scheduled',?)", (identity, now))
                job = db.execute('SELECT * FROM retrieval_check_runs WHERE id=?', (identity,)).fetchone()
            if job is None:
                return
            if job['requested_by']:
                actor = db.execute('SELECT role,active FROM users WHERE id=?', (job['requested_by'],)).fetchone()
                if not actor or not actor['active'] or actor['role'] != 'platform_admin':
                    db.execute("UPDATE retrieval_check_runs SET status='error',finished_at=?,error=? WHERE id=?",
                               (now, '发起人的平台管理员权限已失效，未执行检测。', job['id']))
                    return
            version, gold = current_versions()
            if not version or not gold:
                db.execute("UPDATE retrieval_check_runs SET status='error',finished_at=?,error=? WHERE id=?",
                           (now, '检测代码或固定测试案例缺失、损坏，请核对部署文件。', job['id']))
                if config['enabled']:
                    db.execute('UPDATE retrieval_check_settings SET next_due=? WHERE id=1', (now + config['interval_hours'] * 3600,))
                return
            db.execute("UPDATE retrieval_check_runs SET status='running',started_at=?,owner=?,lease_until=?,source_version=?,cases_version=? WHERE id=?",
                       (now, self.owner, now + TIMEOUT + 30, version, gold, job['id']))
            if config['enabled']:
                db.execute('UPDATE retrieval_check_settings SET next_due=? WHERE id=1', (now + config['interval_hours'] * 3600,))
        report, error, result = None, None, 'error'
        try:
            report = self.runner(self.stop_event)
            if (implementation_fingerprint() != version or cases_version() != gold
                    or report.get('implementation_fingerprint') != version or report.get('gold_cases_hash') != gold):
                raise RuntimeError('检测期间代码或测试案例已变化，本次结果不能认可新版本，请重新检测。')
            result = 'passed' if report_gate(report) else 'failed'
            if result == 'failed':
                error = '检测未通过，请展开查看未通过的案例和指标。'
        except Exception as exc:
            error = str(exc) if isinstance(exc, RuntimeError) else '检测异常，请检查本地模型和检测运行环境。'
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if job['requested_by']:
                actor = db.execute('SELECT role,active FROM users WHERE id=?', (job['requested_by'],)).fetchone()
                if not actor or not actor['active'] or actor['role'] != 'platform_admin':
                    result, report, error = 'error', None, '发起人的平台管理员权限已失效，本次结果未启用。'
            db.execute('''UPDATE retrieval_check_runs SET status=?,finished_at=?,report_json=?,error=?,lease_until=NULL
                          WHERE id=? AND status='running' AND owner=?''',
                       (result, time.time(), json.dumps(report, ensure_ascii=False) if report else None, error, job['id'], self.owner))

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Retrieval check worker iteration failed; details withheld')
            self.stop_event.wait(2)


def register(app, get_store, get_user, allow):
    global _store_getter
    _store_getter = get_store

    @app.get('/api/platform/retrieval-checks')
    def get_status(session: str | None = Cookie(default=None, alias='taxpearls_session')):
        actor = get_user(session)
        allow(actor, 'platform_admin')
        return status(get_store(), actor)

    @app.post('/api/platform/retrieval-checks', status_code=202)
    def start_check(session: str | None = Cookie(default=None, alias='taxpearls_session')):
        actor = get_user(session)
        allow(actor, 'platform_admin')
        return enqueue(get_store(), actor)

    @app.put('/api/platform/retrieval-checks/schedule')
    def put_schedule(body: Schedule, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        actor = get_user(session)
        allow(actor, 'platform_admin')
        return save_schedule(get_store(), actor, body)

    @app.get('/api/platform/retrieval-checks/{run_id}')
    def get_run(run_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        actor, store = get_user(session), get_store()
        allow(actor, 'platform_admin')
        with store.connect() as db:
            current_actor(db, actor, {'platform_admin'})
            row = db.execute('SELECT * FROM retrieval_check_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '检测记录不存在。')
        return run_view(row, details=True)

    @app.get('/retrieval-checks.js')
    def script():
        return FileResponse(ROOT / 'webapp/static/retrieval-checks.js', media_type='application/javascript')


def evaluate_child(target):
    try:
        model, _ = semantic_search.available()
        if model is None:
            raise RuntimeError('本地语义检索模型未就绪，请安装或核对模型配置。')
        cases = json.loads(CASES.read_text(encoding='utf-8-sig'))['cases']
        with tempfile.TemporaryDirectory(prefix='taxpearls-retrieval-corpus-') as directory:
            with sqlite3.connect(':memory:') as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                fixtures.seed(db, Path(directory) / 'originals')
                report, _ = evaluate.evaluate(db, cases, offline=True)
    except Exception as exc:
        report = {'error': str(exc) if isinstance(exc, RuntimeError) else '本地检索测试执行失败，请检查模型依赖和测试文件。'}
    Path(target).write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--evaluate':
        raise SystemExit('Use --evaluate <result-file>')
    evaluate_child(sys.argv[2])
