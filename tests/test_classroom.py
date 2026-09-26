"""E08 classroom/paper API and persistence invariants; only synthetic cases."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.ops_db import create_backup, restore_backup
from src import training
from webapp import app as module, classroom
from webapp.storage import Store


class ClassroomTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'TAXPEARLS_AI_ENABLED':'0','TAXPEARLS_NOTIFICATION_EMAIL_ENABLED':'0'})
        self.env.start();self.tmp=TemporaryDirectory();self.old=module.store
        self.store=Store(Path(self.tmp.name)/'classroom.db');module.store=self.store
        self.password='Classroom-training-2026!'
        self.teacher=self.store.create_user('classroom-teacher',self.password,'本班教师','teacher','school-a')
        self.other_teacher=self.store.create_user('classroom-other-teacher',self.password,'其他教师','teacher','school-a')
        self.student=self.store.create_user('classroom-student',self.password,'班级学生','student','school-a')
        self.other=self.store.create_user('classroom-other-student',self.password,'其他学生','student','school-a')
        self.foreign=self.store.create_user('classroom-foreign',self.password,'外部学生','student','school-b')
        self.client=TestClient(module.app);self.client.__enter__();self.login(self.teacher)
        self.cases=[self.client.post('/api/exercises',json={'rule_id':rule,'seed':seed,'expected_version':'2.0'}).json()
                    for rule,seed in [('R-020',17),('R-001',18)]]
        self.class_id=self.client.post('/api/classes',json={'name':'审计一班','student_ids':[self.student['id']]}).json()['id']
        self.future=datetime(2032,1,1,tzinfo=UTC)

    def tearDown(self):
        self.client.__exit__(None,None,None);module.store=self.old;self.tmp.cleanup();self.env.stop()

    def login(self,who):
        self.client.cookies.clear()
        response=self.client.post('/api/login',json={'username':who['username'],'password':self.password})
        self.assertEqual(response.status_code,200,response.text)

    def paper(self,published=False,**fields):
        body={'title':'综合风险卷','class_id':self.class_id,'deadline_at':self.future.isoformat(),
              'published':published,'items':[{'audit_id':c['audit_id'],'points':points}
                                          for c,points in zip(self.cases,[30,70])]}
        body.update(fields)
        response=self.client.post('/api/papers',json=body)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['id']

    def publish(self,pid,published=True,revision=1,deadline_at=None):
        return self.client.put('/api/papers/'+pid,json={'revision':revision,'published':published,
                               'deadline_at':deadline_at or self.future.isoformat()})

    def paper_cases(self,pid):
        response=self.client.get('/api/papers/'+pid)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['items']

    def submit(self,aid,answers):
        return self.client.post('/api/assignments/'+aid+'/submit',json={'selected_rule_ids':answers})

    def test_class_roster_teacher_ownership_student_privacy_and_role_guard(self):
        roster=self.client.get('/api/classes/students').json()
        self.assertEqual({p['id'] for p in roster},{self.student['id'],self.other['id']})
        self.assertTrue(all(set(p)=={'id','username','display_name'} for p in roster))
        self.assertEqual(self.client.get('/api/classes').json()[0]['students'][0]['id'],self.student['id'])
        self.login(self.other_teacher)
        self.assertEqual(self.client.get('/api/classes').json(),[])
        self.assertEqual(self.client.put('/api/classes/'+self.class_id,json={'name':'冒用','student_ids':[],'revision':1}).status_code,404)
        self.login(self.student)
        mine=self.client.get('/api/classes').json()[0]
        self.assertEqual(mine['member_count'],1);self.assertNotIn('students',mine)
        self.assertEqual(self.client.get('/api/classes/students').status_code,403)
        self.assertEqual(self.client.post('/api/classes',json={'name':'非法'}).status_code,403)
        self.login(self.other);self.assertEqual(self.client.get('/api/classes').json(),[])
        self.login(self.foreign);self.assertEqual(self.client.get('/api/classes').json(),[])

    def test_class_validation_revision_and_atomic_replacement(self):
        url='/api/classes/'+self.class_id
        original=self.client.get('/api/classes').json()
        for ids in [[self.student['id']]*2,[self.foreign['id']],[self.teacher['id']],['unknown']]:
            self.assertEqual(self.client.put(url,json={'name':'改名','student_ids':ids,'revision':1}).status_code,422)
            self.assertEqual(self.client.get('/api/classes').json(),original)
        changed=self.client.put(url,json={'name':'二班','student_ids':[self.other['id']],'revision':1})
        self.assertEqual(changed.json()['revision'],2)
        self.assertEqual(self.client.put(url,json={'name':'覆盖','student_ids':[],'revision':1}).status_code,409)
        self.assertEqual(self.client.get('/api/classes').json()[0]['name'],'二班')

    def test_paper_draft_publish_multiple_cases_permissions_and_frozen_data(self):
        pid=self.paper();items=self.paper_cases(pid)
        self.assertEqual([i['points'] for i in items],[30,70])
        snapshots=[deepcopy(self.store.get_audit(c['audit_id'])) for c in self.cases]
        self.login(self.student);self.assertEqual(self.client.get('/api/papers').json(),[])
        self.assertEqual(self.client.get('/api/assignments').json(),[])
        self.assertEqual(self.client.get('/api/assignments/'+items[0]['id']).status_code,404)
        self.login(self.teacher);self.assertEqual(self.publish(pid).status_code,200)
        self.login(self.other);self.assertEqual(self.client.get('/api/papers/'+pid).status_code,404)
        self.login(self.other_teacher);self.assertEqual(self.client.get('/api/papers/'+pid).status_code,404)
        self.assertEqual(self.client.get('/api/assignments/'+items[0]['id']).status_code,404)
        self.login(self.student)
        self.assertEqual(len(self.client.get('/api/assignments').json()),2)
        self.assertEqual(self.client.get('/api/papers/'+pid).json()['progress']['case_count'],2)
        self.assertEqual([self.store.get_audit(c['audit_id']) for c in self.cases],snapshots)

    def test_weighted_paper_progress_scores_and_reviewed_scores(self):
        pid=self.paper(True);items=self.paper_cases(pid);self.login(self.student)
        self.assertIsNone(self.client.get('/api/papers/'+pid).json()['progress']['score'])
        first=self.submit(items[0]['id'],self.cases[0]['metadata']['standard_answer'])
        self.assertEqual(first.json()['score'],100)
        partial=self.client.get('/api/papers/'+pid).json()['progress']
        self.assertEqual(partial['completed'],1);self.assertEqual(partial['earned_points'],30)
        self.assertIsNone(partial['score'])
        self.assertEqual(self.submit(items[1]['id'],[]).json()['score'],0)
        self.assertEqual(self.client.get('/api/papers/'+pid).json()['progress']['score'],30)
        self.login(self.teacher)
        sub=self.client.get('/api/submissions',params={'assignment_id':items[1]['id']}).json()[0]
        self.assertEqual(self.client.put('/api/submissions/'+sub['id']+'/review',json={'adjusted_score':50,'feedback':'教学复核'}).status_code,200)
        self.login(self.student)
        self.assertEqual(self.client.get('/api/papers/'+pid).json()['progress']['score'],65)

    def test_deadline_timezone_boundary_and_no_grade_overwrite(self):
        due=datetime(2030,1,2,0,0,tzinfo=UTC)
        pid=self.paper(True,deadline_at='2030-01-02T08:00:00+08:00');items=self.paper_cases(pid)
        self.assertEqual(self.client.get('/api/papers/'+pid).json()['deadline_at'],due.isoformat())
        self.login(self.student)
        with patch('webapp.classroom.now',return_value=due-timedelta(microseconds=1)):
            self.assertEqual(self.submit(items[0]['id'],self.cases[0]['metadata']['standard_answer']).status_code,200)
        before=self.store.get_submission(items[0]['id'],self.student['id'])
        with patch('webapp.classroom.now',return_value=due):
            with patch('src.training.score_submission',side_effect=AssertionError('do not grade expired requests')):
                self.assertEqual(self.submit(items[0]['id'],[]).status_code,409)
            self.assertEqual(self.client.get('/api/assignments/'+items[0]['id']).status_code,200)
            self.assertFalse(self.client.get('/api/assignments/'+items[0]['id']).json()['can_submit'])
            self.assertEqual(self.client.post('/api/assignments/'+items[0]['id']+'/feedback',json={'action':'calculate'}).status_code,200)
        self.assertEqual(self.store.get_submission(items[0]['id'],self.student['id']),before)

    def test_membership_revocation_all_student_paths_and_submission_history_retained(self):
        pid=self.paper(True);aid=self.paper_cases(pid)[0]['id'];self.login(self.student)
        self.assertEqual(self.submit(aid,self.cases[0]['metadata']['standard_answer']).status_code,200)
        self.login(self.teacher)
        self.assertEqual(self.client.put('/api/classes/'+self.class_id,json={'name':'审计一班','student_ids':[],'revision':1}).status_code,200)
        self.login(self.student)
        for path in ['/api/papers/'+pid,'/api/assignments/'+aid,'/api/assignments/'+aid+'/materials']:
            self.assertEqual(self.client.get(path).status_code,404,path)
        self.assertEqual(self.client.post('/api/assignments/'+aid+'/feedback',json={'action':'calculate'}).status_code,404)
        self.assertEqual(self.submit(aid,[]).status_code,404)
        self.assertEqual(self.client.get('/api/assignments').json(),[])
        self.assertIsNotNone(self.store.get_submission(aid,self.student['id']))
        self.assertIsNone(self.store.get_generated_material(self.cases[0]['audit_id'],self.student,aid))

    def test_publish_withdraw_revision_and_disallow_per_case_bypass(self):
        pid=self.paper(True);items=self.paper_cases(pid)
        self.assertEqual(self.publish(pid,False).status_code,200)
        self.assertEqual(self.publish(pid,True).status_code,409)
        for item in items:
            self.assertFalse(self.store.get_assignment(item['id'])['published'])
            self.assertEqual(self.client.put('/api/assignments/'+item['id']+'/settings',json={'published':True,'deadline_at':None,'revision':1}).status_code,409)
        self.login(self.student);self.assertEqual(self.client.get('/api/papers').json(),[])
        self.login(self.teacher);self.assertEqual(self.publish(pid,True,2).status_code,200)
        self.assertTrue(all(self.store.get_assignment(i['id'])['published'] for i in items))

    def test_legacy_single_case_and_new_class_deadline_assignment(self):
        legacy=self.client.post('/api/assignments',json={'title':'旧式作业','audit_id':self.cases[0]['audit_id']}).json()['id']
        current=self.client.post('/api/assignments',json={'title':'本班作业','audit_id':self.cases[0]['audit_id'],
            'class_id':self.class_id,'deadline_at':self.future.isoformat()}).json()['id']
        self.login(self.other)
        self.assertEqual(self.client.get('/api/assignments/'+legacy).status_code,200)
        self.assertEqual(self.client.get('/api/assignments/'+current).status_code,404)
        self.login(self.teacher)
        self.assertEqual(self.client.put('/api/assignments/'+legacy+'/settings',json={'revision':1,'published':False,'deadline_at':None}).status_code,200)
        self.assertEqual(self.client.put('/api/assignments/'+legacy+'/settings',json={'revision':1,'published':True,'deadline_at':None}).status_code,409)
        self.login(self.student);self.assertEqual(self.client.get('/api/assignments/'+legacy).status_code,404)

    def test_no_weight_answer_leak_and_teacher_grade_visibility_scoped(self):
        pid=self.paper(True,items=[{'audit_id':self.cases[0]['audit_id'],'weights':{'R-020':4}}]);aid=self.paper_cases(pid)[0]['id']
        self.login(self.student)
        for payload in [self.client.get('/api/assignments').json()[0],self.client.get('/api/assignments/'+aid).json()]:
            self.assertNotIn('weights',payload);self.assertNotIn('standard_answer',payload)
        self.assertEqual(self.submit(aid,self.cases[0]['metadata']['standard_answer']).status_code,200)
        self.login(self.teacher);sub=self.client.get('/api/submissions').json()[0]
        self.login(self.other_teacher)
        self.assertEqual(self.client.get('/api/submissions').json(),[])
        self.assertEqual(self.client.put('/api/submissions/'+sub['id']+'/review',json={'adjusted_score':0,'feedback':'越权'}).status_code,404)

    def test_invalid_paper_inputs_and_failed_case_roll_back_all_rows(self):
        body={'title':'失败卷','class_id':self.class_id,'items':[{'audit_id':self.cases[0]['audit_id']},{'audit_id':'missing'}]}
        self.assertEqual(self.client.post('/api/papers',json=body).status_code,404)
        with self.store.connect() as db:
            for table in ['assignments','training_papers','training_assignment_settings']:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        for fields in [ {'title':' '},{'items':[]},{'items':[{'audit_id':self.cases[0]['audit_id']}]*2},
                {'items':[{'audit_id':self.cases[0]['audit_id'],'points':0}]},
                {'items':[{'audit_id':self.cases[0]['audit_id'],'weights':{'R-999':1}}]},
                {'deadline_at':'2030-01-01T00:00:00'}, {'class_id':'foreign'},
                {'published':True,'deadline_at':'2000-01-01T00:00:00Z'}]:
            data={'title':'测试卷','items':[{'audit_id':self.cases[0]['audit_id']}]};data.update(fields)
            self.assertIn(self.client.post('/api/papers',json=data).status_code,[404,422],data)
        self.assertEqual(self.client.post('/api/papers',content='{"title":"测试","items":[{"audit_id":"x","points":NaN}]}',headers={'Content-Type':'application/json'}).status_code,422)

    def test_write_rechecks_withdraw_and_membership_after_scoring(self):
        pid=self.paper(True);aid=self.paper_cases(pid)[0]['id'];self.login(self.student)
        original=training.score_submission
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.store.connect() as db:
                db.execute('DELETE FROM training_class_members WHERE class_id=?',(self.class_id,))
            return result
        # Mutation happens after pre-check and score calculation, before insert.
        with patch('src.training.score_submission',side_effect=changed):
            self.assertEqual(self.submit(aid,self.cases[0]['metadata']['standard_answer']).status_code,404)
        self.assertIsNone(self.store.get_submission(aid,self.student['id']))
        with self.store.connect() as db:
            db.execute('INSERT INTO training_class_members VALUES (?,?)',(self.class_id,self.student['id']))
        def withdrawn(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.store.connect() as db:
                db.execute('UPDATE assignments SET published=0 WHERE id=?',(aid,))
            return result
        with patch('src.training.score_submission',side_effect=withdrawn):
            self.assertEqual(self.submit(aid,[]).status_code,404)
        self.assertIsNone(self.store.get_submission(aid,self.student['id']))

    def test_write_rechecks_deadline_and_inactive_account(self):
        pid=self.paper(True);aid=self.paper_cases(pid)[0]['id'];self.login(self.student)
        original=training.score_submission
        def expired(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.store.connect() as db:
                db.execute('UPDATE training_assignment_settings SET deadline_at=? WHERE assignment_id=?',('2000-01-01T00:00:00+00:00',aid))
            return result
        with patch('src.training.score_submission',side_effect=expired):
            self.assertEqual(self.submit(aid,[]).status_code,409)
        self.assertIsNone(self.store.get_submission(aid,self.student['id']))
        with self.store.connect() as db:
            db.execute('UPDATE training_assignment_settings SET deadline_at=NULL WHERE assignment_id=?',(aid,))
            db.execute('UPDATE users SET active=0 WHERE id=?',(self.student['id'],))
        with self.assertRaises(classroom.ClassroomError):
            self.store.save_submission(aid,self.student['id'],[],0,{'score':0},user=self.student)

    def test_concurrent_revision_one_winner_and_identical_backup_restore(self):
        pid=self.paper(True);aid=self.paper_cases(pid)[0]['id'];self.login(self.student)
        self.assertEqual(self.submit(aid,self.cases[0]['metadata']['standard_answer']).status_code,200)
        before=self.client.get('/api/papers/'+pid).json()
        backup=Path(self.tmp.name)/'backup.db';restored=Path(self.tmp.name)/'restored.db'
        create_backup(self.store.path,backup);restore_backup(backup,restored);module.store=Store(restored)
        self.assertEqual(self.client.get('/api/papers/'+pid).json(),before)
        self.assertEqual(len(self.client.get('/api/classes').json()),1)
        module.store=self.store
        teacher_token=self.store.authenticate(self.teacher['username'],self.password)[1]
        def update(_):
            with TestClient(module.app) as client:
                client.cookies.set(module.COOKIE_NAME,teacher_token)
                return client.put('/api/papers/'+pid,json={'revision':1,'published':False,'deadline_at':None}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(update,range(2))),[200,409])

    def test_legacy_schema_migration_preserves_assignment_and_submission_rows(self):
        aid=self.client.post('/api/assignments',json={'title':'迁移前作业','audit_id':self.cases[0]['audit_id']}).json()['id']
        self.login(self.student);self.assertEqual(self.submit(aid,[]).status_code,200)
        with self.store.connect() as db:
            before=[dict(r) for r in db.execute('SELECT * FROM assignments')]
            scores=[dict(r) for r in db.execute('SELECT * FROM submissions')]
            # Only this test's temporary database is deliberately downgraded.
            for table in ['training_assignment_settings','training_papers','training_class_members','training_classes']:
                db.execute('DROP TABLE '+table)
        module.store=Store(self.store.path)
        with module.store.connect() as db:
            self.assertEqual([dict(r) for r in db.execute('SELECT * FROM assignments')],before)
            self.assertEqual([dict(r) for r in db.execute('SELECT * FROM submissions')],scores)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM training_classes').fetchone()[0],0)
        item=self.client.get('/api/assignments/'+aid).json()
        self.assertIsNone(item['class_id']);self.assertIsNone(item['deadline_at'])
        self.assertEqual(item['submission']['details']['score'],0)

    def test_institution_paper_scope_role_guards_and_empty_class_not_public(self):
        institution=self.paper(True,class_id=None)
        empty=self.client.post('/api/classes',json={'name':'空班'}).json()['id']
        private=self.paper(True,class_id=empty)
        self.login(self.other)
        self.assertEqual(self.client.get('/api/papers/'+institution).status_code,200)
        self.assertEqual(self.client.get('/api/papers/'+private).status_code,404)
        self.login(self.foreign)
        self.assertEqual(self.client.get('/api/papers/'+institution).status_code,404)
        self.assertEqual(self.client.get('/api/papers').json(),[])
        for role in ['org_admin','accountant','platform_admin']:
            person=self.store.create_user('e08-'+role,self.password,role,role,'school-a');self.login(person)
            self.assertEqual(self.client.get('/api/classes').status_code,403)
            self.assertEqual(self.client.get('/api/papers').status_code,403)
            self.assertEqual(self.client.post('/api/papers',json={'title':'越权','items':[{'audit_id':self.cases[0]['audit_id']}]}).status_code,403)
        self.client.cookies.clear();self.assertEqual(self.client.get('/api/papers').status_code,401)

    def test_inactive_roster_real_material_and_nonfinite_fields_rejected_without_echo(self):
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?',(self.other['id'],))
        self.assertEqual(self.client.post('/api/classes',json={'name':'无效学生班','student_ids':[self.other['id']]}).status_code,422)
        entry=deepcopy(self.store.get_audit(self.cases[0]['audit_id']))
        entry['dataset'].company.name='真实客户';entry['dataset'].company.taxpayer_id='91440100123456789X'
        with patch.object(self.store,'get_audit',return_value=entry):
            self.assertEqual(self.client.post('/api/papers',json={'title':'真实客户卷','items':[{'audit_id':self.cases[0]['audit_id']}]}).status_code,422)
        for value in ['NaN','Infinity','-Infinity']:
            response=self.client.post('/api/papers',json={'title':'非法分值','items':[{'audit_id':self.cases[0]['audit_id'],'points':value}]})
            self.assertEqual(response.status_code,422);self.assertNotIn('input',response.text)
        secret='NEVER-ECHO-SUPPLIED-SECRET'
        response=self.client.post('/api/classes',json={'name':'表单错误','password':secret})
        self.assertEqual(response.status_code,422);self.assertNotIn(secret,response.text)

    def test_single_case_target_class_validation_and_owner_update(self):
        body={'title':'指定学生','audit_id':self.cases[0]['audit_id'],'class_id':self.class_id,'target_student_id':self.other['id']}
        self.assertEqual(self.client.post('/api/assignments',json=body).status_code,422)
        body['target_student_id']=self.student['id'];aid=self.client.post('/api/assignments',json=body).json()['id']
        self.login(self.other_teacher)
        self.assertEqual(self.client.put('/api/assignments/'+aid+'/settings',json={'revision':1,'published':False,'deadline_at':None}).status_code,404)
        self.login(self.teacher)
        self.assertEqual(self.client.put('/api/assignments/'+aid+'/settings',json={'revision':1,'published':True}).status_code,422)
        due='2030-01-01T08:00:00+08:00'
        self.assertEqual(self.client.put('/api/assignments/'+aid+'/settings',json={'revision':1,'published':True,'deadline_at':due}).status_code,200)
        self.assertEqual(self.store.get_assignment(aid)['deadline_at'],'2030-01-01T00:00:00+00:00')
        self.assertEqual(self.store.get_assignment(aid)['class_id'],self.class_id)

    def test_classroom_frontend_asset_and_training_controls_served(self):
        script=self.client.get('/classroom.js')
        self.assertEqual(script.status_code,200)
        self.assertIn('text/javascript',script.headers['content-type'])
        self.assertIn('createTrainingClassroom',script.text)
        page=self.client.get('/').text
        self.assertIn('src="/classroom.js?',page)
        for control in ['classRoster','paperDraft','paperDetail','paperClass','assignmentDeadline','exerciseDeadlineStatus']:
            self.assertIn('id="'+control+'"',page)


if __name__=='__main__':
    unittest.main()
