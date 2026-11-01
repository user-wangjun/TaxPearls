"""Shared application-enforced tenant scope (SQLite has no native row policies)."""


class AccessDenied(PermissionError):
    def __init__(self, message='对象不存在或无权访问。', status=404):
        super().__init__(message)
        self.status = status


def is_teaching_dataset(dataset):
    """Explicit synthetic label, not a guarantee of the material's provenance."""
    return ("仿真" in dataset.company.name or "纯合成测试" in dataset.company.name
            or "TEST" in dataset.company.taxpayer_id.upper())


def current_actor(db, actor, roles=None):
    """Use inside the transaction that reads/writes the protected business rows."""
    actor = dict(actor)
    row = db.execute('SELECT id,org_id,role,active FROM users WHERE id=?', (actor['id'],)).fetchone()
    projected = actor.get('_teaching_staff_id')
    expected = 'org_admin' if projected else actor['role']
    if (not row or not row['active'] or row['org_id'] != actor['org_id'] or row['role'] != expected
            or roles is not None and actor['role'] not in roles):
        raise AccessDenied('账号权限已变化，请刷新后重试。', 403)
    if projected and (actor['role'] != 'teacher' or not _teaching_staff(db, actor['id'], actor['org_id'], projected)):
        raise AccessDenied('教学身份已变化，请重新登录。', 403)
    return row


def _teaching_staff(db, user_id, org_id, staff_id=None):
    row = db.execute("""SELECT c.id FROM university_identity_links l
        JOIN college_user c ON c.id=l.staff_id
        JOIN university_organizations o ON o.college=c.college
        JOIN org_settings s ON s.org_id=o.org_id
        WHERE l.user_id=? AND o.org_id=? AND s.org_type='university'
        AND c.active=1 AND c.role IN ('admin','teacher')""", (user_id, org_id)).fetchone()
    return row and (not staff_id or row['id'] == staff_id) and row['id']


def teaching_actor(db, actor):
    """Scoped classroom capability; canonical account roles are never rewritten."""
    current_actor(db, actor)
    if actor['role'] != 'org_admin':
        return actor
    staff_id = _teaching_staff(db, actor['id'], actor['org_id'])
    return {**actor, 'role':'teacher', '_teaching_staff_id':staff_id} if staff_id else actor


def audit_scope(actor):
    """SQL aliases a=audits, c=clients. Platform management is not a bypass."""
    if actor['role'] not in {'org_admin', 'accountant', 'teacher'}:
        return '0', []
    where, args = 'a.org_id=? AND (a.client_id IS NULL OR c.org_id=a.org_id)', [actor['org_id']]
    if actor['role'] == 'accountant':
        where += ' AND c.accountant_id=?'
        args.append(actor['id'])
    elif actor['role'] == 'teacher':
        # Preparation cases and cases already attached to this teacher's own
        # assignments. Legacy assignments are not an institution-wide grant.
        where += """ AND a.client_id IS NULL
            AND (instr(a.company_name,'仿真')>0 OR instr(a.company_name,'纯合成测试')>0
                 OR instr(upper(tp_taxpayer_value(a.taxpayer_id,'audits',a.id)),'TEST')>0)
            AND (a.created_by=? OR EXISTS (SELECT 1 FROM assignments teaching
                 WHERE teaching.audit_id=a.id AND teaching.org_id=a.org_id AND teaching.created_by=?))"""
        args.extend([actor['id'], actor['id']])
    return where, args


def audit_row(db, audit_id, actor):
    current_actor(db, actor)
    where, args = audit_scope(actor)
    return db.execute('SELECT a.* FROM audits a LEFT JOIN clients c ON c.id=a.client_id WHERE a.id=? AND ' + where,
                      [audit_id, *args]).fetchone()
