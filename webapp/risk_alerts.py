"""Risk follow-up and rule-update reminders over frozen, authorized audits."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Literal

from fastapi import Cookie, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src import engine, loader, risk_changes, snapshots
from src.rule_conversion import canonical, period_rules
from webapp.access import AccessDenied, audit_row, current_actor

RULES_DIR = Path(__file__).resolve().parents[1] / 'rules'


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['pending', 'reviewing', 'done']
    note: str = Field(default='', max_length=2000)
    revision: int = Field(ge=0, strict=True)


def review_key(row, dataset, finding):
    # Repeated saves of identical evidence share a review. Changed evidence,
    # rules, periods, client ownership or organizations require a fresh review.
    frozen = deepcopy(finding)
    frozen['rule'].pop('source_file', None)
    return hashlib.sha256(canonical({'org': row['org_id'], 'client': row['client_id'],
        'dataset': dataset, 'finding': frozen}).encode()).hexdigest()


def reviews_for(db, row, dataset, findings):
    keys = {item['rule']['id']: review_key(row, dataset, item)
            for item in findings if item['status'] == 'hit'}
    if not keys:
        return {}
    saved = {item['review_key']: dict(item) for item in db.execute(
        'SELECT * FROM risk_reviews WHERE review_key IN (' + ','.join('?' for _ in keys) + ')', list(keys.values()))}
    return {rule_id: {'status': saved[key]['status'], 'note': saved[key]['review_note'],
                     'revision': saved[key]['revision'], 'updated_at': saved[key]['updated_at']}
            if key in saved else {'status': 'pending', 'note': '', 'revision': 0, 'updated_at': None}
            for rule_id, key in keys.items()}


def rule_context(db):
    histories = {}
    for row in db.execute('SELECT * FROM rule_version_history ORDER BY rule_id,id'):
        histories.setdefault(row['rule_id'], []).append({
            'version': row['version'], 'effective_from': row['effective_from'], 'effective_to': row['effective_to'],
            'logic': json.loads(row['logic_json']), 'threshold_basis': row['threshold_basis'],
            'rule': json.loads(row['rule_json']) if row['rule_json'] else None})
    state = list(db.execute('SELECT rule_id,enabled FROM rule_state'))
    enabled = {row['rule_id'] for row in state if row['enabled']} if state else None
    return engine.load_rules(RULES_DIR), histories, enabled


def rule_updates(dataset, findings, context):
    """An update warrants rechecking; it never establishes a new risk."""
    data = snapshots.deserialize_dataset(dataset)
    old = {item.rule.id: item for item in snapshots.deserialize_findings(findings)}
    base_rules, histories, enabled = context
    updates = []
    for base in base_rules:
        if enabled is not None and base.id not in enabled:
            continue
        previous = old.get(base.id)
        try:
            current = period_rules([base], histories, data.company.period)[0]
        except (ValueError, loader.InputError):
            # A split-period revision is a reason to review, never silently
            # replace the old version or block other dashboard information.
            if previous:
                updates.append({'id': base.id, 'name': base.name, 'before': previous.rule.version,
                                'after': None, 'reason': '规则生效期发生变化，请确认期间后重新检测。'})
            continue
        if previous and risk_changes._definition(previous.rule) == risk_changes._definition(current):
            continue
        if engine.readiness(current, data):
            # An inapplicable edition or missing facts cannot support a
            # claim that this enterprise has a newly detectable risk.
            continue
        updates.append({'id': current.id, 'name': current.name, 'before': previous.rule.version if previous else None,
                        'after': current.version, 'reason': '检测规则已更新。' if previous else '新增可执行检查。'})
    return updates


def save_review(store, actor, audit_id, rule_id, body):
    from webapp.storage import _now
    note = body.note.strip()
    if body.status == 'done' and not note:
        raise HTTPException(422, '请填写核查结论或处理说明。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor(db, actor, {'org_admin', 'accountant'})
        row = audit_row(db, audit_id, actor)
        if row is None:
            raise AccessDenied()
        dataset, findings = json.loads(row['dataset_json']), json.loads(row['findings_json'])
        finding = next((item for item in findings if item['rule']['id'] == rule_id and item['status'] == 'hit'), None)
        if finding is None:
            raise AccessDenied('该风险疑点不存在或不属于命中事项。')
        key = review_key(row, dataset, finding)
        previous = db.execute('SELECT revision FROM risk_reviews WHERE review_key=?', (key,)).fetchone()
        if (previous['revision'] if previous else 0) != body.revision:
            raise HTTPException(409, '核查记录已被更新，请刷新后重试。')
        revision, stamp = body.revision + 1, _now()
        db.execute('''INSERT INTO risk_reviews VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(review_key) DO UPDATE SET audit_id=excluded.audit_id,status=excluded.status,
            review_note=excluded.review_note,revision=excluded.revision,
            updated_by=excluded.updated_by,updated_at=excluded.updated_at''',
            (key, audit_id, rule_id, body.status, db.field_codec.seal(note, 'risk_reviews', 'review_note', key),
             revision, actor['id'], stamp))
        store._log(db, actor, 'review_risk', 'audit', audit_id, f'{rule_id};status={body.status}')
    return {'status': body.status, 'note': note, 'revision': revision, 'updated_at': stamp}


def register(app, get_store, get_user, cookie_name):
    @app.put('/api/audits/{audit_id}/risks/{rule_id}/review')
    def update_review(audit_id: str, rule_id: str, body: ReviewBody,
                      session: str | None = Cookie(default=None, alias=cookie_name)):
        return save_review(get_store(), get_user(session), audit_id, rule_id, body)
