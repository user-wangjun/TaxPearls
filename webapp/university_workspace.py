"""Teaching workbench over existing identities, classrooms and public knowledge."""
import csv
import io
from datetime import UTC, datetime

from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webapp import university_members
from webapp.access import AccessDenied, current_actor, teaching_actor


class LearningQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    node_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)


class CourseClassrooms(BaseModel):
    model_config = ConfigDict(extra='forbid')
    class_ids: list[str] = Field(default_factory=list, max_length=500)


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS university_course_classrooms (
            course_id TEXT NOT NULL REFERENCES course(id), class_id TEXT NOT NULL REFERENCES training_classes(id),
            PRIMARY KEY(course_id,class_id)
        );
        CREATE TABLE IF NOT EXISTS university_assignment_courses (
            assignment_id TEXT PRIMARY KEY REFERENCES assignments(id), course_id TEXT NOT NULL REFERENCES course(id)
        );
    """)


def course_for_actor(db, actor, course_id):
    current_actor(db, actor, {'teacher'})
    row = db.execute("""SELECT c.* FROM course c JOIN university_organizations o ON o.college=c.college
        JOIN university_identity_links l ON l.user_id=? JOIN college_user u ON u.id=l.staff_id
        WHERE c.id=? AND c.active=1 AND o.org_id=? AND u.college=c.college AND u.active=1
        AND (u.role='admin' OR c.teacher_id=u.id OR c.created_by=u.id)""",
        (actor['id'],course_id,actor['org_id'])).fetchone()
    if not row:
        raise AccessDenied('课程不存在或无权管理。')
    return dict(row)


def bind_assignment_course(db, actor, assignment_id, course_id, class_id):
    course_for_actor(db, actor, course_id)
    if not class_id or not db.execute("SELECT 1 FROM university_course_classrooms WHERE course_id=? AND class_id=?", (course_id,class_id)).fetchone():
        raise HTTPException(422, '课程作业须选择该课程已关联的教学班级。')
    db.execute('INSERT INTO university_assignment_courses VALUES (?,?)', (assignment_id,course_id))


def identity(store, request):
    user = university_members.session_identity(store, request)
    if not user:
        raise HTTPException(401, '请先登录高校账号。')
    if user.get('org_type') != 'university' or not user.get('education_role'):
        raise HTTPException(403, '当前账号尚未关联有效高校教学身份。')
    return user


def dashboard(store, user):
    student = user['education_role'] == 'student'
    linked = not user.get('legacy_teaching')
    with store.connect() as db:
        if linked:
            link = db.execute('SELECT * FROM university_identity_links WHERE user_id=?', (user['id'],)).fetchone()
            table, pid = ('student_info', link['student_id']) if student else ('college_user', link['staff_id'])
            profile = db.execute(f'SELECT * FROM {table} WHERE id=? AND active=1', (pid,)).fetchone()
            actor = teaching_actor(db, user)
        else:
            table = 'student_info' if student else 'college_user'
            pid = user['id'].removeprefix('student-' if student else 'staff-')
            profile = db.execute(f'SELECT * FROM {table} WHERE id=? AND active=1', (pid,)).fetchone()
            actor = None
        if not profile:
            raise HTTPException(403, '教学身份已变化，请重新登录。')
        college = profile['college']
        school = db.execute("SELECT s.display_name FROM university_organizations o JOIN org_settings s ON s.org_id=o.org_id WHERE o.college=?", (college,)).fetchone()
        count = lambda table: db.execute(f'SELECT count(*) FROM {table} WHERE college=? AND active=1', (college,)).fetchone()[0]
        counts = {'courses':count('course'),'students':count('student_info'),'classrooms':0,'assignments':0,
                  'submitted':0,'unreviewed':0}
        classes = []
        if actor and actor['role'] == 'teacher':
            classes = [dict(r) for r in db.execute('SELECT id,name,revision FROM training_classes WHERE org_id=? AND owner_id=? ORDER BY created_at DESC', (actor['org_id'], actor['id']))]
        elif actor and actor['role'] == 'student':
            classes = [dict(r) for r in db.execute('SELECT c.id,c.name,c.revision FROM training_classes c JOIN training_class_members m ON m.class_id=c.id WHERE c.org_id=? AND m.student_id=? ORDER BY c.created_at DESC', (actor['org_id'],actor['id']))]
        counts['classrooms'] = len(classes)
        courses = [dict(r) for r in db.execute('SELECT id,name,semester FROM course WHERE college=? AND active=1 ORDER BY created_at DESC', (college,))]
        # Students see their enrolled courses, never another class's curriculum.
        if student:
            enrolled = []
            for course in courses:
                bindings = db.execute('SELECT class_id FROM university_course_classrooms WHERE course_id=?', (course['id'],)).fetchall()
                if bindings:
                    accessible = linked and db.execute("""SELECT 1 FROM university_course_classrooms c
                        JOIN training_class_members m ON m.class_id=c.class_id
                        WHERE c.course_id=? AND m.student_id=?""", (course['id'], user['id'])).fetchone()
                else:
                    accessible = db.execute('SELECT 1 FROM course_class WHERE course_id=? AND class_name=?', (course['id'],profile['class_name'])).fetchone()
                if accessible:
                    enrolled.append(course)
            courses = enrolled
            counts['courses'] = len(courses)
    capable = actor is not None and actor['role'] in {'teacher','student'} and user['education_role'] != 'school_admin'
    assignments = store.list_assignments(actor) if capable else []
    records = []
    if capable and not student:
        submissions = store.list_submissions(actor)
        counts['submitted'] = len(submissions)
        counts['unreviewed'] = sum(r['adjusted_score'] is None for r in submissions)
        records = [{'id':r['id'],'assignment_id':r['assignment_id'],'student':r['display_name'],'title':r['title'],
                    'score':r['adjusted_score'] if r['adjusted_score'] is not None else r['score'],
                    'reviewed':r['adjusted_score'] is not None,'submitted_at':r['submitted_at']} for r in submissions]
    elif capable:
        for item in assignments:
            submission = store.get_submission(item['id'], actor['id'])
            if submission:
                counts['submitted'] += 1
                records.append({'id':submission['id'],'assignment_id':item['id'],'title':item['title'],
                                'score':submission['adjusted_score'] if submission['adjusted_score'] is not None else submission['score'],
                                'submitted_at':submission['submitted_at'],'reviewed':submission['adjusted_score'] is not None})
    counts['assignments'] = len(assignments)
    return {'school':school[0] if school else college,'classroom_capable':capable,'student':student,
            'counts':counts,'classes':classes,'courses':courses[:10],
            'assignments':[{k:r.get(k) for k in ('id','title','published','deadline_at','deadline_passed','paper_id','course_id','course_name')} for r in assignments],
            'records':records,'queried_at':datetime.now(UTC).isoformat(timespec='seconds'),
            'scope':'课程与教学名册按高校隔离；班级、作业和学情按当前教师或学生的已有访问范围读取。'}


ARCHIVE_SCOPE = ('导出与「班级学情统计」「成绩单导出」同源：当前分取复核分优先，人工调分不改自动分；'
                 '未交记录不进流水（不补 0 分）；已归档任务默认不列出，需显式勾选。')


def _archive_actor(store, user):
    """Resolve the classroom actor exactly like the dashboard does."""
    student = user['education_role'] == 'student'
    linked = not user.get('legacy_teaching')
    with store.connect() as db:
        if linked:
            link = db.execute('SELECT * FROM university_identity_links WHERE user_id=?', (user['id'],)).fetchone()
            if not link:
                raise HTTPException(403, '教学身份已变化，请重新登录。')
            table, pid = ('student_info', link['student_id']) if student else ('college_user', link['staff_id'])
        else:
            table = 'student_info' if student else 'college_user'
            pid = str(user['id']).removeprefix('student-' if student else 'staff-')
        profile = db.execute(f'SELECT * FROM {table} WHERE id=? AND active=1', (pid,)).fetchone()
        if not profile:
            raise HTTPException(403, '教学身份已变化，请重新登录。')
        actor = teaching_actor(db, user) if linked else None
    return actor, profile


def _archive_rows(db, who, class_id, course_id, include_archived, keyword):
    """提交流水：一行 = 一名学生对一份任务的一次提交（订正不重复成行）。"""
    sql = """SELECT sub.assignment_id, sub.student_id, u.username AS student_no, u.display_name AS name,
                    a.title, s.class_id, c.name AS class_name, s.paper_id,
                    COALESCE(tp.archived_at, ta.archived_at) AS archived_at,
                    sub.score, sub.adjusted_score, sub.submitted_at,
                    uc.course_id, co.name AS course_name
               FROM submissions sub
               JOIN assignments a ON a.id=sub.assignment_id
               JOIN training_assignment_settings s ON s.assignment_id=a.id
               JOIN users u ON u.id=sub.student_id
          LEFT JOIN training_classes c ON c.id=s.class_id
          LEFT JOIN training_task_settings ta ON ta.subject_kind='assignment' AND ta.subject_id=a.id
          LEFT JOIN training_task_settings tp ON tp.subject_kind='paper' AND tp.subject_id=s.paper_id
          LEFT JOIN university_assignment_courses uc ON uc.assignment_id=a.id
          LEFT JOIN course co ON co.id=uc.course_id
              WHERE a.org_id=?"""
    args = [who['org_id']]
    if who['role'] == 'student':
        sql += ' AND sub.student_id=?'
        args.append(who['id'])
    else:
        # 教师只看自己建的作业：同校其他教师的班级不在这条流水里，避免"看着有记录却改不了"。
        sql += ' AND a.created_by=?'
        args.append(who['id'])
    if class_id:
        sql += ' AND s.class_id=?'
        args.append(class_id)
    if course_id:
        sql += ' AND uc.course_id=?'
        args.append(course_id)
    if not include_archived:
        sql += ' AND COALESCE(tp.archived_at, ta.archived_at) IS NULL'
    sql += ' ORDER BY sub.submitted_at DESC, sub.student_id, sub.assignment_id'
    rows = []
    for row in db.execute(sql, args):
        item = dict(row)
        item['kind'] = '组卷' if item.pop('paper_id') else '案例作业'
        item['archived'] = bool(item.pop('archived_at'))
        item['reviewed'] = item['adjusted_score'] is not None
        # 当前分 = 复核分优先；自动分原样保留，人工调分不改写客观记录。
        item['final'] = item['adjusted_score'] if item['adjusted_score'] is not None else item['score']
        rows.append(item)
    if keyword:
        needle = keyword.strip().lower()
        rows = [r for r in rows if any(needle in str(r[key] or '').lower()
                                       for key in ('name', 'student_no', 'title', 'class_name', 'course_name'))]
    return rows


def archive_records(store, user, *, class_id=None, course_id=None, include_archived=False, keyword=None):
    student = user['education_role'] == 'student'
    actor, profile = _archive_actor(store, user)
    capable = actor is not None and actor['role'] in {'teacher', 'student'} and user['education_role'] != 'school_admin'
    data = {'rows': [], 'classes': [], 'courses': [], 'scope': ARCHIVE_SCOPE, 'capable': capable}
    if not capable:
        # 通用工作台账号（未绑定课堂身份）没有提交流水；页面按 capable=false 显示说明而不是空表。
        return data
    with store.connect() as db:
        if actor['role'] == 'teacher':
            classes = [dict(r) for r in db.execute(
                'SELECT id,name FROM training_classes WHERE org_id=? AND owner_id=? ORDER BY created_at DESC',
                (actor['org_id'], actor['id']))]
        else:
            classes = [dict(r) for r in db.execute(
                'SELECT c.id,c.name FROM training_classes c JOIN training_class_members m ON m.class_id=c.id '
                'WHERE c.org_id=? AND m.student_id=? ORDER BY c.created_at DESC',
                (actor['org_id'], actor['id']))]
        courses = [dict(r) for r in db.execute(
            'SELECT id,name FROM course WHERE college=? AND active=1 ORDER BY created_at DESC',
            (profile['college'],))] if not student else []
        data.update(classes=classes, courses=courses,
                    rows=_archive_rows(db, actor, class_id, course_id, include_archived, keyword))
    data['total'] = len(data['rows'])
    data['archived_total'] = sum(1 for r in data['rows'] if r['archived'])
    return data


def archive_csv(data):
    """提交流水 CSV：UTF-8 BOM，前三行写明范围与口径，Excel 双击即开。"""
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    exported = datetime.now(UTC).astimezone().isoformat(timespec='seconds')
    writer.writerow(['# 教学档案 · 提交流水'])
    writer.writerow([f'# 导出时间 {exported} · 记录 {len(data["rows"])} 条'
                     f' · 其中已归档 {data["archived_total"]} 条'])
    writer.writerow([f'# 口径：{data["scope"]}'])
    writer.writerow(['学号', '姓名', '班级', '课程', '任务', '类型', '归档', '自动分', '复核分', '当前分', '提交时间'])
    for row in data['rows']:
        writer.writerow([row['student_no'], row['name'], row['class_name'] or '', row['course_name'] or '',
                         row['title'], row['kind'], '已归档' if row['archived'] else '在办',
                         _blank(row['score']), _blank(row['adjusted_score']), _blank(row['final']),
                         row['submitted_at']])
    return ('\ufeff' + buffer.getvalue()).encode('utf-8')


def _blank(value):
    return '' if value is None else value


def register(app, store_provider, effective_rules, ai_config):
    def response(value):
        return JSONResponse(value, headers={'Cache-Control':'private, no-store'})

    @app.get('/api/training/workspace/dashboard')
    def workbench(request: Request):
        store = store_provider()
        return response(dashboard(store, identity(store, request)))

    @app.get('/api/training/workspace/records')
    def archive(request: Request, class_id: str | None = None, course_id: str | None = None,
                include_archived: bool = False, keyword: str | None = Query(default=None, max_length=50)):
        store = store_provider()
        return response(archive_records(store, identity(store, request), class_id=class_id, course_id=course_id,
                                        include_archived=include_archived, keyword=keyword))

    @app.get('/api/training/workspace/records.csv')
    def archive_export(request: Request, class_id: str | None = None, course_id: str | None = None,
                       include_archived: bool = False, keyword: str | None = Query(default=None, max_length=50)):
        store = store_provider()
        data = archive_records(store, identity(store, request), class_id=class_id, course_id=course_id,
                               include_archived=include_archived, keyword=keyword)
        return Response(archive_csv(data), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="teaching-archives.csv"',
                                 'Cache-Control': 'private, no-store'})

    @app.get('/api/training/knowledge/graph')
    def graph(request: Request):
        if request.query_params:
            raise HTTPException(422, '高校公共学习图谱不接受企业或审计范围。')
        store = store_provider()
        identity(store, request)
        from webapp.knowledge import public_graph
        with store.connect() as db:
            data = public_graph(db, effective_rules(), teaching=True)
        base, model, _ = ai_config()
        return response({**data,'ai':{'configured':bool(base and model),'model':model}})

    @app.get('/api/teaching/cases')
    def cases(request: Request):
        store = store_provider()
        user = identity(store, request)
        with store.connect() as db:
            actor = teaching_actor(db, user)
        if actor['role'] != 'teacher':
            raise HTTPException(403, '当前账号无权读取教师备课案例。')
        return response(store.list_audits(actor))

    @app.post('/api/training/knowledge/ask')
    async def ask(body: LearningQuestion, request: Request):
        store = store_provider()
        from webapp.app import NodeQuestion, public_node_answer
        try:
            question = NodeQuestion(node_id=body.node_id, question=body.question)
        except ValueError:
            raise HTTPException(422, '问题包含无效字符。') from None
        result = await public_node_answer(question, None,
                                          stream=False, identity_reader=lambda: identity(store, request))
        return response(result)

    @app.get('/api/teaching/courses')
    def courses(request: Request):
        store = store_provider()
        user = identity(store, request)
        with store.connect() as db:
            actor = teaching_actor(db, user)
            current_actor(db, actor, {'teacher'})
            candidates = db.execute("""SELECT c.id FROM course c JOIN university_organizations o ON o.college=c.college
                WHERE o.org_id=? AND c.active=1 ORDER BY c.created_at DESC""", (actor['org_id'],)).fetchall()
            result = []
            for row in candidates:
                try:
                    course = course_for_actor(db, actor, row['id'])
                except AccessDenied:
                    continue
                course['class_ids'] = [r[0] for r in db.execute('SELECT class_id FROM university_course_classrooms WHERE course_id=?', (row['id'],))]
                result.append(course)
        return response(result)

    @app.put('/api/teaching/courses/{course_id}/classrooms')
    def course_classrooms(course_id: str, body: CourseClassrooms, request: Request):
        store = store_provider()
        user = identity(store, request)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            actor = teaching_actor(db, user)
            course_for_actor(db, actor, course_id)
            from webapp.classroom import owned_class
            for class_id in body.class_ids:
                try:
                    owned_class(db, class_id, actor)
                except ValueError as exc:
                    raise HTTPException(getattr(exc,'status',404), str(exc)) from None
            db.execute('''DELETE FROM university_course_classrooms WHERE course_id=? AND class_id IN
                (SELECT id FROM training_classes WHERE org_id=? AND owner_id=?)''', (course_id,actor['org_id'],actor['id']))
            db.executemany('INSERT INTO university_course_classrooms VALUES (?,?)', [(course_id, k) for k in dict.fromkeys(body.class_ids)])
            store._log(db, actor, 'bind_course_classrooms', 'course', course_id, 'classes='+str(len(set(body.class_ids))))
        return response({'ok':True})
