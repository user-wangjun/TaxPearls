"""Upload scope declarations and client prefills, never policy eligibility."""
from copy import deepcopy
import json
import re

from src.input_errors import InputError
from webapp import material_batches as batches
from webapp.access import AccessDenied, current_actor

FIELDS = ('name', 'taxpayer_id', 'industry', 'region', 'taxpayer_type', 'business_scope', 'period_start', 'period_end')


def identity(value):
    return re.sub(r'\s+', '', value or '').upper()


def normalize(value):
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise InputError('企业信息含未知字段或格式无效。')
    if any(not isinstance(v, str) or len(v) > 200 for v in value.values()):
        raise InputError('企业信息须为不超过200字符的文本。')
    return {key: value.get(key, '').strip() for key in FIELDS
            if key not in {'region', 'taxpayer_type', 'business_scope'} or value.get(key, '').strip()}


def profile(store, user, client_id):
    """Read only an authorized client's last confirmed scope; never draft data."""
    with store.connect() as db:
        db.execute('BEGIN')
        current_actor(db, user, batches.ROLES)
        client = db.execute('SELECT * FROM clients WHERE id=? AND org_id=?', (client_id, user['org_id'])).fetchone()
        if not client or user['role'] == 'accountant' and client['accountant_id'] != user['id']:
            raise AccessDenied()
        template = normalize({'name': client['name'], 'taxpayer_id': client['taxpayer_id']})
        row = db.execute('''SELECT a.id,a.audited_at,a.period,b.org_id,b.id AS batch_id,r.revision,r.kind,r.payload_cipher
            FROM audits a JOIN material_executions e ON e.audit_id=a.id
            JOIN material_batches b ON b.id=e.batch_id
            JOIN material_revisions r ON r.batch_id=b.id AND r.revision=e.analysis_revision
            WHERE a.org_id=? AND a.client_id=? AND b.client_id=a.client_id
            ORDER BY a.rowid DESC LIMIT 1''', (user['org_id'], client_id)).fetchone()
        history = None
        if row:
            payload = json.loads(batches._open(row['payload_cipher'], batches._context(
                row['org_id'], row['batch_id'], 'revision:' + row['kind'], row['revision'])))
            for key in ('industry', 'region', 'taxpayer_type', 'business_scope'):
                template[key] = payload['company'].get(key, '')
            history = {'audit_id': row['id'], 'period': row['period'], 'audited_at': row['audited_at']}
        count = db.execute('SELECT COUNT(*) FROM audits WHERE org_id=? AND client_id=?', (user['org_id'], client_id)).fetchone()[0]
        return {'client_id': client_id, 'company': template, 'history': history, 'analysis_count': count}


def context(store, user, mode, client_id, company):
    if mode not in {'new', 'existing'} or (mode == 'existing') != bool(client_id):
        raise InputError('请选择新建企业，或明确选择一个已有企业档案。')
    declared = normalize(company)
    archive = profile(store, user, client_id) if client_id else None
    batches.authorize_client(store, user, client_id)
    origins = {key: 'user' for key, value in declared.items() if value}
    if archive:
        for key, value in archive['company'].items():
            if value and not declared.get(key):
                declared[key] = value
                origins[key] = 'client_archive' if key in {'name', 'taxpayer_id'} else 'confirmed_history'
    return {'mode': mode, 'client_id': client_id, 'declared': declared, 'origins': origins, 'archive': archive}


def prefill(candidate, scope):
    result = normalize(candidate)
    origins = {key: 'material_candidate' for key, value in result.items() if value}
    for key, value in scope['declared'].items():
        if value:
            result[key] = value
            origins[key] = scope['origins'].get(key, 'user')
    return result, origins


def decorate(payload, scope, origins=None):
    if not scope:
        return payload
    payload['scope_context'] = deepcopy(scope)
    if origins is not None:
        payload['scope_context']['prefill_origins'] = origins
    archive = scope.get('archive')
    if archive and identity(payload['company'].get('taxpayer_id')) != identity(archive['company']['taxpayer_id']):
        payload['analysis']['feedback']['blocking'].append({'code': 'client_identity_conflict',
            'message': '本次核对税号与所选企业档案不一致。', 'file_id': None,
            'impact': '不能将另一企业材料归入此档案。', 'required': '核对主体，选择正确企业或另开上传批次。'})
        payload['analysis']['can_confirm'] = False
    return payload
