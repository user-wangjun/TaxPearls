"""Small Feishu task projection over the existing enterprise task services."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

from webapp import core_inputs, material_batches as batches, members
from webapp.access import AccessDenied, current_actor


def actor(db, config, subject, expected=''):
    if not re.fullmatch(r'ou_[A-Za-z0-9]+', subject):
        raise AccessDenied('飞书身份无效。', 422)
    row = db.execute('''SELECT u.id,u.org_id,u.role,b.id AS binding_id,b.revision
        FROM channel_bindings b JOIN users u ON u.id=b.user_id
        WHERE b.channel='feishu' AND b.scope_key=? AND b.subject_key=? AND u.active=1
        AND b.org_id=u.org_id AND b.role=u.role''',
        (config.scope.key, hashlib.sha256(subject.encode()).hexdigest())).fetchone()
    if not row:
        raise AccessDenied('账户绑定已变化，请重新唤出菜单。', 403)
    user = {key: row[key] for key in ('id', 'org_id', 'role')}
    current_actor(db, user, batches.ROLES)
    raw = json.dumps([config.scope.key, dict(row)], sort_keys=True, separators=(',', ':'))
    tag = hmac.new(config.bridge_key, ('task-binding:' + raw).encode(), hashlib.sha256).hexdigest()
    if expected and not hmac.compare_digest(expected, tag):
        raise AccessDenied('账户绑定已变化，请重新唤出菜单。', 403)
    return user, tag


def task_url(config, task_id):
    return config.public_base + '/?input_task=' + task_id + '#panel=uploadPanel'


def display_name(task, created_at):
    company = ' '.join((task.get('company_name') or '').split())[:120]
    if company:
        title = company
    else:
        try:
            stamp = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
            title = '新建任务 · ' + stamp.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
        except (ValueError, TypeError, AttributeError):
            title = '新建任务（待补充企业）'
    if task.get('start_date') or task.get('end_date'):
        dates = [value if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value or '') else '待确认'
                 for value in (task.get('start_date'), task.get('end_date'))]
        title += ' · ' + ' 至 '.join(dates)
    return title


def execute(store, config, body):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE' if body.operation == 'task_create' else 'BEGIN')
        user, tag = actor(db, config, body.subject, body.binding_tag)
        if body.operation == 'task_menu':
            return {'binding_tag': tag}
        if not body.binding_tag:
            raise AccessDenied('请从当前菜单发起操作。', 422)
        if body.operation == 'task_create':
            if not re.fullmatch(r'[0-9a-f]{32}', body.operation_id):
                raise AccessDenied('任务创建请求无效。', 422)
            key = hmac.new(config.bridge_key, ('task-create:' + tag + ':' + body.operation_id).encode(), hashlib.sha256).hexdigest()
            receipt = db.execute('SELECT task_id FROM feishu_task_creations WHERE operation_key=?', (key,)).fetchone()
            if receipt:
                task_id = receipt['task_id']
                if not db.execute('SELECT 1 FROM core_input_tasks WHERE id=?', (task_id,)).fetchone():
                    raise AccessDenied('本次请求创建的任务已删除，请从新菜单重新创建。', 410)
                batches._authorize(db, user, task_id)
            else:
                task_id = core_inputs.create_task(db, store, user, {})
                db.execute('INSERT INTO feishu_task_creations VALUES (?,?,?)', (key, task_id, members.now()))
            batch = batches._authorize(db, user, task_id)
            version = db.execute('SELECT kind,payload_cipher FROM material_revisions WHERE batch_id=? AND revision=?',
                                 (task_id, batch['revision'])).fetchone()
            payload = json.loads(batches._open(version['payload_cipher'], batches._context(
                user['org_id'], task_id, 'revision:' + version['kind'], batch['revision'])))
            return {'id': task_id, 'display_name': display_name(payload['task'], batch['created_at']),
                    'url': task_url(config, task_id), 'binding_tag': tag}
    if body.operation != 'task_list' or body.view not in {'ongoing', 'completed'} or not 0 <= body.page <= 9:
        raise AccessDenied('任务查询无效。', 422)
    rows = batches.listing(store, user, workflow='core', include_latest=True)
    rows = [row for row in rows if row['payload'] and row['payload'].get('core_task')
            and bool(row['payload'].get('completion')) == (body.view == 'completed')]
    selected = rows[body.page * 10:(body.page + 1) * 10]
    items = []
    with store.connect() as db:
        db.execute('BEGIN')
        actor(db, config, body.subject, tag)
        for row in selected:
            batch = batches._authorize(db, user, row['id'])
            if batch['revision'] != row['revision']:
                raise AccessDenied('任务状态已变化，请刷新列表。', 409)
            payload = row['payload']
            # Return a readable task label; tax IDs, files and financial values
            # stay inside the website. Internal IDs are used only for navigation.
            item = {'id': row['id'], 'display_name': display_name(payload['task'], row['created_at']),
                    'created_at': row['created_at'], 'url': task_url(config, row['id']),
                    'status': 'archived' if payload.get('completion') else payload['processing']['status'],
                    'files': len(payload.get('documents', [])), 'report_ready': False}
            report = db.execute('SELECT 1 FROM core_tax_reports WHERE batch_id=? AND input_revision=? LIMIT 1',
                                (row['id'], row['revision'])).fetchone()
            item['report_ready'] = bool(report)
            job = db.execute('SELECT id,state FROM material_jobs WHERE batch_id=? ORDER BY rowid DESC LIMIT 1', (row['id'],)).fetchone()
            if job:
                counts = {r['state']: r['n'] for r in db.execute('SELECT state,COUNT(*) AS n FROM material_job_files WHERE job_id=? GROUP BY state', (job['id'],))}
                item['progress'] = {'state': job['state'], 'done': counts.get('done', 0), 'failed': counts.get('failed', 0), 'total': sum(counts.values())}
            items.append(item)
    return {'items': items, 'count': len(rows), 'page': body.page, 'has_more': (body.page + 1) * 10 < len(rows), 'binding_tag': tag}
