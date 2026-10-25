"""教学档案（提交流水）：范围、归档口径、筛选与 CSV 导出。

这里直接打 `_archive_rows` / `archive_csv` 这两个纯函数，不走 HTTP 登录链路：
被验证的是"谁的行该出现、归档默认怎么算、当前分取哪个分、CSV 表头写了什么口径"，
课堂身份与路由鉴权由既有 `test_university_workspace` 覆盖。
"""
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from webapp import university_workspace as archives
from webapp.storage import Store

PASSWORD = 'Archive-check-2026!'


class ArchiveRowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'archives.sqlite'
        patch.dict(os.environ, {'TAXPEARLS_DB': str(self.path)}).start()
        self.addCleanup(patch.stopall)
        # keepalive：Windows 上并发初始化 WAL 会以 readonly 面目偶发报错（见 storage 注释）。
        self.store = Store(self.path, keepalive=True)
        self.addCleanup(self.store.close)
        self.org = 'campus'
        self.teacher = self.store.create_user('teacher1', PASSWORD, '教师一', 'teacher', self.org)
        self.other = self.store.create_user('teacher2', PASSWORD, '教师二', 'teacher', self.org)
        self.s1 = self.store.create_user('2026001', PASSWORD, '陈雨欣', 'student', self.org)
        self.s2 = self.store.create_user('2026002', PASSWORD, '李四', 'student', self.org)
        self.who = {'id': self.teacher['id'], 'org_id': self.org, 'role': 'teacher'}
        self.student_who = {'id': self.s1['id'], 'org_id': self.org, 'role': 'student'}
        self.stamp = datetime.now(UTC).isoformat(timespec='seconds')
        self._seed()

    def _seed(self):
        # 加密列由触发器守着（audits 的 taxpayer_id/dataset_json/findings_json、
        # submissions 的 details_json），种子数据必须走同一个 codec 才能落库。
        codec = self.store._field_codec
        seal = codec.seal
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO audits(id,org_id,client_id,created_by,company_name,taxpayer_id,
                          taxpayer_lookup,industry,period,dataset_json,findings_json,summary_json,audited_at)
                          VALUES ('audit1',?,NULL,?,'仿真案例',?,?,'制造业','2026',?,?,'{}',?)""",
                       (self.org, self.teacher['id'], seal('TEST001', 'audits', 'taxpayer_id', 'audit1'),
                        codec.taxpayer_lookup(self.org, 'TEST001'),
                        seal('{}', 'audits', 'dataset_json', 'audit1'),
                        seal('{}', 'audits', 'findings_json', 'audit1'), self.stamp))
            db.execute("INSERT INTO training_classes(id,org_id,owner_id,name,revision,created_at) "
                       "VALUES ('class1',?,?,'会计2601 演示班',1,?)", (self.org, self.teacher['id'], self.stamp))
            db.execute("INSERT INTO training_class_members VALUES ('class1',?)", (self.s1['id'],))
            db.execute("INSERT INTO training_class_members VALUES ('class1',?)", (self.s2['id'],))
            db.execute("INSERT INTO course(id,college,name,semester,active,created_at) "
                       "VALUES ('course1','演示学院','涉税风险识别与合规实务','2026-2027-1',1,?)", (self.stamp,))
            db.execute("INSERT INTO course(id,college,name,semester,active,created_at) "
                       "VALUES ('course2','演示学院','税务会计基础','2026-2027-1',1,?)", (self.stamp,))
            for assignment_id, owner, title, published in (
                    ('a1', self.teacher['id'], '案例作业一', 1),
                    ('a2', self.teacher['id'], '已归档作业', 1),
                    ('a3', self.other['id'], '别人的作业', 1)):
                db.execute("""INSERT INTO assignments(id,org_id,title,audit_id,created_by,target_student_id,
                              weights_json,false_positive_penalty,published,created_at)
                              VALUES (?,?,?,'audit1',?,NULL,'{}',0.5,?,?)""",
                           (assignment_id, self.org, title, owner, published, self.stamp))
                db.execute("""INSERT INTO training_assignment_settings(assignment_id,class_id,paper_id,
                              position,points,deadline_at,revision) VALUES (?,'class1',NULL,1,100,NULL,1)""",
                           (assignment_id,))
            # 归档记在旁挂表 training_task_settings（作业本体与提交记录都不动）。
            db.execute("""INSERT INTO training_task_settings(subject_kind,subject_id,org_id,archived_at,
                          updated_by,updated_at) VALUES ('assignment','a2',?,?,?,?)""",
                       (self.org, self.stamp, self.teacher['id'], self.stamp))
            # 课程归属只挂在 a1 上，用来验证课程筛选与 CSV 的课程列。
            db.execute("INSERT INTO university_assignment_courses VALUES ('a1','course1')")
            for submission_id, assignment_id, student, score, adjusted in (
                    ('sub1', 'a1', self.s1['id'], 80.0, None),
                    ('sub2', 'a1', self.s2['id'], 70.0, 75.0),
                    ('sub3', 'a2', self.s1['id'], 60.0, None),
                    ('sub4', 'a3', self.s1['id'], 55.0, None)):
                db.execute("""INSERT INTO submissions(id,assignment_id,student_id,answers_json,score,
                              details_json,submitted_at,adjusted_score) VALUES (?,?,?,'{}',?,?,?,?)""",
                           (submission_id, assignment_id, student, score,
                            seal('{}', 'submissions', 'details_json', assignment_id, student),
                            self.stamp, adjusted))

    def rows(self, who=None, **kwargs):
        with self.store.connect() as db:
            return archives._archive_rows(db, who or self.who, kwargs.get('class_id'),
                                          kwargs.get('course_id'), kwargs.get('include_archived', False),
                                          kwargs.get('keyword'))

    def test_archived_and_other_teachers_rows_stay_out_by_default(self):
        rows = self.rows()
        self.assertEqual(sorted(r['student_no'] for r in rows), ['2026001', '2026002'])
        self.assertEqual({r['title'] for r in rows}, {'案例作业一'})

    def test_include_archived_adds_rows_and_marks_them(self):
        rows = self.rows(include_archived=True)
        self.assertEqual(len(rows), 3)
        archived = [r for r in rows if r['archived']]
        self.assertEqual([r['title'] for r in archived], ['已归档作业'])
        # 别人的作业即使在同校同班也不进这条流水。
        self.assertNotIn('别人的作业', {r['title'] for r in rows})

    def test_filters_by_class_course_and_keyword(self):
        self.assertEqual(len(self.rows(class_id='class1')), 2)
        self.assertEqual(len(self.rows(class_id='class-missing')), 0)
        self.assertEqual({r['course_name'] for r in self.rows(course_id='course1')}, {'涉税风险识别与合规实务'})
        self.assertEqual(len(self.rows(course_id='course2')), 0)
        self.assertEqual([r['student_no'] for r in self.rows(keyword='2026002')], ['2026002'])
        self.assertEqual([r['name'] for r in self.rows(keyword='李四')], ['李四'])
        self.assertEqual({r['title'] for r in self.rows(keyword='案例作业')}, {'案例作业一'})

    def test_current_score_prefers_reviewed_and_keeps_automatic(self):
        rows = {r['student_no']: r for r in self.rows()}
        self.assertEqual(rows['2026001']['final'], 80.0)
        self.assertIsNone(rows['2026001']['adjusted_score'])
        self.assertFalse(rows['2026001']['reviewed'])
        # 人工复核后当前分取复核分，但自动分原样保留，不改写客观记录。
        self.assertEqual(rows['2026002']['final'], 75.0)
        self.assertEqual(rows['2026002']['score'], 70.0)
        self.assertTrue(rows['2026002']['reviewed'])

    def test_student_sees_own_rows_even_under_another_teacher(self):
        # 学生看的是"自己的记录"，不按建作业的教师过滤：别人班/别人建的作业，
        # 只要提交人是本人，就该留在自己的档案里（与教师视角不同）。
        rows = self.rows(who=self.student_who)
        self.assertEqual({r['student_no'] for r in rows}, {'2026001'})
        self.assertEqual({r['title'] for r in rows}, {'案例作业一', '别人的作业'})

    def test_unlinked_account_without_college_profile_is_rejected(self):
        stranger = {'id': 'no-such-user', 'org_id': self.org, 'role': 'teacher',
                    'education_role': 'teacher', 'legacy_teaching': True}
        with self.assertRaises(HTTPException) as caught:
            archives.archive_records(self.store, stranger)
        self.assertEqual(caught.exception.status_code, 403)


class ArchiveCsvTests(unittest.TestCase):
    def data(self):
        return {'rows': [{'student_no': '2026001', 'name': '陈雨欣', 'class_name': '会计2601 演示班',
                          'course_name': '涉税风险识别与合规实务', 'title': '案例作业一', 'kind': '案例作业',
                          'archived': False, 'score': 80.0, 'adjusted_score': None, 'final': 80.0,
                          'submitted_at': '2026-10-25T10:00:00+08:00'},
                         {'student_no': '2026002', 'name': '李四', 'class_name': '会计2601 演示班',
                          'course_name': None, 'title': '已归档作业', 'kind': '组卷',
                          'archived': True, 'score': 70.0, 'adjusted_score': 75.0, 'final': 75.0,
                          'submitted_at': '2026-10-25T11:00:00+08:00'}],
                'archived_total': 1, 'scope': archives.ARCHIVE_SCOPE}

    def test_csv_has_bom_scope_lines_and_blank_cells(self):
        raw = archives.archive_csv(self.data())
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        lines = raw.decode('utf-8-sig').splitlines()
        self.assertEqual(lines[0], '# 教学档案 · 提交流水')
        self.assertIn('记录 2 条', lines[1])
        self.assertIn('其中已归档 1 条', lines[1])
        self.assertTrue(lines[2].startswith('# 口径：'))
        self.assertEqual(lines[3].split(','), ['学号', '姓名', '班级', '课程', '任务', '类型', '归档',
                                               '自动分', '复核分', '当前分', '提交时间'])
        self.assertEqual(lines[4].split(','), ['2026001', '陈雨欣', '会计2601 演示班', '涉税风险识别与合规实务',
                                               '案例作业一', '案例作业', '在办', '80.0', '', '80.0',
                                               '2026-10-25T10:00:00+08:00'])
        self.assertEqual(lines[5].split(',')[6], '已归档')


if __name__ == '__main__':
    unittest.main()
