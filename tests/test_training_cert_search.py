"""学生端「找证书」接口测试：证书搜索 + 备考指引（倒计时/重点知识点/实操工作）。

与 test_training_portal 同构：临时库 + 迷你 FastAPI 只挂 training_portal 路由，
不导入完整 webapp.app（其全局 Store 指向默认库）。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_portal import register, today_cst

_passwords = PasswordHasher()
COOKIE = "test_training_cookie"
NOW = "2026-10-21T10:00:00"
PASSWORD = "secret123"


class CertificateSearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "cert.db")
        app = FastAPI()
        register(app, lambda: self.store, COOKIE)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        student = uuid.uuid4().hex
        cert = uuid.uuid4().hex
        other = uuid.uuid4().hex
        inactive = uuid.uuid4().hex
        self.future = (today_cst() + timedelta(days=30)).isoformat()
        self.planned = (today_cst() + timedelta(days=10)).isoformat()
        chapter = uuid.uuid4().hex
        leaf = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student, "示例大学", "2026001", "李同学", None,
                        _passwords.hash(PASSWORD), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj-2027", "初级会计职称", "面向 2027 考试年度的初级会计职称",
                        '["初级会计实务","经济法基础"]', "依据考试公告", NOW, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (other, "cpa", "注册会计师", "执业资格考试",
                        '["会计","审计"]', "", NOW, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,0,?,?)",
                       (inactive, "cjkj-old", "旧版初级会计", "已下架", '["初级会计实务"]', "", NOW, NOW))
            db.execute("INSERT INTO exam_date (id,certificate_id,round_label,date_type,exam_date,"
                       "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, cert, "2027年第一批", "official", self.future, NOW, NOW))
            # 章 + 子知识点（实操清单按章编码 SW-03 命中）
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,parent_id,"
                       "description,source_ref,outline_version,active,created_at,updated_at,"
                       "review_status) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,'')",
                       (chapter, cert, "SW-03", "资产", "初级会计实务", None,
                        "官方能力要求——掌握 2 项、熟悉 0 项、了解 0 项", "", "2026", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,parent_id,"
                       "description,source_ref,outline_version,active,created_at,updated_at,"
                       "review_status) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,'')",
                       (leaf, cert, "SW-03-04", "存货", "初级会计实务", chapter,
                        "1.存货的初始计量（掌握）\n2.存货的发出计价（掌握）", "", "2026", NOW, NOW))
            db.execute("INSERT INTO knowledge_point_mark (id,knowledge_point_id,mark_type,level,"
                       "basis_ref,basis_version,created_by,created_at)"
                       " VALUES (?,?,'high_freq','high','2026大纲《初级会计实务》','2026大纲',NULL,?)",
                       (uuid.uuid4().hex, leaf, NOW))
            db.execute("INSERT INTO knowledge_point_link (id,knowledge_point_id,target_type,"
                       "target_id,created_by,created_at) VALUES (?,?,'rule','R-013',NULL,?)",
                       (uuid.uuid4().hex, leaf, NOW))
        self.student, self.cert, self.other, self.inactive = student, cert, other, inactive

    def _login(self):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": "2026001", "password": PASSWORD, "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _search(self, q=""):
        res = self.client.get("/api/training/certificates/search", params={"q": q})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _guide(self, cert_id=None):
        res = self.client.get("/api/training/my/certificate-guide",
                              params={"certificate_id": cert_id or self.cert})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    # ---- 搜索 -----------------------------------------------------------

    def test_search_requires_login(self):
        self.assertEqual(self.client.get("/api/training/certificates/search").status_code, 401)
        self.assertEqual(self.client.get("/api/training/my/certificate-guide",
                                         params={"certificate_id": self.cert}).status_code, 401)

    def test_empty_query_lists_active_certificates_only(self):
        self._login()
        data = self._search("")
        codes = {c["code"] for c in data["results"]}
        self.assertEqual(codes, {"cjkj-2027", "cpa"})
        self.assertNotIn("cjkj-old", codes)
        self.assertIn("初级会计实务", data["suggestions"])
        # 建议词里带上常考知识点，便于空结果时给出口
        self.assertIn("存货", data["suggestions"])

    def test_search_by_name_and_countdown(self):
        self._login()
        data = self._search("初级会计")
        self.assertEqual([c["code"] for c in data["results"]], ["cjkj-2027"])
        hit = data["results"][0]
        self.assertEqual(hit["match"][0]["field"], "name")
        self.assertEqual(hit["next_exam"]["countdown"]["days_left"], 30)
        self.assertEqual(hit["counts"]["knowledge_points"], 2)
        self.assertEqual(hit["counts"]["high_freq"], 1)
        self.assertEqual(hit["counts"]["practice_kps"], 1)

    def test_search_counts_include_zero_for_absent_marks_and_links(self):
        self._login()
        empty = next(c for c in self._search()["results"] if c["id"] == self.other)
        self.assertEqual(empty["counts"], {"knowledge_points": 0, "high_freq": 0, "practice_kps": 0})
        with self.store.connect() as db:
            db.execute("DELETE FROM knowledge_point_mark")
        counts = self._search("cjkj-2027")["results"][0]["counts"]
        self.assertEqual(counts, {"knowledge_points": 2, "high_freq": 0, "practice_kps": 1})
        with self.store.connect() as db:
            db.execute("UPDATE knowledge_point_link SET active=0")
        counts = self._search("cjkj-2027")["results"][0]["counts"]
        self.assertEqual(counts, {"knowledge_points": 2, "high_freq": 0, "practice_kps": 0})
        with self.store.connect() as db:
            db.execute("UPDATE knowledge_point SET active=0")
        self.assertEqual(self._search("cjkj-2027")["results"][0]["counts"], empty["counts"])

    def test_search_by_subject_and_case_insensitive_code(self):
        self._login()
        self.assertEqual(self._search("经济法")["results"][0]["match"][0]["field"], "subjects")
        self.assertEqual([c["code"] for c in self._search("CJKJ")["results"]], ["cjkj-2027"])

    def test_search_by_knowledge_point_explains_hit(self):
        self._login()
        hit = self._search("存货")["results"][0]
        self.assertEqual(hit["match"][0]["field"], "knowledge_point")
        self.assertIn("SW-03-04", hit["match"][0]["text"])

    def test_search_multiple_tokens_are_and(self):
        self._login()
        self.assertEqual(self._search("初级 存货")["total"], 1)
        self.assertEqual(self._search("初级 注册会计师")["total"], 0)
        self.assertEqual(self._search("不存在的证")["total"], 0)

    # ---- 备考指引 -------------------------------------------------------

    def test_guide_countdown_and_focus(self):
        self._login()
        data = self._guide()
        self.assertEqual(data["certificate"]["code"], "cjkj-2027")
        self.assertEqual(data["exam"]["countdown"]["days_left"], 30)
        self.assertEqual(data["exam"]["countdown_scope"], "certificate")
        # 重点清单：常考 + 掌握要求 + 有仿真规则的知识点排第一
        top = data["focus"]["items"][0]
        self.assertEqual(top["code"], "SW-03-04")
        self.assertIn("常考", top["reason"])
        self.assertIn("掌握", top["reason"])
        self.assertEqual(top["rule_links"], 1)
        self.assertIn("排序口径", data["focus"]["rule"])
        self.assertEqual(data["progress"]["kp_total"], 1)

    def test_guide_practical_work_items(self):
        self._login()
        practical = self._guide()["practical"]
        self.assertTrue(practical["items"], "章节实操清单应至少有一条")
        self.assertTrue(all(item["steps"] for item in practical["items"]))
        self.assertTrue(all(item["deliverable"] for item in practical["items"]))
        self.assertTrue(practical["source_note"])
        self.assertEqual(practical["items"][0]["chapter_code"], "SW-03")
        # 规则仿真案例单独成组，来源可追到规则 id/名称
        labs = practical["rule_labs"]
        self.assertEqual(len(labs), 1)
        self.assertEqual(labs[0]["rule_code"], "R-013")
        self.assertTrue(labs[0]["title"])

    def test_guide_subject_and_chapter_tree(self):
        self._login()
        data = self._guide()
        # 只列出已配置知识点的科目：经济法基础在本用例里没有知识点，不硬凑空章节。
        self.assertEqual([s["subject"] for s in data["subjects"]], ["初级会计实务"])
        chapter = data["subjects"][0]["chapters"][0]
        self.assertEqual(chapter["code"], "SW-03")
        self.assertEqual(chapter["requirement"]["master"], 2)
        self.assertEqual(chapter["kps"][0]["code"], "SW-03-04")

    def test_guide_uses_personal_goal_date(self):
        self._login()
        res = self.client.post("/api/training/my/goals",
                               json={"certificate_id": self.cert, "planned_date": self.planned})
        self.assertEqual(res.status_code, 200, res.text)
        data = self._guide()
        self.assertEqual(data["exam"]["countdown"]["days_left"], 10)
        self.assertEqual(data["exam"]["countdown_scope"], "goal")
        self.assertEqual(data["exam"]["countdown_source"], "planned")
        self.assertEqual(data["pacing"]["days_left"], 10)

    def test_guide_certificate_without_knowledge_points(self):
        self._login()
        data = self._guide(self.other)
        self.assertEqual(data["focus"]["items"], [])
        self.assertEqual(data["subjects"], [])
        self.assertEqual(data["practical"]["items"], [])
        self.assertEqual(data["exam"]["countdown_scope"], "none")
        self.assertIn("考试日期", data["pacing"]["note"])

    def test_guide_rejects_unknown_or_inactive_certificate(self):
        self._login()
        res = self.client.get("/api/training/my/certificate-guide",
                              params={"certificate_id": uuid.uuid4().hex})
        self.assertEqual(res.status_code, 404)
        res = self.client.get("/api/training/my/certificate-guide",
                              params={"certificate_id": self.inactive})
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
