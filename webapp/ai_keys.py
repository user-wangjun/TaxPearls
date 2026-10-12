"""Encrypted administrator key registry and effective web-service credentials."""
from dataclasses import replace
import hashlib
import json
import secrets

from fastapi import HTTPException
from webapp.access import current_actor
from webapp import ai_probes
from src import ai_reasoning

STORE_PROVIDER = lambda: None
USES = {'all': '通用（当前模型）', 'extraction': '材料提取兜底', 'knowledge': '知识问答',
        'report': '风险解读与报告', 'tutor': '教学答疑', 'none': '仅检测'}
PROTOCOLS = {'auto', 'deepseek', 'openrouter', 'credit_grants'}
BUSINESS_USES = ('extraction', 'knowledge', 'report', 'tutor')


def selected_usages(value):
    if value == 'all':
        return list(BUSINESS_USES)
    if value == 'none':
        return []
    if value in BUSINESS_USES:
        return [value]
    try:
        items = json.loads(value)
        if isinstance(items, list) and all(item in BUSINESS_USES for item in items):
            return [item for item in BUSINESS_USES if item in items]
    except (ValueError, TypeError):
        pass
    return []


def encode_usages(usage, usages):
    if usages is None:
        if usage not in USES:
            raise HTTPException(422, '用途无效。')
        return usage
    if (not isinstance(usages, list) or any(item not in BUSINESS_USES for item in usages) or
            len(set(usages)) != len(usages)):
        raise HTTPException(422, '请选择不重复的有效业务用途。')
    return json.dumps([item for item in BUSINESS_USES if item in usages])


def migrate(db):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(ai_service_keys)')}
    for name, definition in [('usage', "TEXT NOT NULL DEFAULT 'all'"),
                             ('balance_protocol', "TEXT NOT NULL DEFAULT 'auto'"),
                             ('json_mode', 'INTEGER NOT NULL DEFAULT 1'),
                             ('vision', 'INTEGER NOT NULL DEFAULT 1'),
                             ('reasoning_effort', "TEXT NOT NULL DEFAULT ''"),
                             ('reasoning_protocol', "TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:
            db.execute('ALTER TABLE ai_service_keys ADD COLUMN ' + name + ' ' + definition)
    db.execute('''CREATE TABLE IF NOT EXISTS ai_model_capabilities
                  (credential_id TEXT NOT NULL,model TEXT NOT NULL,capability TEXT NOT NULL,
                   checked_at TEXT NOT NULL,PRIMARY KEY(credential_id,model))''')


def _saved_settings(settings, row):
    return replace(settings, enabled=True, base_url=row['base_url'], model=row['model'],
                   api_key=row['secret_value'], backup_api_keys=(), json_mode=bool(row['json_mode']),
                   vision=bool(row['vision']), disable_thinking=bool(row['disable_thinking']),
                   reasoning_effort=row['reasoning_effort'], reasoning_protocol=row['reasoning_protocol'])


def credential_id(settings):
    return hashlib.sha256((settings.base_url.rstrip('/') + '\0' + settings.api_key).encode()).hexdigest()


def capability(db, settings):
    row = db.execute('SELECT capability,checked_at FROM ai_model_capabilities WHERE credential_id=? AND model=?',
                     (credential_id(settings), settings.effective_model)).fetchone()
    return {**json.loads(row['capability']), 'checked_at': row['checked_at']} if row else ai_probes.reasoning_capability({})


def save_capabilities(db, settings, data):
    cid = credential_id(settings)
    db.execute('DELETE FROM ai_model_capabilities WHERE credential_id=?', (cid,))
    db.executemany('INSERT INTO ai_model_capabilities VALUES (?,?,?,?)',
                   [(cid, model, json.dumps(info), store_now()) for model, info in data.items()])


def _reasoning_settings(db, target, effort):
    chosen = target.reasoning_effort if effort is None else effort
    if chosen and not ai_reasoning.allowed(capability(db, target), chosen):
        raise HTTPException(422, '该模型未确认支持此思考程度，请从上游获取能力并选择可用档位。')
    if effort is None:
        if not _reasoning_valid(db, target):
            raise HTTPException(422, '已保存的思考配置与上游能力不符，请选择上游默认或重新获取能力。')
        return replace(target, disable_thinking=chosen == 'none') if chosen else target
    return replace(target, reasoning_effort=chosen,
                   reasoning_protocol=capability(db, target)['protocol'] if chosen != 'default' else '',
                   disable_thinking=chosen == 'none')


def _reasoning_valid(db, item):
    info = capability(db, item)
    if not item.reasoning_effort:
        return not item.disable_thinking or info.get('can_disable') is not False
    return ai_reasoning.allowed(info, item.reasoning_effort) and (
        item.reasoning_effort == 'default' or info['protocol'] == item.reasoning_protocol)


def _entries(settings, db):
    removed = {row['id'] for row in db.execute('SELECT id FROM ai_key_exclusions')}
    saved = {row['id']: dict(row) for row in db.execute('SELECT * FROM ai_service_keys ORDER BY created_at,id')}
    entries, seen = [], set()
    for index, item in enumerate(ai_probes.configured(settings)):
        cid = credential_id(item)
        if cid in removed:
            continue
        row = saved.get(cid)
        if row:
            item = _saved_settings(settings, row)
        entries.append({'settings': item, 'name': row['name'] if row else
                        ('主用密钥' if item.api_key == settings.api_key.strip() else
                         f'备用密钥 {index if settings.api_key.strip() else index + 1}'),
                        'source': 'database' if row else 'environment',
                        'revision': row['revision'] if row else ai_probes.identity(item),
                        'usage': row['usage'] if row else 'all',
                        'balance_protocol': row['balance_protocol'] if row else 'auto'})
        seen.add(cid)
    for cid, row in saved.items():
        if cid in seen or cid in removed:
            continue
        item = _saved_settings(settings, row)
        entries.append({'settings': item, 'name': row['name'], 'source': 'database', 'revision': row['revision'],
                        'usage': row['usage'], 'balance_protocol': row['balance_protocol']})
    for entry in entries:
        entry['usages'] = selected_usages(entry['usage'])
    return entries


def in_pool(item, settings):
    return item.base_url == settings.base_url and item.effective_model == settings.effective_model


def entries(settings, store):
    with store.connect() as db:
        return _entries(settings, db)


def apply(settings, store=None, *, usage='all'):
    store = store if store is not None else STORE_PROVIDER()
    if store is None:
        return settings
    with store.connect() as db:
        items = _entries(settings, db)
        valid = {ai_probes.identity(entry['settings']) for entry in items if _reasoning_valid(db, entry['settings'])}
    dedicated = [entry for entry in items if entry['usage'] != 'all' and usage in entry['usages']]
    shared = [entry for entry in items if entry['usage'] == 'all' and in_pool(entry['settings'], settings)]
    selected = (dedicated[0]['settings'] if dedicated else shared[0]['settings'] if shared else settings)
    candidates = [*dedicated, *shared]
    keys = tuple(dict.fromkeys(entry['settings'].api_key for entry in candidates
                              if same_profile(entry['settings'], selected) and ai_probes.identity(entry['settings']) in valid))
    return replace(selected, enabled=settings.enabled, api_key=keys[0] if keys else '', backup_api_keys=keys[1:])


def same_profile(left, right):
    return (left.base_url, left.effective_model, left.json_mode, left.disable_thinking, left.vision,
            left.reasoning_effort, left.reasoning_protocol) == (
        right.base_url, right.effective_model, right.json_mode, right.disable_thinking, right.vision,
        right.reasoning_effort, right.reasoning_protocol)


def catalog(settings, store, user):
    items = entries(settings, store)
    logs = store.list_ai_key_probes(user, [ai_probes.identity(entry['settings']) for entry in items])
    data = ai_probes.catalog(replace(settings, api_key='', backup_api_keys=()), [])
    data.update(usages=[{'id': key, 'name': name} for key, name in USES.items()],
                default_base_url=settings.base_url, default_model=settings.model,
                default_json_mode=settings.json_mode, default_vision=settings.vision,
                default_disable_thinking=settings.disable_thinking, default_reasoning_effort=ai_reasoning.mode(settings))
    for entry in items:
        row = ai_probes.catalog(entry['settings'], logs)['keys'][0]
        with store.connect() as db:
            info = capability(db, entry['settings'])
            if not _reasoning_valid(db, entry['settings']):
                row['problem'] = '上游未确认支持已保存的思考程度，请编辑配置。'
        row.update(name=entry['name'], source=entry['source'], revision=entry['revision'],
                   in_call_pool=bool(entry['usages']) and (entry['usage'] != 'all' or
                   in_pool(entry['settings'], settings)),
                   usage=entry['usage'] if entry['usage'] in USES else 'multiple' if entry['usages'] else 'none',
                   usages=entry['usages'], balance_protocol=entry['balance_protocol'],
                   json_mode=entry['settings'].json_mode, vision=entry['settings'].vision,
                   disable_thinking=entry['settings'].disable_thinking,
                   reasoning_effort=ai_reasoning.mode(entry['settings']), reasoning_capability=info,
                   reasoning_legacy=not entry['settings'].reasoning_effort)
        data['keys'].append(row)
    return data


def add(store, user, settings, *, name, key, provider, model, base_url='', usage=None,
        balance_protocol='auto', json_mode=None, vision=None, disable_thinking=None, reasoning_effort=None, usages=None):
    target = ai_probes.temporary(settings, provider, key.strip(), model.strip())
    if base_url:
        target = replace(target, base_url=base_url.strip().rstrip('/'))
    target = replace(target, json_mode=target.json_mode if json_mode is None else json_mode,
                     vision=target.vision if vision is None else vision,
                     disable_thinking=target.disable_thinking if disable_thinking is None else disable_thinking)
    usage = usage if usage is not None else 'all' if in_pool(target, settings) else 'none'
    usage = encode_usages(usage, usages)
    if balance_protocol not in PROTOCOLS:
        raise HTTPException(422, '用途或余额协议无效。')
    if not name.strip() or target.problem():
        raise HTTPException(422, '请填写名称、有效密钥和模型名称，并核对服务端接口配置。')
    cid, revision = credential_id(target), secrets.token_hex(16)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, user, {'platform_admin'})
        target = _reasoning_settings(db, target, reasoning_effort)
        existing = _entries(settings, db)
        if any(credential_id(entry['settings']) == cid for entry in existing):
            raise HTTPException(409, '此接口的密钥已经存在，请勿重复加入。')
        if len(existing) >= 30:
            raise HTTPException(409, '密钥列表最多保留 30 把，请先移除不再使用的密钥。')
        if selected_usages(usage) and sum(bool(entry['usages']) and same_profile(entry['settings'], target) for entry in existing) >= 10:
            raise HTTPException(409, '同一业务接口和模型的主备密钥合计最多 10 把。')
        db.execute('DELETE FROM ai_key_exclusions WHERE id=?', (cid,))
        db.execute('''INSERT INTO ai_service_keys(id,name,base_url,model,secret_value,disable_thinking,revision,created_at,
                      usage,balance_protocol,json_mode,vision,reasoning_effort,reasoning_protocol) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (cid, name.strip(), target.base_url, target.model,
                    db.field_codec.seal(target.api_key, 'ai_service_keys', 'secret_value', cid),
                    int(target.disable_thinking), revision, store_now(), usage, balance_protocol,
                    int(target.json_mode), int(target.vision), target.reasoning_effort, target.reasoning_protocol))
        store._log(db, user, 'ai_key_added', 'ai_key', ai_probes.identity(target),
                   json.dumps({'in_call_pool': in_pool(target, settings)}))
    return {'id': ai_probes.identity(target)}


def _find(db, user, settings, key_id, revision):
    current_actor(db, user, {'platform_admin'})
    entry = next((entry for entry in _entries(settings, db)
                  if ai_probes.identity(entry['settings']) == key_id), None)
    if entry is None:
        raise HTTPException(404, '密钥已移除或配置已变更，请刷新列表。')
    if entry['revision'] != revision:
        raise HTTPException(409, '密钥已被修改，请刷新后再操作。')
    return entry


def update(store, user, settings, key_id, revision, *, name=None, key='', base_url=None, model=None,
           usage=None, balance_protocol=None, json_mode=None, vision=None, disable_thinking=None, reasoning_effort=None, usages=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        entry = _find(db, user, settings, key_id, revision)
        old = entry['settings']
        target = replace(old, api_key=key.strip() or old.api_key,
                         base_url=old.base_url if base_url is None else base_url.strip().rstrip('/'),
                         model=old.model if model is None else model.strip(),
                         json_mode=old.json_mode if json_mode is None else json_mode,
                         vision=old.vision if vision is None else vision,
                         disable_thinking=old.disable_thinking if disable_thinking is None else disable_thinking)
        name = entry['name'] if name is None else name.strip()
        usage = entry['usage'] if usage is None else usage
        if usages is not None or usage != entry['usage']:
            usage = encode_usages(usage, usages)
        balance_protocol = entry['balance_protocol'] if balance_protocol is None else balance_protocol
        target = _reasoning_settings(db, target, reasoning_effort)
        if not name or target.problem() or balance_protocol not in PROTOCOLS:
            raise HTTPException(422, '请核对名称、接口地址、模型、密钥和用途。')
        oldcid, cid = credential_id(old), credential_id(target)
        others = [item for item in _entries(settings, db) if credential_id(item['settings']) != oldcid]
        if any(credential_id(item['settings']) == cid for item in others):
            raise HTTPException(409, '此接口的密钥已存在，不能覆盖另一条记录。')
        if selected_usages(usage) and sum(bool(item['usages']) and same_profile(item['settings'], target) for item in others) >= 10:
            raise HTTPException(409, '同一服务配置的主备密钥最多 10 把。')
        if oldcid != cid:
            db.execute('INSERT OR IGNORE INTO ai_key_exclusions(id,created_at) VALUES (?,?)', (oldcid, store_now()))
        db.execute('DELETE FROM ai_service_keys WHERE id=?', (oldcid,))
        db.execute('DELETE FROM ai_key_exclusions WHERE id=?', (cid,))
        newrevision = secrets.token_hex(16)
        db.execute('''INSERT INTO ai_service_keys(id,name,base_url,model,secret_value,disable_thinking,revision,created_at,
                      usage,balance_protocol,json_mode,vision,reasoning_effort,reasoning_protocol) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (cid, name, target.base_url, target.model,
                    db.field_codec.seal(target.api_key, 'ai_service_keys', 'secret_value', cid),
                    int(target.disable_thinking), newrevision, store_now(), usage, balance_protocol,
                    int(target.json_mode), int(target.vision), target.reasoning_effort, target.reasoning_protocol))
        store._log(db, user, 'ai_key_updated', 'ai_key', ai_probes.identity(target),
                   json.dumps({'usage': usage, 'credential_changed': cid != oldcid}))
    return {'id': ai_probes.identity(target), 'revision': newrevision}


def reveal(store, user, settings, key_id, revision):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        entry = _find(db, user, settings, key_id, revision)
        store._log(db, user, 'ai_key_viewed', 'ai_key', key_id)
        return {'api_key': entry['settings'].api_key}


def remove(store, user, settings, key_id, revision):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, user, {'platform_admin'})
        target = next((entry for entry in _entries(settings, db)
                       if ai_probes.identity(entry['settings']) == key_id), None)
        if target is None:
            raise HTTPException(404, '密钥已移除或配置已变更，请刷新列表。')
        if target['revision'] != revision:
            raise HTTPException(409, '密钥已重新加入，请刷新后再决定是否删除。')
        cid = credential_id(target['settings'])
        # Exclusion also prevents an environment credential from reappearing on restart.
        db.execute('INSERT OR IGNORE INTO ai_key_exclusions(id,created_at) VALUES (?,?)', (cid, store_now()))
        db.execute('DELETE FROM ai_service_keys WHERE id=?', (cid,))
        db.execute('DELETE FROM ai_model_capabilities WHERE credential_id=?', (cid,))
        store._log(db, user, 'ai_key_removed', 'ai_key', key_id)
    return {'removed': True}


def store_now():
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec='microseconds')
