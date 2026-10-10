"""学习图谱关系（FR-K03/K04 待完善项）接口测试。

覆盖：三类关系建立与依据强制、跨证书/自环/重复（含对称反向）拒绝、
前置依赖环检测、教师端列表（出入边）、学生视图聚合、删除。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_content import register as register_content
from webapp.training_graph import register as register_graph
from webapp.training_portal import register

_passwords = PasswordHasher()
STAFF_COOKIE = "taxpearls_staff_session"
NOW = "2026-10-09T23:00:00"


class TrainingGraphTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register(app, lambda: self.store, "test_training_cookie")
        register_content(app, lambda: self.store)
        register_graph(app, lambda: self.store)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id, cert, cert2 = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        kpa, kpb, kpc, kpx = (uuid.uuid4().hex for _ in range(4))
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", "[]", "", NOW, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert2, "other", "其他证书", "", "[]", "", NOW, NOW))
            for kp_id, code in ((kpa, "KP-A"), (kpb, "KP-B"), (kpc, "KP-C")):
                db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,1,?,?)", (kp_id, cert, code, f"知识点{code}", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,1,?,?)", (kpx, cert2, "KP-X", "异证知识点", "", NOW, NOW))
        self.staff_id, self.cert, self.cert2 = staff_id, cert, cert2
        self.kpa, self.kpb, self.kpc, self.kpx = kpa, kpb, kpc, kpx

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login", json={
            "username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _create(self, from_kp, to_kp, relation_type="prerequisite", basis="考纲第二章"):
        return self.client.post(f"/api/training/staff/knowledge-points/{from_kp}/relations", json={
            "to_kp_id": to_kp, "relation_type": relation_type, "basis_ref": basis})

    def test_relation_requires_basis_and_same_certificate(self):
        self._staff_login()
        no_basis = self.client.post(f"/api/training/staff/knowledge-points/{self.kpa}/relations", json={
            "to_kp_id": self.kpb, "relation_type": "prerequisite", "basis_ref": ""})
        self.assertEqual(no_basis.status_code, 422)
        cross = self._create(self.kpa, self.kpx)
        self.assertEqual(cross.status_code, 422)
        self.assertIn("跨证书", cross.json()["detail"])
        selfrel = self._create(self.kpa, self.kpa)
        self.assertEqual(selfrel.status_code, 422)
        bad_type = self._create(self.kpa, self.kpb, relation_type="friend")
        self.assertEqual(bad_type.status_code, 422)

    def test_duplicate_and_symmetric_duplicate_rejected(self):
        self._staff_login()
        self.assertEqual(self._create(self.kpa, self.kpb, "confusable").status_code, 200)
        dup = self._create(self.kpa, self.kpb, "confusable")
        self.assertEqual(dup.status_code, 409)
        sym = self._create(self.kpb, self.kpa, "confusable")
        self.assertEqual(sym.status_code, 409)
        self.assertIn("对称", sym.json()["detail"])
        # 前置的反向语义不同：A 依赖 B 后，B 依赖 A 形成环（见下一个用例），此处先建反向概念链
        self.assertEqual(self._create(self.kpb, self.kpc, "concept").status_code, 200)

    def test_prerequisite_cycle_rejected(self):
        self._staff_login()
        self.assertEqual(self._create(self.kpa, self.kpb).status_code, 200)   # A 依赖 B
        self.assertEqual(self._create(self.kpb, self.kpc).status_code, 200)   # B 依赖 C
        cycle = self._create(self.kpc, self.kpa)                              # C 依赖 A → 环
        self.assertEqual(cycle.status_code, 409)
        self.assertIn("依赖环", cycle.json()["detail"])
        # 反向直连同样构成环：B 依赖 A（A 依赖 B 已存在）
        rev = self._create(self.kpb, self.kpa)
        self.assertEqual(rev.status_code, 409)

    def test_list_both_directions_and_student_view(self):
        self._staff_login()
        self.assertEqual(self._create(self.kpa, self.kpb).status_code, 200)
        listing = self.client.get(f"/api/training/staff/knowledge-points/{self.kpa}/relations").json()
        self.assertEqual(len(listing["relations"]), 1)
        self.assertEqual(listing["relations"][0]["to_kp"]["code"], "KP-B")
        self.assertEqual(listing["relations"][0]["relation_type_label"], "前置知识")
        # 学生视角：KP-A 的前置是 KP-B；KP-B 的被依赖关系在学生载荷中对称聚合为前置
        kps = self.client.get(f"/api/training/staff/certificates/{self.cert}/knowledge-points").json()
        by_code = {k["code"]: k for k in kps["knowledge_points"]}
        self.assertEqual(by_code["KP-A"]["relations"]["prerequisite"][0]["code"], "KP-B")
        self.assertEqual(by_code["KP-B"]["relations"]["prerequisite"][0]["code"], "KP-A")

    def test_delete_relation(self):
        self._staff_login()
        created = self._create(self.kpa, self.kpb, "concept").json()["relation"]
        res = self.client.delete(f"/api/training/staff/relations/{created['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        gone = self.client.get(f"/api/training/staff/knowledge-points/{self.kpa}/relations").json()
        self.assertEqual(gone["relations"], [])
        missing = self.client.delete(f"/api/training/staff/relations/{created['id']}")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
