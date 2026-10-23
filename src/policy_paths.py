"""Policy originals are relative to the database directory, never the working directory."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

import yaml


def database_directory(db):
    filename = next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), '')
    return Path(filename).resolve().parent if filename else None


def resolve_path(value, root=None):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError('政策原文路径为空。')
    path = Path(value)
    if path.is_absolute():
        return path.resolve()  # Existing absolute references remain readable until explicitly migrated.
    if PureWindowsPath(str(value)).drive or root is None:
        raise ValueError('相对原文路径须提供数据库目录，不能依赖当前工作目录。')
    parts = str(value).replace('\\', '/').split('/')
    if '..' in parts:
        raise ValueError('政策原文路径不能越出数据库目录。')
    root = Path(root).resolve()
    path = (root / Path(*parts)).resolve()
    if not path.is_relative_to(root):
        raise ValueError('政策原文路径不能越出数据库目录。')
    return path


def stored_path(db, value):
    path = Path(value).resolve()
    root = database_directory(db)
    if root is None:
        return str(path)  # In-memory engineering fixtures have no portable database directory.
    if not path.is_relative_to(root):
        raise ValueError('原文留存目录须位于数据库目录内，才能保存可迁移的相对路径。')
    return path.relative_to(root).as_posix()


def source_identity(value):
    """Physical retention locations are not a new edition of a hash-bound source."""
    try:
        location = json.loads(value)
    except (ValueError, TypeError):
        return value
    if not isinstance(location, dict) or not all(location.get(k) for k in
            ('raw_path', 'text_path', 'raw_sha256', 'text_sha256')):
        return value
    if not all(isinstance(location[k], str) and re.fullmatch(r'[a-f0-9]{64}', location[k])
               for k in ('raw_sha256', 'text_sha256')):
        return value
    return {k: v for k, v in location.items() if k not in {'retained_raw_path', 'retained_text_path'}}


def evidence_identity(value):
    if isinstance(value, list):
        return [evidence_identity(item) for item in value]
    if isinstance(value, dict):
        return {key: source_identity(item) if key == 'source_location' else evidence_identity(item)
                for key, item in value.items()}
    return value


def check_text_location(location, data, content, *, documents=None):
    """Keep verbatim offsets distinct from explicitly identified catalog annotations."""
    kind = location.get('provenance_kind')
    if kind in {'catalog_annotation', 'manifest_annotation', 'retained_catalog_snapshot'}:
        documents = {} if documents is None else documents
        key = hashlib.sha256(data).hexdigest()
        if key not in documents:
            try:
                documents[key] = json.loads(data)
            except ValueError:
                documents[key] = yaml.safe_load(data.decode('utf-8-sig'))
        document = documents[key]
        if not isinstance(document, dict):
            raise ValueError('来源目录结构不正确。')
        if kind == 'retained_catalog_snapshot':
            match = re.fullmatch(r'sources\[id=([^\]]+)\]\.snapshot\.text', location.get('snapshot_field', ''))
            sources = document.get('sources', [])
            source = next((s for s in sources if match and s['id'] == match[1]), None)
            actual = source['snapshot']['text'] if source else None
            if (actual != content or hashlib.sha256(re.sub(r'\s+', '', content).encode()).hexdigest()
                    != location.get('snapshot_sha256')):
                raise ValueError('目录原文快照字段或哈希不一致。')
        else:
            if hashlib.sha256(content.encode()).hexdigest() != location.get('annotation_sha256'):
                raise ValueError('目录解释内容哈希不一致。')
            if kind == 'catalog_annotation':
                match = re.fullmatch(r'policies\[id=([^\]]+)\]', location.get('catalog_field', ''))
                topic = next((p for p in document.get('policies', []) if match and p['id'] == match[1]), None)
                if topic is None or json.loads(content) != topic:
                    raise ValueError('目录解释字段不一致。')
            else:
                identifier = location.get('manifest_document_id')
                if (json.loads(content).get('document_id') != identifier or not any(
                        item['id'] == identifier for item in document.get('documents', []))):
                    raise ValueError('来源清单整理记录不一致。')
    elif 'start_char' in location and 'end_char_exclusive' in location:
        start, end = location['start_char'], location['end_char_exclusive']
        text = data.decode('utf-8-sig')
        if (not isinstance(start, int) or not isinstance(end, int) or
                not 0 <= start <= end <= len(text) or text[start:end] != content):
            raise ValueError('条款原文字符定位不一致，路径迁移已停止。')


def relativize_paths(db):
    """Validate every retained hash before atomically changing only physical paths.

    The caller owns the transaction/commit. No IDs, text, review states, timestamps,
    fingerprints or historical evidence snapshots are changed.
    """
    root = database_directory(db)
    if root is None:
        raise ValueError('路径迁移需要磁盘数据库。')
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    policies, clauses, files, documents = [], [], {}, {}

    def checked(value, expected):
        path = resolve_path(value, root)
        relative = stored_path(db, path)
        if path not in files:
            files[path] = path.read_bytes()
        if not expected or hashlib.sha256(files[path]).hexdigest() != expected:
            raise ValueError('政策原文或提取文本哈希不一致，路径迁移已停止。')
        return relative, files[path]

    for key, value, expected in db.execute('SELECT policy_id,raw_file_path,file_sha256 FROM Policy'):
        relative, _ = checked(value, expected)
        if relative != value:
            policies.append((relative, key))
    for key, value, content in db.execute('SELECT clause_id,source_location,content FROM Clause'):
        try:
            location = json.loads(value)
        except (ValueError, TypeError):
            continue  # Older human-written page/line coordinates contain no retained file paths.
        if not isinstance(location, dict):
            continue
        changed = False
        for kind in ('raw', 'text'):
            field = 'retained_' + kind + '_path'
            if field not in location:
                continue
            relative, data = checked(location[field], location.get(kind + '_sha256'))
            if kind == 'text':
                check_text_location(location, data, content, documents=documents)
            changed |= relative != location[field]
            location[field] = relative
        if changed:
            clauses.append((json.dumps(location, ensure_ascii=False, sort_keys=True, separators=(',', ':')), key))
    db.execute('SAVEPOINT policy_relative_paths')
    try:
        db.executemany('UPDATE Policy SET raw_file_path=? WHERE policy_id=?', policies)
        db.executemany('UPDATE Clause SET source_location=? WHERE clause_id=?', clauses)
        db.execute('RELEASE policy_relative_paths')
    except Exception:
        db.execute('ROLLBACK TO policy_relative_paths')
        db.execute('RELEASE policy_relative_paths')
        raise
    return {'policies_changed': len(policies), 'clauses_changed': len(clauses), 'verified_files': len(files)}
