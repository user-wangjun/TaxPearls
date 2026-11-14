"""Teaching workbench over existing identities, classrooms and public knowledge."""
import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, Request
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
        CREATE TABLE IF NOT EXISTS university_legacy_classrooms (
            college TEXT NOT NULL, class_name TEXT NOT NULL,
            class_id TEXT NOT NULL REFERENCES training_classes(id),
            PRIMARY KEY(college,class_name)
        );
    """)
    migrate_legacy_classrooms(db)


def migrate_legacy_classrooms(db):
    """Bridge existing school rosters once; never rewrite later classroom edits."""
    schools = db.execute("""SELECT o.college,o.org_id FROM university_organizations o
        JOIN org_settings s ON s.org_id=o.org_id WHERE s.org_type='university'""").fetchall()
    for school in schools:
        college, org_id = school['college'], school['org_id']
        staff = db.execute("""SELECT u.id,c.id staff_id,c.role FROM college_user c
            JOIN university_identity_links l ON l.staff_id=c.id JOIN users u ON u.id=l.user_id
            WHERE c.college=? AND u.org_id=? AND c.active=1 AND u.active=1
            AND c.role IN ('admin','teacher') AND u.role IN ('org_admin','teacher')""", (college,org_id)).fetchall()
        teachers = {row['staff_id']: row['id'] for row in staff}
        admins = {row['id'] for row in staff if row['role'] == 'admin'}
        entities = db.execute('SELECT name,archived,created_at FROM college_class WHERE college=?', (college,)).fetchall()
        names = {row['name'] for row in entities if not row['archived']}
        entity_names = {row['name'] for row in entities}
        names.update(row[0] for row in db.execute("""SELECT DISTINCT class_name FROM student_info
            WHERE college=? AND active=1 AND TRIM(COALESCE(class_name,''))<>''""", (college,))
                     if row[0] not in entity_names)
        courses = db.execute("""SELECT c.id,c.teacher_id,b.class_name FROM course c
            JOIN course_class b ON b.course_id=c.id WHERE c.college=? AND c.active=1""", (college,)).fetchall()
        names.update(row['class_name'] for row in courses if row['class_name'] not in entity_names)
        for name in sorted(names):
            if (not name or len(name) > 200 or db.execute(
                    'SELECT 1 FROM university_legacy_classrooms WHERE college=? AND class_name=?',
                    (college,name)).fetchone()):
                continue
            owners = {teachers[row['teacher_id']] for row in courses
                      if row['class_name'] == name and row['teacher_id'] in teachers}
            owner = next(iter(owners)) if len(owners) == 1 else next(iter(admins)) if len(admins) == 1 else None
            # A same-name manual classroom is not proof that two cohorts match.
            if not owner or db.execute('SELECT 1 FROM training_classes WHERE org_id=? AND name=?', (org_id,name)).fetchone():
                continue
            key = json.dumps(['taxpearls-legacy-classroom',college,name], ensure_ascii=False)
            class_id = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
            stamp = min((row['created_at'] for row in entities if row['name'] == name and not row['archived']),
                        default=datetime.now(UTC).isoformat(timespec='seconds'))
            db.execute('INSERT INTO training_classes VALUES (?,?,?,?,1,?)', (class_id,org_id,owner,name,stamp))
            people = db.execute("""SELECT u.id FROM student_info s
                JOIN university_identity_links l ON l.student_id=s.id JOIN users u ON u.id=l.user_id
                WHERE s.college=? AND s.class_name=? AND s.active=1
                AND u.org_id=? AND u.role='student' AND u.active=1
                AND NOT EXISTS (SELECT 1 FROM training_class_members m JOIN training_classes c ON c.id=m.class_id
                    WHERE m.student_id=u.id AND c.org_id=?)""", (college,name,org_id,org_id)).fetchall()
            db.executemany('INSERT INTO training_class_members VALUES (?,?)', [(class_id,row['id']) for row in people])
            db.executemany('INSERT OR IGNORE INTO university_course_classrooms VALUES (?,?)',
                           [(row['id'],class_id) for row in courses if row['class_name'] == name])
            db.execute('INSERT INTO university_legacy_classrooms VALUES (?,?,?)', (college,name,class_id))


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
        from webapp.training_submissions import result_for_student
        for item in assignments:
            submission = result_for_student(store, item['id'], actor['id'])
            if submission:
                counts['submitted'] += 1
                records.append({'id':submission['id'],'assignment_id':item['id'],'title':item['title'],
                                'score':submission['adjusted_score'] if submission['adjusted_score'] is not None else submission['score'],
                                'submitted_at':submission['submitted_at'],'reviewed':bool(submission['reviewed_by']),
                                'result_released':submission['result_released']})
    counts['assignments'] = len(assignments)
    return {'school':school[0] if school else college,'classroom_capable':capable,'student':student,
            'counts':counts,'classes':classes,'courses':courses[:10],
            'assignments':[{k:r.get(k) for k in ('id','title','published','deadline_at','deadline_passed','paper_id','course_id','course_name')} for r in assignments],
            'records':records,'queried_at':datetime.now(UTC).isoformat(timespec='seconds'),
            'scope':'课程与教学名册按高校隔离；班级、作业和学情按当前教师或学生的已有访问范围读取。'}


def register(app, store_provider, effective_rules, ai_config):
    def response(value):
        return JSONResponse(value, headers={'Cache-Control':'private, no-store'})

    @app.get('/api/training/workspace/dashboard')
    def workbench(request: Request):
        store = store_provider()
        return response(dashboard(store, identity(store, request)))

    @app.get('/api/training/knowledge/graph')
    def graph(request: Request):
        initial = list(request.query_params.multi_items()) == [('initial', '1')]
        if request.query_params and not initial:
            raise HTTPException(422, '高校公共学习图谱不接受企业或审计范围。')
        store = store_provider()
        identity(store, request)
        from webapp.knowledge import public_graph
        with store.connect() as db:
            data = public_graph(db, effective_rules(), teaching=True, initial=initial)
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
