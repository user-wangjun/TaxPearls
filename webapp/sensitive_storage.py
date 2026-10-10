"""Authenticated encryption for columns carrying personal/financial evidence.

Structured IDs, dates, counts and lookup columns remain queryable. Full JSON,
free-text evidence and archived report bytes are encrypted before SQL binding.
The independent key never lives in SQLite, a backup, or a command argument.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import sqlite3

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = 'tp-field-v1:'
REGISTRY = {
    'clients': (('id',), ('taxpayer_id',)),
    'audits': (('id',), ('taxpayer_id', 'dataset_json', 'findings_json')),
    'audit_report_versions': (('audit_id', 'version'), ('html', 'manifest_json', 'pdf_bytes')),
    'org_reports': (('id',), ('snapshot_json', 'html', 'pdf_bytes')),
    'generated_exercises': (('audit_id',), ('metadata_json',)),
    'finding_interpretations': (('audit_id', 'rule_id', 'evidence_hash'), ('result_json',)),
    'audit_narratives': (('audit_id', 'evidence_hash'), ('result_json',)),
    'audit_log': (('id',), ('detail',)),
    'submissions': (('assignment_id', 'student_id'), ('details_json', 'feedback')),
    'training_mistake_cases': (('id',), ('errors_json',)),
    'training_practice_attempts': (('id',), ('result_json',)),
    'training_self_practice_attempts': (('id',), ('result_json',)),
}
PROTECTED_COLUMNS = frozenset(c for _, columns in REGISTRY.values() for c in columns)


class SensitiveStorageError(RuntimeError):
    """Safe diagnostic: never echo a key, plaintext, or provider exception."""


class Connection(sqlite3.Connection):
    """Carries the pinned codec for trusted transaction helpers."""


def configured_key():
    try:
        raw = base64.b64decode(os.getenv('TAXPEARLS_FIELD_KEY', ''), validate=True)
    except (ValueError, binascii.Error):
        raw = b''
    if len(raw) != 32:
        raise SensitiveStorageError('敏感字段存储须配置独立 TAXPEARLS_FIELD_KEY（32 字节随机密钥的 Base64）。')
    for name in ('TAXPEARLS_MATERIAL_KEY', 'TAXPEARLS_BACKUP_KEY'):
        try:
            other = base64.b64decode(os.getenv(name, ''), validate=True)
        except (ValueError, binascii.Error):
            other = b''
        if other == raw:
            raise SensitiveStorageError('敏感字段密钥须与材料及备份密钥分别配置。')
    return raw


class Codec:
    def __init__(self, key):
        self.key = key
        self.key_id = hashlib.sha256(key).hexdigest()[:24]

    @staticmethod
    def context(table, column, identity):
        if table not in REGISTRY or column not in REGISTRY[table][1] or len(identity) != len(REGISTRY[table][0]):
            raise SensitiveStorageError('敏感字段上下文无效。')
        return [table, column, *map(str, identity)]

    def seal(self, value, table, column, *identity):
        if value is None:
            return None
        context = self.context(table, column, identity)
        binary = isinstance(value, bytes)
        raw = value if binary else value.encode('utf-8')
        header = json.dumps([self.key_id, context, binary], separators=(',', ':')).encode()
        nonce = secrets.token_bytes(12)
        envelope = PREFIX + base64.b64encode(header).decode() + ':' + base64.b64encode(
            nonce + AESGCM(self.key).encrypt(nonce, raw, header)).decode()
        return envelope.encode() if binary else envelope

    def taxpayer_lookup(self, org_id, value):
        # Domain separated, tenant-scoped equality index; not an unkeyed hash
        # that would allow enumeration of identity/telephone numbers.
        data = json.dumps(['taxpayer-lookup-v1', str(org_id), value], separators=(',', ':')).encode()
        return hmac.digest(self.key, data, 'sha256').hex()

    def open(self, value, table=None, column=None, identity=None):
        if value is None:
            return None
        try:
            envelope = value.decode('ascii') if isinstance(value, bytes) else value
            if not isinstance(envelope, str) or not envelope.startswith(PREFIX):
                raise ValueError()
            encoded_header, encoded_cipher = envelope[len(PREFIX):].split(':')
            header = base64.b64decode(encoded_header, validate=True)
            key_id, context, binary = json.loads(header)
            if (type(binary) is not bool or binary != isinstance(value, bytes)
                    or not isinstance(context, list) or key_id != self.key_id
                    or self.context(context[0], context[1], context[2:]) != context):
                raise ValueError()
            if table is not None and context != self.context(table, column, identity):
                raise ValueError()
            cipher = base64.b64decode(encoded_cipher, validate=True)
            raw = AESGCM(self.key).decrypt(cipher[:12], cipher[12:], header)
            return (raw if binary else raw.decode('utf-8')), context
        except (InvalidTag, ValueError, TypeError, KeyError, IndexError, UnicodeError, binascii.Error):
            raise SensitiveStorageError('敏感字段完整性校验失败或密钥不匹配，请核查备份。') from None

    def row(self, cursor, values):
        names = [c[0] for c in cursor.description]
        result = list(values)
        for index, value in enumerate(values):
            if names[index] not in PROTECTED_COLUMNS or value is None:
                continue
            marker = PREFIX.encode() if isinstance(value, bytes) else PREFIX
            if not isinstance(value, (str, bytes)) or not value.startswith(marker):
                raise SensitiveStorageError('已加密存储出现明文或损坏字段，拒绝读取。')
            plain, context = self.open(value)
            table, column, *identity = context
            if names[index] != column:
                raise SensitiveStorageError('敏感字段查询须保留原列名。')
            for key, expected in zip(REGISTRY[table][0], identity):
                # Joined teaching queries expose an audit as audit_id, while id
                # belongs to their assignment. Standalone audit reads expose id.
                name = 'audit_id' if table == 'audits' and key == 'id' and 'audit_id' in names else key
                if name not in names or str(values[names.index(name)]) != expected:
                    raise SensitiveStorageError('敏感字段查询或对象关联不一致。')
            result[index] = plain
        return sqlite3.Row(cursor, tuple(result))


def attach(db, codec):
    db.field_codec = codec
    db.row_factory = codec.row
    db.create_function('tp_field_open', 4, lambda value, table, column, identity:
                       codec.open(value, table, column, json.loads(identity))[0])
    def valid(value, table, column, identity):
        if value is not None:
            codec.open(value, table, column, json.loads(identity))
        return 1
    db.create_function('tp_field_valid', 4, valid)
    db.create_function('tp_taxpayer_value', 3, lambda value, table, identity:
                       codec.open(value, table, 'taxpayer_id', [identity])[0])
    db.create_function('tp_taxpayer_index_valid', 4, lambda value, table, identity, org:
                       codec.taxpayer_lookup(org, codec.open(value, table, 'taxpayer_id', [identity])[0]))


def verify_state(db, codec):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sensitive_storage_state'").fetchone():
        row = db.execute('SELECT * FROM sensitive_storage_state WHERE id=1').fetchone()
        if not row or row['key_id'] != codec.key_id or row['contract'] != 1:
            raise SensitiveStorageError('敏感字段密钥或存储版本不匹配，未迁移数据。')


def migrate(db, codec):
    """Called after schema installation. One atomic transaction, no raw backups.

    Restart is idempotent. All envelopes are authenticated before a migration
    succeeds; a foreign key, corrupt envelope or wrong key rolls back the unit.
    VACUUM/checkpoint are explicit offline operations, not startup side effects.
    """
    db.execute('BEGIN IMMEDIATE')
    db.execute('CREATE TABLE IF NOT EXISTS sensitive_storage_state (id INTEGER PRIMARY KEY CHECK(id=1), key_id TEXT NOT NULL, contract INTEGER NOT NULL)')
    state = db.execute('SELECT * FROM sensitive_storage_state WHERE id=1').fetchone()
    if state and (state['key_id'] != codec.key_id or state['contract'] != 1):
        raise SensitiveStorageError('敏感字段密钥或存储版本不匹配，未迁移数据。')
    for table in ('clients', 'audits'):
        if 'taxpayer_lookup' not in {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}:
            db.execute('ALTER TABLE ' + table + ' ADD COLUMN taxpayer_lookup TEXT')
    for table, (keys, columns) in REGISTRY.items():
        if state:
            continue
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        selected = (*keys, *columns, 'org_id') if table in ('clients', 'audits') else (*keys, *columns)
        rows = cursor.execute('SELECT ' + ','.join(selected) + ' FROM ' + table)
        for row in rows:
            identity = [row[k] for k in keys]
            changes = {}
            for column in columns:
                value = row[column]
                if value is None:
                    continue
                marker = PREFIX.encode() if isinstance(value, bytes) else PREFIX
                if value.startswith(marker):
                    plain = codec.open(value, table, column, identity)[0]
                else:
                    plain = value
                    changes[column] = codec.seal(value, table, column, *identity)
                if column == 'taxpayer_id':
                    changes['taxpayer_lookup'] = codec.taxpayer_lookup(row['org_id'], plain)
            if changes:
                db.execute('UPDATE ' + table + ' SET ' + ','.join(c + '=?' for c in changes)
                           + ' WHERE ' + ' AND '.join(k + '=?' for k in keys), [*changes.values(), *identity])
    if not state:
        db.execute('INSERT INTO sensitive_storage_state VALUES (1,?,1)', (codec.key_id,))
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_taxpayer_lookup ON clients(org_id,taxpayer_lookup)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audits_taxpayer_lookup ON audits(org_id,taxpayer_lookup,period,audited_at DESC)')
    for table, (keys, columns) in REGISTRY.items():
        for event in ('INSERT', 'UPDATE'):
            identity = 'json_array(' + ','.join('CAST(NEW.' + key + ' AS TEXT)' for key in keys) + ')'
            checks = '\n'.join("SELECT tp_field_valid(NEW." + column + ",'" + table + "','" + column + "'," + identity + ');' for column in columns)
            if table in ('clients', 'audits'):
                checks += ("SELECT CASE WHEN NEW.taxpayer_lookup IS NULL OR NEW.taxpayer_lookup != "
                           "tp_taxpayer_index_valid(NEW.taxpayer_id,'" + table + "',NEW.id,NEW.org_id) "
                           "THEN RAISE(ABORT,'Sensitive lookup integrity failure') END;")
            name = 'tp_fields_' + table + '_' + event.lower()
            db.execute('DROP TRIGGER IF EXISTS ' + name)
            db.execute('CREATE TRIGGER ' + name
                       + ' BEFORE ' + event + ' ON ' + table + ' BEGIN ' + checks + ' END')
