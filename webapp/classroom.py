"""Class rosters, immutable paper composition and server-enforced deadlines.

Legacy single-case assignments remain valid. New settings live in separate
tables; publishing changes all paper cases in one transaction, not one by one.
"""
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import csv
import io
import json
import secrets
from typing import Annotated
from urllib.parse import quote

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from webapp import training_profiles, training_stats
from webapp.access import audit_row, current_actor


class ClassroomError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(UTC)


def deadline(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ClassroomError("截止时间必须明确时区。")
    return parsed.astimezone(UTC).isoformat()


def clean_title(value):
    value = value.strip()
    if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ClassroomError("名称须为 1–200 字且不能包含控制字符。")
    return value


def migrate(db):
    # No rewrites, synthesized classes or artificial deadlines for old rows.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS training_classes (
            id TEXT PRIMARY KEY,org_id TEXT NOT NULL,owner_id TEXT NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_training_classes_owner ON training_classes(org_id,owner_id);
        CREATE TABLE IF NOT EXISTS training_class_members (
            class_id TEXT NOT NULL REFERENCES training_classes(id),student_id TEXT NOT NULL REFERENCES users(id),
            PRIMARY KEY(class_id,student_id)
        );
        CREATE TABLE IF NOT EXISTS training_papers (
            id TEXT PRIMARY KEY,org_id TEXT NOT NULL,owner_id TEXT NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,class_id TEXT REFERENCES training_classes(id),deadline_at TEXT,
            published INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS training_assignment_settings (
            assignment_id TEXT PRIMARY KEY REFERENCES assignments(id),class_id TEXT REFERENCES training_classes(id),
            paper_id TEXT REFERENCES training_papers(id),position INTEGER NOT NULL DEFAULT 1,
            points REAL NOT NULL DEFAULT 100 CHECK(points>0),deadline_at TEXT,revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(paper_id,position)
        );
        CREATE INDEX IF NOT EXISTS idx_training_settings_paper ON training_assignment_settings(paper_id);
    """)
    if 'revision' not in {r['name'] for r in db.execute('PRAGMA table_info(training_assignment_settings)')}:
        db.execute('ALTER TABLE training_assignment_settings ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
    # 教学班与组织架构班级实体的显式绑定（FR-U05 院系/专业维度聚合的前置）。
    # 只增列不猜名字：历史上「会计2601 演示班」与实体「会计2601」靠字符串对不上，
    # 自动模糊匹配会把成绩算到错的院系，所以绑定必须由管理员确认（可为空=未归属）。
    if 'college_class_id' not in {r['name'] for r in db.execute('PRAGMA table_info(training_classes)')}:
        db.execute('ALTER TABLE training_classes ADD COLUMN college_class_id TEXT')
    # 归属的判定来源（FR-U01 尾巴）。四态各有明确含义，回填只碰 NULL：
    # NULL=从未判定（本次升级前的历史行）｜auto=按「同院校内同名唯一」自动认领
    # ｜unbound=已判定但未归属（无唯一同名，或管理员显式解绑）｜manual=管理员指定。
    # 有了它，管理员解绑过的教学班不会在下次启动又被自动挂回去。
    #
    # 回填**不在启动时自动跑**：报表数字会因此跨院系移位，属于会被审计追问的改动，
    # 必须先用 `scripts/backfill_class_bindings.py --dry-run` 预演、复核后再 --apply。
    if 'binding_source' not in {r['name'] for r in db.execute('PRAGMA table_info(training_classes)')}:
        db.execute('ALTER TABLE training_classes ADD COLUMN binding_source TEXT')


def decorate(db, item):
    row = db.execute("SELECT * FROM training_assignment_settings WHERE assignment_id=?", (item["id"],)).fetchone()
    item.update({"class_id":None,"paper_id":None,"position":1,"points":100,"deadline_at":None,"revision":1})
    if row:
        item.update({k:v for k,v in dict(row).items() if k != "assignment_id"})
    item["deadline_passed"] = bool(item["deadline_at"] and now() >= datetime.fromisoformat(item["deadline_at"]))
    item["can_submit"] = bool(item["published"] and not item["deadline_passed"])
    course = db.execute("""SELECT c.id,c.name FROM university_assignment_courses ac
        JOIN course c ON c.id=ac.course_id JOIN university_organizations o ON o.college=c.college
        WHERE ac.assignment_id=? AND o.org_id=?""", (item['id'],item['org_id'])).fetchone()
    item.update(course_id=course['id'] if course else None, course_name=course['name'] if course else None)
    return item


def can_access(db, item, user):
    if not item or item["org_id"] != user["org_id"]:
        return False
    if user["role"] == "teacher":
        return item["created_by"] == user["id"] and bool(audit_row(db, item["audit_id"], user))
    if user["role"] != "student" or not item["published"] or item["target_student_id"] not in (None,user["id"]):
        return False
    return not item.get("class_id") or bool(db.execute(
        "SELECT 1 FROM training_class_members WHERE class_id=? AND student_id=?", (item["class_id"],user["id"])
    ).fetchone())


def check_submission(db, assignment_id, user):
    row = db.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    item = decorate(db, dict(row)) if row else None
    account = db.execute("SELECT role,org_id,active FROM users WHERE id=?", (user["id"],)).fetchone()
    if (not account or not account["active"] or account["role"] != "student"
            or account["org_id"] != user["org_id"] or not can_access(db,item,user)):
        raise ClassroomError("作业不存在。",404)
    if item["deadline_passed"]:
        raise ClassroomError("已到截止时间，不能提交或覆盖已有成绩；仍可查阅已获授权作业。",409)


def owned_class(db, class_id, user):
    current_actor(db, user, {'teacher'})
    row = db.execute("SELECT * FROM training_classes WHERE id=? AND org_id=? AND owner_id=?",
                     (class_id,user["org_id"],user["id"])).fetchone()
    if not row:
        raise ClassroomError("班级不存在。",404)
    return dict(row)


def validate_students(db, student_ids, org_id):
    if len(set(student_ids)) != len(student_ids):
        raise ClassroomError("名册不能重复添加同一学生。")
    for sid in student_ids:
        row = db.execute("SELECT role,org_id,active FROM users WHERE id=?", (sid,)).fetchone()
        if not row or row["role"] != "student" or row["org_id"] != org_id or not row["active"]:
            raise ClassroomError("名册只能选择本机构的有效学生账号。")


def set_assignment_settings(db, assignment_id, user, class_id=None, deadline_at=None,
                            paper_id=None, position=1, points=100):
    if class_id:
        owned_class(db,class_id,user)
    db.execute("INSERT INTO training_assignment_settings (assignment_id,class_id,paper_id,position,points,deadline_at) VALUES (?,?,?,?,?,?)",
               (assignment_id,class_id,paper_id,position,points,deadline(deadline_at)))


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassBody(StrictBody):
    name: str = Field(min_length=1,max_length=200)
    student_ids: list[Annotated[str,Field(min_length=1,max_length=64,strict=True)]] = Field(default_factory=list,max_length=500)


class ClassUpdate(ClassBody):
    revision: int = Field(ge=1,strict=True)


Weight = Annotated[float,Field(gt=0,le=1000000,allow_inf_nan=False)]


class PaperItem(StrictBody):
    audit_id: str = Field(min_length=1,max_length=64)
    points: Weight = 100
    weights: dict[str,Weight] = Field(default_factory=dict)
    false_positive_penalty: float = Field(default=5,ge=0,le=100,allow_inf_nan=False)


class PaperBody(StrictBody):
    title: str = Field(min_length=1,max_length=200)
    course_id: str | None = Field(default=None,min_length=1,max_length=200)
    class_id: str | None = Field(default=None,min_length=1,max_length=64)
    deadline_at: AwareDatetime | None = None
    published: bool = False
    items: list[PaperItem] = Field(min_length=1,max_length=30)


class PaperUpdate(StrictBody):
    revision: int = Field(ge=1,strict=True)
    published: bool
    deadline_at: AwareDatetime | None


def gradebook(db, classroom, include_withdrawn=False):
    """教师端成绩单（导出 v1）：学生 × 任务矩阵 + 长表明细。

    口径刻意与 `/api/classes/{id}/statistics` 保持一致，避免"页面一套数、导出另一套数"：
    有效名册与作业指定学生的交集；默认只纳入已发布作业（有提交的撤回作业需显式勾选）；
    冻结案例缺失或损坏的提交记为"材料异常"，不当 0 分也不当未交；
    "自动分"保留客观记录，"当前分"= 复核分优先，人工调分不改客观分。
    """
    roster = db.execute(
        """SELECT u.id,u.username,u.display_name FROM training_class_members m
             JOIN users u ON u.id=m.student_id
            WHERE m.class_id=? AND u.org_id=? AND u.role='student' AND u.active=1
            ORDER BY u.username""", (classroom['id'], classroom['org_id'])).fetchall()
    # a.* 必须保留：加密列 findings_json 的解密要按 audits(id) 校验归属，而这条联表里
    # 审计 id 来自 assignments.audit_id（codec 认这个别名，见 sensitive_storage.row）。
    tasks = db.execute(
        """SELECT a.*,f.org_id AS audit_org,f.findings_json,s.paper_id,s.points,s.position,s.deadline_at
             FROM assignments a JOIN training_assignment_settings s ON s.assignment_id=a.id
             LEFT JOIN audits f ON f.id=a.audit_id
            WHERE s.class_id=? AND a.org_id=? AND a.created_by=?
            ORDER BY a.created_at,a.id""",
        (classroom['id'], classroom['org_id'], classroom['owner_id'])).fetchall()
    submissions = {}
    for row in db.execute(
        """SELECT sub.* FROM submissions sub JOIN assignments a ON a.id=sub.assignment_id
             JOIN training_assignment_settings s ON s.assignment_id=a.id
            WHERE s.class_id=? AND a.org_id=? AND a.created_by=?""",
        (classroom['id'], classroom['org_id'], classroom['owner_id'])):
        submissions[(row['assignment_id'], row['student_id'])] = row
    kept, skipped = [], []
    for task in tasks:
        has_rows = any(key[0] == task['id'] for key in submissions)
        if not task['published']:
            if not has_rows:
                skipped.append({'title': task['title'], 'reason': '未发布且无人提交'})
                continue
            if not include_withdrawn:
                skipped.append({'title': task['title'], 'reason': '已撤回（勾选「含撤回」才计入）'})
                continue
        kept.append(task)
    frozen_cache = {}
    for task in kept:
        try:
            if task['audit_org'] != classroom['org_id']:
                raise ValueError('snapshot not accessible')
            frozen_cache[task['id']] = training_stats.frozen_rules(task['findings_json'])
        except (ValueError, TypeError, KeyError):
            frozen_cache[task['id']] = None
    # 同名任务（如重复发布同一份作业）在成绩单里必须能分辨，否则矩阵列会"撞车"。
    counted, labels, seen = {}, {}, {}
    for task in kept:
        counted[task['title']] = counted.get(task['title'], 0) + 1
    for task in kept:
        title = task['title']
        if counted[title] > 1:
            seen[title] = seen.get(title, 0) + 1
            title = f"{title}（第{seen[title]}份）"
        labels[task['id']] = title
    detail, matrix = [], {person['id']: {} for person in roster}
    for person in roster:
        for task in kept:
            in_scope = task['target_student_id'] in (None, person['id'])
            cell = {'state': '未交' if in_scope else '不在本题范围',
                    'auto': None, 'adjusted': None, 'final': None, 'submitted_at': None}
            row = submissions.get((task['id'], person['id'])) if in_scope else None
            if row is not None:
                cell.update(auto=row['score'], adjusted=row['adjusted_score'],
                            final=row['adjusted_score'] if row['adjusted_score'] is not None else row['score'],
                            submitted_at=row['submitted_at'])
                try:
                    if frozen_cache[task['id']] is None:
                        raise ValueError('missing snapshot')
                    training_stats.selected_answers(row, frozen_cache[task['id']])
                    cell['state'] = '已交'
                except (ValueError, TypeError, KeyError):
                    cell['state'] = '材料异常'
            matrix[person['id']][task['id']] = cell
            detail.append({'student_no': person['username'], 'name': person['display_name'],
                           'class_name': classroom['name'], 'task': labels[task['id']],
                           'kind': '组卷' if task['paper_id'] else '案例作业',
                           'points': task['points'], 'deadline_at': task['deadline_at'], **cell})
    students = []
    for person in roster:
        cells = [matrix[person['id']][task['id']] for task in kept]
        scored = [c['final'] for c in cells if c['final'] is not None]
        expected = [c for c in cells if c['state'] != '不在本题范围']
        submitted = [c for c in expected if c['state'] in ('已交', '材料异常')]
        students.append({'id': person['id'], 'student_no': person['username'], 'name': person['display_name'],
                         'expected': len(expected), 'submitted': len(submitted),
                         'mean': (Decimal(str(sum(scored))) / Decimal(len(scored))).quantize(
                             Decimal('0.01'), rounding=ROUND_HALF_UP) if scored else None})
    return {'class': {k: classroom[k] for k in ('id', 'name', 'revision')},
            'tasks': [{'id': t['id'], 'title': labels[t['id']],
                       'kind': '组卷' if t['paper_id'] else '案例作业'} for t in kept],
            'skipped': skipped, 'students': students, 'matrix': matrix, 'detail': detail,
            'include_withdrawn': include_withdrawn,
            'scope': '有效名册与作业指定学生的交集；仅本班明确分配的作业，不含全机构公开作业。'
                     '当前分取复核分优先（无复核分用自动分），材料异常不计 0 分、也不算未交。'}


def gradebook_csv(book, layout='matrix'):
    """把成绩单渲染成 UTF-8 BOM 的 CSV（Excel 双击即开，中文不乱码）。"""
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    exported = datetime.now(UTC).astimezone().isoformat(timespec='seconds')
    writer.writerow([f"# 班级成绩单 · {book['class']['name']} · 名册 v{book['class']['revision']}"])
    writer.writerow([f"# 导出时间 {exported} · 有效学生 {len(book['students'])} 人 · 任务 {len(book['tasks'])} 个"
                     f" · 含撤回作业 {'是' if book['include_withdrawn'] else '否'}"])
    writer.writerow([f"# 口径：{book['scope']}"])
    if book['skipped']:
        writer.writerow(['# 未纳入：' + '；'.join(f"{s['title']}（{s['reason']}）" for s in book['skipped'])])
    if not book['tasks']:
        # 没有可统计的任务时不铺名册行：一份全是 0/0 的表比空表更容易被误读成"全班零分"。
        writer.writerow(['# 本班暂无可导出的任务：未发布且无人提交，或已撤回且未勾选「含撤回」。'])
        return '\ufeff' + buffer.getvalue()
    if layout == 'detail':
        writer.writerow(['学号', '姓名', '班级', '任务', '类型', '状态', '自动分', '复核分', '当前分', '提交时间'])
        for row in book['detail']:
            writer.writerow([row['student_no'], row['name'], row['class_name'], row['task'], row['kind'],
                             row['state'], _blank(row['auto']), _blank(row['adjusted']),
                             _blank(row['final']), row['submitted_at'] or ''])
    else:
        header = ['学号', '姓名', '班级']
        for task in book['tasks']:
            header += [f"{task['title']}(分)", f"{task['title']}(状态)"]
        header += ['已交/应交', '均分']
        writer.writerow(header)
        for person in book['students']:
            line = [person['student_no'], person['name'], book['class']['name']]
            for task in book['tasks']:
                cell = book['matrix'][person['id']][task['id']]
                line += [_blank(cell['final']), cell['state']]
            line += [f"{person['submitted']}/{person['expected']}", _blank(person['mean'])]
            writer.writerow(line)
    return '\ufeff' + buffer.getvalue()


def _blank(value):
    return '' if value is None else value


def register(app, store_provider, user_for_session, allow, synthetic, cookie_name):
    def user(session, teacher_only=False):
        who = user_for_session(session)
        allow(who, *(('teacher',) if teacher_only else ('teacher','student')))
        return who

    def response(value):
        return JSONResponse(value,headers={"Cache-Control":"private, no-store"})

    def checked(function):
        try:
            return response(function())
        except ClassroomError as exc:
            raise HTTPException(exc.status,str(exc)) from None

    @app.get("/api/classes/students")
    def students(session: str | None = Cookie(default=None,alias=cookie_name)):
        who = user(session,True)
        return response([{k:person[k] for k in ('id','username','display_name')}
                         for person in store_provider().list_users(who['org_id'], actor=who)
                         if person['role']=='student' and person['active']])

    @app.get("/api/classes")
    def classes(session: str | None = Cookie(default=None,alias=cookie_name)):
        who = user(session)
        with store_provider().connect() as db:
            db.execute("BEGIN")
            current_actor(db, who)
            rows = db.execute("SELECT * FROM training_classes WHERE org_id=? ORDER BY created_at,id", (who['org_id'],)).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                roster=db.execute("SELECT u.id,u.username,u.display_name FROM training_class_members m JOIN users u ON u.id=m.student_id WHERE m.class_id=? AND u.org_id=? ORDER BY u.username",(row['id'],who['org_id'])).fetchall()
                if who['role']=='teacher' and row['owner_id']==who['id']:
                    item['students']=[dict(s) for s in roster]; result.append(item)
                elif who['role']=='student' and any(s['id']==who['id'] for s in roster):
                    item['member_count']=len(roster);result.append(item)
        return response(result)

    @app.post("/api/classes")
    def create_class(body: ClassBody,session: str | None = Cookie(default=None,alias=cookie_name)):
        who=user(session,True)
        def create():
            sid=secrets.token_hex(12)
            with store_provider().connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current_actor(db, who, {'teacher'})
                validate_students(db,body.student_ids,who['org_id'])
                db.execute("INSERT INTO training_classes (id,org_id,owner_id,name,revision,created_at,binding_source)"
                           " VALUES (?,?,?,?,1,?, 'unbound')",(sid,who['org_id'],who['id'],clean_title(body.name),now().isoformat()))
                db.executemany("INSERT INTO training_class_members VALUES (?,?)",[(sid,s) for s in body.student_ids])
                store_provider()._log(db,who,'create_class','class',sid,f"members={len(body.student_ids)}")
            return {'id':sid,'revision':1}
        return checked(create)

    @app.get("/api/classes/{class_id}/statistics")
    def class_statistics(class_id: str, include_withdrawn: bool = False,
                         session: str | None = Cookie(default=None,alias=cookie_name)):
        who = user(session,True)
        def get():
            with store_provider().connect() as db:
                db.execute("BEGIN")
                item = owned_class(db,class_id,who)
                return training_stats.collect(db,item,include_withdrawn)
        return checked(get)

    @app.get("/api/classes/{class_id}/statistics.csv")
    def class_gradebook(class_id: str, layout: str = "matrix", include_withdrawn: bool = False,
                        session: str | None = Cookie(default=None,alias=cookie_name)):
        """成绩单导出（CSV，Excel 可直接打开）。与 /statistics 同一数据口径与鉴权。"""
        who = user(session,True)
        if layout not in ("matrix","detail"):
            raise HTTPException(422,"layout 只支持 matrix（成绩单矩阵）或 detail（长表明细）。")
        def build():
            with store_provider().connect() as db:
                db.execute("BEGIN")
                item = owned_class(db,class_id,who)
                book = gradebook(db,item,include_withdrawn)
                stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d")
                name = f"{item['name']}-{'成绩单' if layout=='matrix' else '成绩明细'}-{stamp}.csv"
                return gradebook_csv(book,layout), name
        try:
            content,name = build()
        except ClassroomError as exc:
            raise HTTPException(exc.status,str(exc)) from None
        return Response(content=content.encode("utf-8"),media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                                 "Cache-Control":"private, no-store"})

    @app.get("/api/training/profile")
    def own_profile(session: str | None = Cookie(default=None,alias=cookie_name)):
        who = user_for_session(session)
        allow(who,'student')
        def get():
            with store_provider().connect() as db:
                db.execute("BEGIN")
                person = db.execute("SELECT * FROM users WHERE id=? AND org_id=? AND role='student' AND active=1",
                                    (who['id'],who['org_id'])).fetchone()
                if not person:
                    raise ClassroomError("学生不存在。",404)
                return training_profiles.collect(db,dict(person))
        return checked(get)

    @app.get("/api/classes/{class_id}/students/{student_id}/profile")
    def student_profile(class_id: str, student_id: str, include_withdrawn: bool = False,
                        session: str | None = Cookie(default=None,alias=cookie_name)):
        who = user(session,True)
        def get():
            with store_provider().connect() as db:
                db.execute("BEGIN")
                item = owned_class(db,class_id,who)
                person = db.execute("""SELECT u.* FROM users u JOIN training_class_members m ON m.student_id=u.id
                    WHERE m.class_id=? AND u.id=? AND u.org_id=? AND u.role='student' AND u.active=1""",
                    (class_id,student_id,who['org_id'])).fetchone()
                if not person:
                    raise ClassroomError("学生不存在。",404)
                return training_profiles.collect(db,dict(person),item,include_withdrawn)
        return checked(get)

    @app.put("/api/classes/{class_id}")
    def update_class(class_id: str,body: ClassUpdate,session: str | None = Cookie(default=None,alias=cookie_name)):
        who=user(session,True)
        def update():
            with store_provider().connect() as db:
                db.execute("BEGIN IMMEDIATE")
                item=owned_class(db,class_id,who)
                if item['revision']!=body.revision:
                    raise ClassroomError("班级名册已变化，请刷新后重试。",409)
                validate_students(db,body.student_ids,who['org_id'])
                db.execute("UPDATE training_classes SET name=?,revision=revision+1 WHERE id=?",(clean_title(body.name),class_id))
                db.execute("DELETE FROM training_class_members WHERE class_id=?",(class_id,))
                db.executemany("INSERT INTO training_class_members VALUES (?,?)",[(class_id,s) for s in body.student_ids])
                store_provider()._log(db,who,'update_class','class',class_id,f"revision={body.revision+1};members={len(body.student_ids)}")
            return {'id':class_id,'revision':body.revision+1}
        return checked(update)

    def paper_for(db,paper_id,who):
        current_actor(db, who, {'teacher', 'student'})
        row=db.execute("SELECT * FROM training_papers WHERE id=? AND org_id=?",(paper_id,who['org_id'])).fetchone()
        if not row or (who['role']=='teacher' and row['owner_id']!=who['id']):
            raise ClassroomError("试卷不存在。",404)
        if who['role']=='student' and (not row['published'] or (row['class_id'] and not db.execute("SELECT 1 FROM training_class_members WHERE class_id=? AND student_id=?",(row['class_id'],who['id'])).fetchone())):
            raise ClassroomError("试卷不存在。",404)
        return dict(row)

    @app.put("/api/assignments/{assignment_id}/settings")
    def update_assignment(assignment_id: str,body: PaperUpdate,session: str | None = Cookie(default=None,alias=cookie_name)):
        who=user(session,True)
        def update():
            due=deadline(body.deadline_at)
            with store_provider().connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current_actor(db, who, {'teacher'})
                row=db.execute("SELECT * FROM assignments WHERE id=? AND org_id=? AND created_by=?",(assignment_id,who['org_id'],who['id'])).fetchone()
                if not row:
                    raise ClassroomError("作业不存在。",404)
                item=decorate(db,dict(row))
                if item['paper_id']:
                    raise ClassroomError("组卷题目须通过试卷统一更新，不能单独绕过发布或截止时间。",409)
                if item['revision']!=body.revision:
                    raise ClassroomError("作业已变化，请刷新后重试。",409)
                if body.published and due and datetime.fromisoformat(due)<=now():
                    raise ClassroomError("发布时截止时间必须在未来。")
                db.execute("UPDATE assignments SET published=? WHERE id=?",(int(body.published),assignment_id))
                db.execute("INSERT INTO training_assignment_settings (assignment_id,deadline_at,revision) VALUES (?,?,2) ON CONFLICT(assignment_id) DO UPDATE SET deadline_at=excluded.deadline_at,revision=training_assignment_settings.revision+1",(assignment_id,due))
                store_provider()._log(db,who,'update_assignment','assignment',assignment_id,f"revision={body.revision+1};published={body.published}")
            return {'id':assignment_id,'revision':body.revision+1}
        return checked(update)

    def paper_detail(db,paper_id,who):
        item=paper_for(db,paper_id,who);item['published']=bool(item['published'])
        rows=db.execute("SELECT a.id,a.title,s.position,s.points FROM training_assignment_settings s JOIN assignments a ON a.id=s.assignment_id WHERE s.paper_id=? ORDER BY s.position",(paper_id,)).fetchall()
        item['items']=[dict(r) for r in rows]
        item['deadline_passed']=bool(item['deadline_at'] and now()>=datetime.fromisoformat(item['deadline_at']))
        if who['role']=='student':
            earned=Decimal(0);completed=0
            for question in item['items']:
                sub=db.execute("SELECT score,adjusted_score FROM submissions WHERE assignment_id=? AND student_id=?",(question['id'],who['id'])).fetchone()
                question['score']=None
                if sub:
                    completed+=1;question['score']=sub['adjusted_score'] if sub['adjusted_score'] is not None else sub['score']
                    earned+=Decimal(str(question['score']))*Decimal(str(question['points']))/100
            total=sum((Decimal(str(r['points'])) for r in rows),Decimal(0))
            item['progress']={'completed':completed,'case_count':len(rows),'total_points':float(total),
                              'earned_points':float(earned.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),
                              'score':float((earned*100/total).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if rows and completed==len(rows) else None}
        return item

    @app.get("/api/papers")
    def papers(session: str | None = Cookie(default=None,alias=cookie_name)):
        who=user(session)
        with store_provider().connect() as db:
            db.execute("BEGIN")
            ids=db.execute("SELECT id FROM training_papers WHERE org_id=? ORDER BY created_at DESC,id",(who['org_id'],)).fetchall()
            result=[]
            for row in ids:
                try:
                    result.append(paper_detail(db,row['id'],who))
                except ClassroomError:
                    continue
        return response(result)

    @app.get("/api/papers/{paper_id}")
    def get_paper(paper_id: str,session: str | None = Cookie(default=None,alias=cookie_name)):
        who=user(session)
        def get():
            with store_provider().connect() as db:
                db.execute("BEGIN")
                return paper_detail(db,paper_id,who)
        return checked(get)

    @app.post("/api/papers")
    def create_paper(body: PaperBody,session: str | None = Cookie(default=None,alias=cookie_name)):
        who=user(session,True)
        def create():
            repo=store_provider();pid=secrets.token_hex(12);due=deadline(body.deadline_at)
            if len({i.audit_id for i in body.items})!=len(body.items):
                raise ClassroomError("同一案例不能重复组入试卷。")
            with repo.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current_actor(db, who, {'teacher'})
                if body.class_id:
                    owned_class(db,body.class_id,who)
                if body.published and due and datetime.fromisoformat(due)<=now():
                    raise ClassroomError("发布时截止时间必须在未来。")
                db.execute("INSERT INTO training_papers VALUES (?,?,?,?,?,?,?,1,?)",(pid,who['org_id'],who['id'],clean_title(body.title),body.class_id,due,int(body.published),now().isoformat()))
                for position,question in enumerate(body.items,1):
                    permitted=audit_row(db,question.audit_id,who)
                    if not permitted:
                        raise ClassroomError("案例不存在。",404)
                    entry=repo._audit_dict(permitted)
                    if not synthetic(entry['dataset']):
                        raise ClassroomError("组卷只能使用明确标记的仿真案例。")
                    hits={f.rule.id for f in entry['findings'] if f.status=='hit'}
                    if not hits or set(question.weights)-hits:
                        raise ClassroomError("案例须有可评分风险，权重仅能配置实际命中的规则。")
                    aid=secrets.token_hex(12)
                    db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?,?,?)",(aid,who['org_id'],f"{body.title.strip()} · 第{position}题",question.audit_id,who['id'],None,json.dumps(question.weights,allow_nan=False),question.false_positive_penalty,int(body.published),now().isoformat()))
                    set_assignment_settings(db,aid,who,body.class_id,due,pid,position,question.points)
                    if body.course_id:
                        from webapp.university_workspace import bind_assignment_course
                        bind_assignment_course(db,who,aid,body.course_id,body.class_id)
                repo._log(db,who,'create_paper','paper',pid,f"cases={len(body.items)};published={body.published}")
            return {'id':pid,'revision':1}
        return checked(create)

    @app.put("/api/papers/{paper_id}")
    def update_paper(paper_id: str,body: PaperUpdate,session: str | None = Cookie(default=None,alias=cookie_name)):
        who=user(session,True)
        def update():
            due=deadline(body.deadline_at)
            with store_provider().connect() as db:
                db.execute("BEGIN IMMEDIATE")
                item=paper_for(db,paper_id,who)
                if item['revision']!=body.revision:
                    raise ClassroomError("试卷已变化，请刷新后重试。",409)
                if body.published and due and datetime.fromisoformat(due)<=now():
                    raise ClassroomError("发布时截止时间必须在未来。")
                db.execute("UPDATE training_papers SET published=?,deadline_at=?,revision=revision+1 WHERE id=?",(int(body.published),due,paper_id))
                db.execute("UPDATE assignments SET published=? WHERE id IN (SELECT assignment_id FROM training_assignment_settings WHERE paper_id=?)",(int(body.published),paper_id))
                db.execute("UPDATE training_assignment_settings SET deadline_at=? WHERE paper_id=?",(due,paper_id))
                store_provider()._log(db,who,'update_paper','paper',paper_id,f"revision={body.revision+1};published={body.published}")
            return {'id':paper_id,'revision':body.revision+1}
        return checked(update)
