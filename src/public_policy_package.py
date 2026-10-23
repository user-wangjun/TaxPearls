"""Versioned public graph delivery: insert missing nodes, preserve local reviewed content."""
import gzip
import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from src.policy_paths import database_directory, resolve_path, source_identity, check_text_location
from webapp import policy_graph as pg

FORMAT = 'taxpearls-public-policy-v1'
MAX_BYTES = 64 * 1024 * 1024


def bootstrap(store, rules_dir, *, repository=None):
    """Install bundled public knowledge before serving requests, once per content version.

    School accounts are not needed. Existing teaching publications and source
    reviews remain local; a changed bundle never publishes a new teaching version.
    """
    from src.rule_conversion import effective_rules
    from webapp.knowledge_sync import teaching

    repository = Path(repository or Path(__file__).resolve().parents[1])
    package = repository / 'data/public-policy/snapshot.json.gz'
    manifest = json.loads(package.with_name('manifest.json').read_text(encoding='utf-8'))
    if digest(package.read_bytes()) != manifest['compressed_sha256']:
        raise ValueError('公共图谱数据包哈希不一致，自动初始化已停止。')
    package_hash = digest(pg.canonical(manifest).encode())
    # Startup runs before workers and requests. The lock also protects repeated
    # startup attempts in this process; SQLite rechecks the marker after BEGIN.
    with store._lock:
        rules = effective_rules(store, rules_dir)
        rules_hash = digest(pg.canonical([asdict(r) for r in sorted(rules, key=lambda r: r.id)]).encode())

        def installed(db):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_knowledge_installation'").fetchone():
                return False
            return bool(db.execute('SELECT 1 FROM public_knowledge_installation WHERE package_hash=? AND rules_hash=?',
                                   (package_hash, rules_hash)).fetchone())

        with store.connect() as db:
            if installed(db):
                return {'status': 'current', 'content_sha256': manifest['content_sha256']}
        payload = load_package(package, repository)
        with store.connect() as db:
            # Backup via a read connection before acquiring a write transaction.
            backup = store.path.with_name(store.path.name + '.before-public-' +
                                          datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite')
            target = sqlite3.connect(backup)
            try:
                db.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('公共图谱自动导入前备份完整性检查失败。')
            finally:
                target.close()
            db.execute('BEGIN IMMEDIATE')
            if installed(db):
                return {'status': 'current', 'content_sha256': manifest['content_sha256']}
            imported = import_package(db, payload, repository)
            pg.sync_rules(db, rules)
            education = teaching(db, rules)
            db.execute('''CREATE TABLE IF NOT EXISTS public_knowledge_installation (
                package_hash TEXT NOT NULL, rules_hash TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, installed_at TEXT NOT NULL,
                PRIMARY KEY(package_hash, rules_hash))''')
            # This marker describes the current projection, not every historical
            # installation; returning to an older ruleset must rebuild it too.
            db.execute('DELETE FROM public_knowledge_installation')
            db.execute('INSERT INTO public_knowledge_installation VALUES (?,?,?,?)',
                       (package_hash, rules_hash, manifest['content_sha256'], datetime.now(timezone.utc).isoformat()))
        logging.getLogger(__name__).info('公共政策图谱自动初始化完成：%s；教学知识点 %s',
                                        manifest['content_sha256'], education['knowledge_points'])
        return {'status': 'installed', 'content_sha256': manifest['content_sha256'],
                'imported': imported, 'teaching': education, 'backup': str(backup)}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    if (not isinstance(value, str) or not value or Path(value).is_absolute() or
            PureWindowsPath(value).drive or '\\' in value or '..' in value.split('/')):
        raise ValueError('公共数据包须使用不越界的相对路径。')
    return value


def load_package(package, repository):
    package, repository = Path(package).resolve(), Path(repository).resolve()
    manifest = json.loads(package.with_name('manifest.json').read_text(encoding='utf-8'))
    compressed = package.read_bytes()
    if package.name != manifest['snapshot'] or digest(compressed) != manifest['compressed_sha256']:
        raise ValueError('公共图谱数据包哈希不一致。')
    with gzip.open(package, 'rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or digest(raw) != manifest['content_sha256']:
        raise ValueError('公共图谱内容超过限额或哈希不一致。')
    payload = json.loads(raw)
    if (payload.get('format') != FORMAT or payload.get('schema_version') != 1 or
            manifest.get('format') != FORMAT or manifest.get('schema_version') != 1 or
            set(payload.get('tables', {})) != set(pg.IDS)):
        raise ValueError('公共数据包只接受八类政策图谱表。')
    for table, key in pg.IDS.items():
        rows = payload['tables'][table]
        if (not isinstance(rows, list) or len(rows) != manifest['table_counts'].get(table) or
                len({r[key] for r in rows}) != len(rows)):
            raise ValueError('公共图谱数量或节点 ID 不一致。')
    files = payload['originals']
    if len(files) != manifest['original_file_count'] or len({f['path'] for f in files}) != len(files):
        raise ValueError('公共原文清单不一致。')
    for item in files:
        name = relative(item['path'])
        if not name.startswith('policy-originals/'):
            raise ValueError('原文目标须在 policy-originals 目录内。')
        data = resolve_path(relative(item['repository_path']), repository).read_bytes()
        if digest(data) != item['sha256']:
            raise ValueError('仓库原文哈希不一致。')
    return payload


def _same_row(table, existing, incoming):
    # Human source review belongs to the local installation and is never reset by a package.
    ignored = {'created_at', 'updated_at'} | {
        'Policy': {'raw_file_path', 'issuer', 'validity_status', 'effective_from', 'effective_to'},
        'Clause': {'extraction_status'},
        'TaxRule': {'review_status', 'reviewed_by', 'reviewed_at'},
        'Relationship': {'review_status'},
    }.get(table, set())
    for key, value in incoming.items():
        if key in ignored:
            continue
        old = existing[key]
        if table == 'Clause' and key == 'source_location':
            old, value = source_identity(old), source_identity(value)
        if old != value:
            return False
    return True


def import_package(db, payload, repository):
    """Call after schema initialization; caller owns the surrounding transaction."""
    root = database_directory(db)
    if root is None:
        raise ValueError('公共数据包导入需要磁盘数据库。')
    if payload.get('format') != FORMAT or set(payload.get('tables', {})) != set(pg.IDS):
        raise ValueError('公共数据包只接受八类政策图谱表。')
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    # Version/parent IDs are immutable, but their lexical order is not dependency order.
    db.execute('PRAGMA defer_foreign_keys=ON')
    files, pending, kept, documents = {}, {}, {}, {}
    for item in payload['originals']:
        name = relative(item['path'])
        if not name.startswith('policy-originals/') or name in files:
            raise ValueError('公共原文目标无效或重复。')
        raw = resolve_path(relative(item['repository_path']), repository).read_bytes()
        if digest(raw) != item['sha256']:
            raise ValueError('仓库原文哈希不一致。')
        target = resolve_path(name, root)
        if target.exists() and target.read_bytes() != raw:
            raise ValueError('已有留存原文内容冲突，未覆盖。')
        files[name] = raw
    for table, key in pg.IDS.items():
        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        pending[table], kept[table] = [], 0
        for row in payload['tables'][table]:
            if not set(row) <= columns:
                raise ValueError('数据包与当前数据库字段不兼容。')
            if table == 'Policy':
                raw = files[relative(row['raw_file_path'])]
                if digest(raw) != row['file_sha256']:
                    raise ValueError('政策原文与记录哈希不一致。')
            if table == 'Clause':
                location = json.loads(row['source_location'])
                for kind in ('raw', 'text'):
                    raw = files[relative(location['retained_' + kind + '_path'])]
                    if digest(raw) != location[kind + '_sha256']:
                        raise ValueError('条款留存文件哈希不一致。')
                    if kind == 'text':
                        check_text_location(location, raw, row['content'], documents=documents)
            existing = db.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (row[key],)).fetchone()
            if existing:
                if not _same_row(table, existing, row):
                    raise ValueError(f'已有 {table} 节点内容冲突，未覆盖：{row[key]}')
                if table == 'Policy':
                    retained = resolve_path(existing['raw_file_path'], root).read_bytes()
                    if digest(retained) != row['file_sha256']:
                        raise ValueError('已有政策原件缺失或哈希冲突，未覆盖。')
                kept[table] += 1
            else:
                pending[table].append(row)
    created = []
    db.execute('SAVEPOINT public_policy_package')
    try:
        for name, raw in files.items():
            target = resolve_path(name, root)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    created.append(target)
                    stream.write(raw)
        for table in pg.IDS:
            rows = pending[table]
            if rows:
                columns = list(rows[0])
                if any(set(row) != set(columns) for row in rows):
                    raise ValueError('同表节点字段不一致。')
                names = ','.join('"' + c + '"' for c in columns)
                marks = ','.join('?' for _ in columns)
                db.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
                               [[row[c] for c in columns] for row in rows])
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('公共图谱外键不完整。')
        for row in payload['tables']['Relationship']:
            pg._check_relationship(db, row)
        for row in payload['tables']['TaxRule']:
            pg.validate_rule(db, row['rule_id'])
        db.execute('RELEASE public_policy_package')
    except Exception:
        db.execute('ROLLBACK TO public_policy_package')
        db.execute('RELEASE public_policy_package')
        for path in created:
            path.unlink()
        raise
    return {'inserted': {t: len(v) for t, v in pending.items()}, 'preserved': kept,
            'source_files': len(files), 'business_tables_changed': False}
