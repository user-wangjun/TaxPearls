"""Fail-closed installation binding; separate containers are not physical hosts."""
import base64
import binascii
from dataclasses import dataclass
import hmac
import json
import os
import re
from urllib.parse import urlsplit

from webapp.sensitive_storage import SensitiveStorageError


@dataclass(frozen=True)
class Deployment:
    mode: str
    instance: str


def configured():
    mode = os.getenv('TAXPEARLS_ENVIRONMENT', 'local').strip()
    instance = os.getenv('TAXPEARLS_INSTANCE_ID', 'local' if mode == 'local' else '').strip()
    if mode not in {'local', 'training', 'production'} or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}', instance):
        raise SensitiveStorageError('须配置有效环境类型和独立 TAXPEARLS_INSTANCE_ID。')
    if mode == 'production':
        try:
            origin = urlsplit(os.getenv('TAXPEARLS_PUBLIC_BASE_URL', ''))
            if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                    or origin.path not in ('', '/') or origin.query or origin.fragment
                    or origin.port == 0 or os.getenv('TAXPEARLS_COOKIE_SECURE') != '1'
                    or '*' in os.getenv('TAXPEARLS_TRUSTED_PROXY_IPS', '')):
                raise ValueError()
            keys = [base64.b64decode(os.getenv(name, ''), validate=True) for name in
                    ('TAXPEARLS_FIELD_KEY', 'TAXPEARLS_MATERIAL_KEY', 'TAXPEARLS_BACKUP_KEY')]
            if any(len(key) != 32 for key in keys) or len(set(keys)) != 3:
                raise ValueError()
        except (ValueError, binascii.Error):
            raise SensitiveStorageError('生产须使用可信 HTTPS 基址、Secure Cookie、明确代理及三个独立存储密钥。') from None
    return Deployment(mode, instance)


def _signature(codec, identity):
    payload = json.dumps(['TaxPearls/deployment/v1', identity.mode, identity.instance], separators=(',', ':')).encode()
    return hmac.digest(codec.key, payload, 'sha256').hex()


def _existing(db, codec):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployment_identity'").fetchone():
        return None
    row = db.execute('SELECT * FROM deployment_identity WHERE id=1').fetchone()
    if row is None:
        raise SensitiveStorageError('部署身份记录缺失，拒绝自动重新标记。')
    identity = Deployment(row['mode'], row['instance'])
    if row['contract'] != 1 or not hmac.compare_digest(row['signature'], _signature(codec, identity)):
        raise SensitiveStorageError('部署身份认证失败。')
    return identity


def _has_business_data(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return any(db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone() for table in
               ('users', 'clients', 'audits', 'material_batches', 'assignments', 'org_reports') if table in names)


def verify(db, identity, codec, *, explicit_bind=False, require_bound=False):
    previous = _existing(db, codec)
    if previous is None and require_bound:
        raise SensitiveStorageError('运行中的部署身份缺失，拒绝读取。')
    if previous is not None:
        if previous != identity and not (explicit_bind and previous.mode == 'local'):
            raise SensitiveStorageError('数据库属于另一部署环境或实例，未读取或迁移业务数据。')
    elif identity.mode != 'local' and _has_business_data(db) and not explicit_bind:
        raise SensitiveStorageError('旧业务库未绑定部署身份；请停服后显式 bind-environment，不能自动认领。')


def install(db, identity, codec, *, explicit_bind=False):
    verify(db, identity, codec, explicit_bind=explicit_bind)
    previous = _existing(db, codec)
    if previous == identity:
        return
    db.execute('''CREATE TABLE IF NOT EXISTS deployment_identity
                  (id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT NOT NULL,instance TEXT NOT NULL,
                   contract INTEGER NOT NULL,signature TEXT NOT NULL)''')
    db.execute('INSERT OR REPLACE INTO deployment_identity VALUES (1,?,?,1,?)',
               (identity.mode, identity.instance, _signature(codec, identity)))
