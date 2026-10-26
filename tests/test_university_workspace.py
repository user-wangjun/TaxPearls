"""University shell, shared public knowledge and real classroom integration."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.settings import AISettings
from webapp import app as main, university_members
from webapp.access import AccessDenied, current_actor
from webapp.login_guard import LoginGuard
from webapp.storage import Store, _passwords, _now


class UniversityWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'university.db')
        self.addCleanup(patch.stopall)
        patch.object(main, 'store', self.store).start()
        patch.object(main, 'login_guard', LoginGuard()).start()
        self.password = 'University-flow-2026!'
        self.admin = self.store.create_user('campus-admin', self.password, '高校管理员', 'org_admin', 'campus')
        self.student = self.store.create_user('campus-student', self.password, '学生甲', 'student', 'campus')
        self.business = self.store.create_user('business-admin', self.password, '企业管理员', 'org_admin', 'business')
        with self.store.connect() as db:
            db.execute("INSERT INTO org_settings(org_id,org_type,display_name,updated_at) VALUES ('campus','university','示例高校',?)", (_now(),))
            university_members.provision_founder(db, self.admin['id'], 'campus', 5, 10)
            self.staff_id = db.execute('SELECT staff_id FROM university_identity_links WHERE user_id=?', (self.admin['id'],)).fetchone()[0]
            db.execute("INSERT INTO student_info VALUES ('learner','campus','S001','学生甲','教学一班',?,NULL,1,?)", (_passwords.hash(self.password), _now()))
            db.execute("INSERT INTO university_identity_links VALUES (?,NULL,'learner')", (self.student['id'],))
        self.client = TestClient(main.app)
        self.login(self.admin)

    def login(self, user):
        self.client.cookies.clear()
        response = self.client.post('/api/login', json={'username':user['username'], 'password':self.password})
        self.assertEqual(response.status_code, 200, response.text)

    def test_campus_admin_teaches_without_changing_canonical_account_role(self):
        response = self.client.post('/api/classes', json={'name':'教学一班','student_ids':[self.student['id']]})
        self.assertEqual(response.status_code, 200, response.text)
        classroom = response.json()['id']
        course = self.client.post('/api/training/staff/courses',json={'college':'campus','name':'税务实训课程','teacher_id':self.staff_id})
        self.assertEqual(course.status_code,200,course.text)
        cid = course.json()['course']['id']
        linked = self.client.put('/api/teaching/courses/'+cid+'/classrooms',json={'class_ids':[classroom]})
        self.assertEqual(linked.status_code,200,linked.text)
        generated = self.client.post('/api/exercises', json={'rule_id':'R-020','seed':17,'expected_version':'2.0'})
        self.assertEqual(generated.status_code, 200, generated.text)
        audit_id = generated.json()['audit_id']
        unscoped = self.client.post('/api/assignments', json={'title':'范围错误','audit_id':audit_id,'course_id':cid,'published':True})
        self.assertEqual(unscoped.status_code,422,unscoped.text)
        assigned = self.client.post('/api/assignments', json={'title':'教学实训','audit_id':audit_id,'class_id':classroom,'course_id':cid,'published':True})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        aid = assigned.json()['id']
        self.assertEqual(self.store.get_user(self.admin['id'])['role'], 'org_admin')
        self.login(self.student)
        self.assertEqual(self.client.get('/api/training/workspace/dashboard').json()['counts']['courses'],1)
        self.assertEqual([a['id'] for a in self.client.get('/api/assignments').json()], [aid])
        detail = self.client.get('/api/assignments/'+aid)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['course_name'],'税务实训课程')
        submitted = self.client.post('/api/assignments/'+aid+'/submit', json={'selected_rule_ids':['R-020']})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.login(self.admin)
        stats = self.client.get('/api/classes/'+classroom+'/statistics')
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertEqual(stats.json()['active_students'], 1)
        submissions = self.client.get('/api/submissions')
        self.assertEqual(submissions.status_code, 200, submissions.text)
        sid = submissions.json()[0]['id']
        reviewed = self.client.put('/api/submissions/'+sid+'/review', json={'adjusted_score':80,'feedback':'已复核'})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)

    def test_enterprise_admin_cannot_use_campus_teacher_capability(self):
        self.login(self.business)
        self.assertEqual(self.client.post('/api/classes', json={'name':'越权班级','student_ids':[]}).status_code, 403)
        self.assertEqual(self.client.get('/api/training/workspace/dashboard').status_code, 403)
        with self.store.connect() as db:
            spoofed = {**self.business, 'role':'teacher', '_teaching_staff_id':self.staff_id}
            with self.assertRaises(AccessDenied):
                current_actor(db, spoofed, {'teacher'})

    def test_dashboard_has_real_teaching_counts_and_no_enterprise_facts(self):
        response = self.client.get('/api/training/workspace/dashboard')
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['counts']['courses'], 0)
        self.assertEqual(data['counts']['classrooms'], 0)
        self.assertEqual(data['counts']['students'], 1)
        self.assertEqual(data['assignments'], [])
        self.assertNotIn('audits', data)
        self.assertNotIn('taxpayer_id', response.text)

    def test_student_public_question_uses_only_public_evidence(self):
        from tests.test_public_rag import fixtures
        from webapp import node_qa, public_rag
        self.login(self.student)
        with self.store.connect() as db:
            fixtures(db)
        contexts = []
        async def answer(_settings, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            contexts.append(payload)
            source = payload['nodes'][0]['id']
            yield json.dumps({'status':'answerable', 'citations':[source], 'summary':'合成原文说明',
                              'answer':'合成测试：核对抵扣凭证。['+source+']'})
        with patch('webapp.ai_keys.apply', return_value=AISettings(enabled=True, api_key='synthetic')), \
             patch.object(public_rag, 'semantic', return_value={'status':'not_ready','hits':[]}), \
             patch.object(node_qa, 'chat_stream', answer):
            response = self.client.post('/api/knowledge/rag-ask', json={'question':'什么是进项税额抵扣？'})
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith('data: ')]
        self.assertEqual(events[-1]['type'], 'done')
        self.assertTrue(events[-1]['citations'])
        self.assertIsNone(contexts[0]['as_of'])
        text = json.dumps(contexts)
        for private in ['campus-student', 'learner', 'student_info', 'submissions', 'audit_id', 'taxpayer_id']:
            self.assertNotIn(private, text)

    def test_student_public_question_rejects_private_scope_and_revoked_identity(self):
        self.login(self.student)
        for key in ['student_id','class_id','assignment_id','audit_id','taxpayer_id','node_id']:
            result = self.client.post('/api/knowledge/rag-ask', json={'question':'进项税额抵扣', key:'private'})
            self.assertEqual(result.status_code, 422, result.text)
        with self.store.connect() as db:
            db.execute("UPDATE student_info SET active=0 WHERE id='learner'")
        response = self.client.post('/api/knowledge/rag-ask', json={'question':'进项税额抵扣'})
        self.assertIn(response.status_code, (401,403), response.text)

    def test_teaching_graph_reuses_public_ids_and_adds_only_explicit_learning_links(self):
        original = self.client.get('/api/knowledge/graph').json()
        response = self.client.get('/api/training/knowledge/graph')
        self.assertEqual(response.status_code, 200, response.text)
        graph = response.json()
        self.assertEqual(graph['schema_version'], original['schema_version'])
        public = {n['id']:n for n in original['nodes']}
        shared = {n['id']:n for n in graph['nodes'] if n['schema_type'] in {'Policy','Clause','TaxRule','Entity','Metric','Condition','Exception'}}
        self.assertEqual(shared, public)
        self.assertEqual(graph['public_source'], 'shared_policy_catalog')
        self.assertNotIn('taxpayer_id', graph)
        self.assertEqual(self.client.get('/api/training/knowledge/graph?audit_id=other').status_code, 422)

    def test_course_binding_rejects_foreign_same_named_class_without_losing_links(self):
        own = self.client.post('/api/classes', json={'name':'同名班级','student_ids':[]}).json()['id']
        course = self.client.post('/api/training/staff/courses', json={'college':'campus','name':'隔离课程','teacher_id':self.staff_id}).json()['course']['id']
        path = '/api/teaching/courses/'+course+'/classrooms'
        self.assertEqual(self.client.put(path,json={'class_ids':[own]}).status_code,200)
        outsider = self.store.create_user('outside-teacher',self.password,'外校教师','teacher','other-campus')
        self.login(outsider)
        foreign = self.client.post('/api/classes',json={'name':'同名班级','student_ids':[]}).json()['id']
        self.login(self.admin)
        denied = self.client.put(path,json={'class_ids':[own,foreign]})
        self.assertEqual(denied.status_code,404,denied.text)
        self.assertEqual(self.client.get('/api/teaching/courses').json()[0]['class_ids'],[own])
        self.login(self.student)
        # Equal display names never grant enrollment in explicitly bound courses.
        self.assertEqual(self.client.get('/api/training/workspace/dashboard').json()['counts']['courses'],0)
        self.assertEqual(self.client.put(path,json={'class_ids':[]}).status_code,403)

    def test_course_binding_keeps_other_teachers_classrooms(self):
        teacher = self.store.create_user('second-teacher',self.password,'同校教师','teacher','campus')
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user(id,username,password_hash,display_name,college,role,active,created_at) VALUES ('staff-two','second-teacher',?,'同校教师','campus','teacher',1,?)",(_passwords.hash(self.password),_now()))
            db.execute("INSERT INTO university_identity_links VALUES (?,'staff-two',NULL)",(teacher['id'],))
        own = self.client.post('/api/classes',json={'name':'管理员班','student_ids':[]}).json()['id']
        course = self.client.post('/api/training/staff/courses',json={'college':'campus','name':'合授课程','teacher_id':'staff-two'}).json()['course']['id']
        path = '/api/teaching/courses/'+course+'/classrooms'
        self.assertEqual(self.client.put(path,json={'class_ids':[own]}).status_code,200)
        self.login(teacher)
        other = self.client.post('/api/classes',json={'name':'教师班','student_ids':[]}).json()['id']
        self.assertEqual(self.client.put(path,json={'class_ids':[other]}).status_code,200)
        self.assertEqual(set(self.client.get('/api/teaching/courses').json()[0]['class_ids']),{own,other})
        self.assertEqual(self.client.put(path,json={'class_ids':[]}).status_code,200)
        self.assertEqual(self.client.get('/api/teaching/courses').json()[0]['class_ids'],[own])

    def test_learning_question_rejects_enterprise_scope_and_student_teacher_data(self):
        self.login(self.student)
        response = self.client.post('/api/training/knowledge/ask',json={'node_id':'R-020','question':'学习依据是什么','audit_id':'foreign'})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.client.get('/api/teaching/cases').status_code,403)
        self.assertEqual(self.client.get('/api/teaching/courses').status_code,403)

    def test_unified_login_reaches_partner_feedback_and_teacher_processing(self):
        self.login(self.student)
        response = self.client.post('/api/training/my/feedback',
                                    json={'category':'bug','content':'合成联调：来源位置展示核验'})
        self.assertEqual(response.status_code,200,response.text)
        feedback_id = response.json()['feedback']['id']
        self.login(self.admin)
        response = self.client.get('/api/training/staff/trial-feedback')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn(feedback_id,{r['id'] for r in response.json()['feedback']})
        self.assertEqual(self.client.get('/api/training/staff/trial-stats').status_code,200)
        response = self.client.patch('/api/training/staff/trial-feedback/'+feedback_id,
                                     json={'status':'processing','staff_note':'教师联调复核'})
        self.assertEqual(response.status_code,200,response.text)
        self.login(self.student)
        rows = self.client.get('/api/training/my/feedback').json()['feedback']
        self.assertEqual(rows[0]['status'],'processing')

    def test_teaching_capability_is_revalidated_after_profile_disabled(self):
        from webapp.access import teaching_actor
        with self.store.connect() as db:
            actor = teaching_actor(db, self.admin)
            db.execute('UPDATE college_user SET active=0 WHERE id=?', (self.staff_id,))
            with self.assertRaises(AccessDenied):
                current_actor(db, actor, {'teacher'})


if __name__ == '__main__':
    unittest.main()
