"""考纲年度升版（第四条就绪）接口测试。

覆盖：发起升版（现行树标记 pending、from/to 版本口径、同证书仅一张核对中
升版单）、核对期内新增知识点对学生隐藏（added+active=0）、逐条核对状态流转
（confirmed/updated/removed，非法状态拒绝）、待核对未清零禁止激活、激活切换
（版本统一改写、removed 停用留痕、added 上线、知识点 id 不变）、升版历史。
"""
from __future__ import annotations

import auth_support

import tempfile
import unittest
import uuid
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp.storage import Store
from webapp.training_outline import register as register_outline
from webapp.training_portal import register as register_portal

_passwords = PasswordHasher()
STAFF_COOKIE = "taxpearls_staff_session"
NOW = "2026-10-18T10:00:00"


class TrainingOutlineUpgradeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register_portal(app, lambda: self.store, "test_item4_student")
        register_outline(app, lambda: self.store)
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        staff_id, cert = uuid.uuid4().hex, uuid.uuid4().hex
        kps = {}
        with self.store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                        "示例大学", None, None, None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", NOW, NOW))
            for code, name in (("KP-01", "会计基础"), ("KP-02", "收入确认"), ("KP-03", "财务报表")):
                kp_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO knowledge_point (id,certificate_id,code,name,subject,"
                    "outline_version,active,review_status,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,'2026',1,'',?,?)",
                    (kp_id, cert, code, name, "初级会计实务", NOW, NOW))
                kps[code] = kp_id
        self.staff_id, self.cert, self.kps = staff_id, cert, kps

    def _staff_login(self):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": "admin1", "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)
        auth_support.content_session(self.store, self.client)

    def _start(self, to_version="2027", note=""):
        return self.client.post(
            f"/api/training/staff/certificates/{self.cert}/outline-upgrade/start",
            json={"to_version": to_version, "note": note})

    def _patch_kp(self, kp_id, **fields):
        return self.client.patch(f"/api/training/staff/knowledge-points/{kp_id}", json=fields)

    def _status(self):
        return self.client.get(
            f"/api/training/staff/certificates/{self.cert}/outline-upgrade").json()

    def _kp_row(self, kp_id):
        with self.store.connect() as db:
            return dict(db.execute("SELECT * FROM knowledge_point WHERE id=?", (kp_id,)).fetchone())

    def test_start_marks_tree_pending_and_blocks_duplicate(self):
        self._staff_login()
        # 现行版本与目标版本相同拒绝；空目标拒绝
        self.assertEqual(self._start(to_version="2026").status_code, 422)
        self.assertEqual(self._start(to_version="").status_code, 422)
        res = self._start(note="全国会计考办 2027 年度考试大纲")
        self.assertEqual(res.status_code, 200, res.text)
        upgrade = res.json()["upgrade"]
        self.assertEqual(upgrade["status"], "working")
        self.assertEqual(upgrade["from_version"], "2026")
        self.assertEqual(upgrade["to_version"], "2027")
        self.assertEqual(upgrade["review_counts"]["pending"], 3)
        for kp_id in self.kps.values():
            self.assertEqual(self._kp_row(kp_id)["review_status"], "pending")
        # 同证书同时仅一张核对中升版单
        dup = self._start()
        self.assertEqual(dup.status_code, 409)
        status = self._status()
        self.assertEqual(status["current"]["id"], upgrade["id"])
        self.assertEqual(status["history"], [])
        self.assertEqual(status["dominant_version"], "2026")

    def test_review_transitions_and_hidden_added_kp(self):
        self._staff_login()
        self._start()
        # 核对期内新增知识点：added 且对学生隐藏，版本默认目标年度
        added = self.client.post(
            f"/api/training/staff/certificates/{self.cert}/knowledge-points",
            json={"code": "KP-04", "name": "新版新增考点", "subject": "初级会计实务"})
        self.assertEqual(added.status_code, 200, added.text)
        added_row = self._kp_row(added.json()["knowledge_point"]["id"])
        self.assertEqual(added_row["review_status"], "added")
        self.assertEqual(added_row["active"], 0)
        self.assertEqual(added_row["outline_version"], "2027")
        # 非法核对状态拒绝
        self.assertEqual(self._patch_kp(self.kps["KP-01"], review_status="ok").status_code, 422)
        # 逐条核对：沿用 / 修改 / 删除
        self.assertEqual(self._patch_kp(self.kps["KP-01"], review_status="confirmed").status_code, 200)
        self.assertEqual(self._patch_kp(self.kps["KP-02"], review_status="updated",
                                        name="收入确认（新纲）").status_code, 200)
        self.assertEqual(self._patch_kp(self.kps["KP-03"], review_status="removed").status_code, 200)
        status = self._status()["review_counts"]
        self.assertEqual(status, {"pending": 0, "confirmed": 1, "updated": 1,
                                  "added": 1, "removed": 1})

    def test_activate_blocked_until_all_reviewed(self):
        self._staff_login()
        self._start()
        # 有 pending 时激活被拒
        blocked = self.client.post(
            f"/api/training/staff/certificates/{self.cert}/outline-upgrade/activate")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("待核对", blocked.json()["detail"])
        # 核对清零但全部删除时拒绝激活
        for kp_id in self.kps.values():
            self._patch_kp(kp_id, review_status="removed")
        all_removed = self.client.post(
            f"/api/training/staff/certificates/{self.cert}/outline-upgrade/activate")
        self.assertEqual(all_removed.status_code, 422)
        self.assertIn("没有可激活", all_removed.json()["detail"])

    def test_activate_switches_versions_and_keeps_ids(self):
        self._staff_login()
        self._start()
        added = self.client.post(
            f"/api/training/staff/certificates/{self.cert}/knowledge-points",
            json={"code": "KP-04", "name": "新版新增考点"}).json()["knowledge_point"]["id"]
        self._patch_kp(self.kps["KP-01"], review_status="confirmed")
        self._patch_kp(self.kps["KP-02"], review_status="updated", name="收入确认（新纲）")
        self._patch_kp(self.kps["KP-03"], review_status="removed")
        res = self.client.post(
            f"/api/training/staff/certificates/{self.cert}/outline-upgrade/activate")
        self.assertEqual(res.status_code, 200, res.text)
        result = res.json()["upgrade"]
        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["activated"], 3)   # confirmed+updated+added
        self.assertEqual(result["removed"], 1)
        # 知识点 id 不变（学生作答与覆盖统计天然连续）
        kept = self._kp_row(self.kps["KP-01"])
        self.assertEqual(kept["review_status"], "")
        self.assertEqual(kept["outline_version"], "2027")
        self.assertEqual(kept["active"], 1)
        updated = self._kp_row(self.kps["KP-02"])
        self.assertEqual(updated["outline_version"], "2027")
        self.assertEqual(updated["name"], "收入确认（新纲）")
        removed = self._kp_row(self.kps["KP-03"])
        self.assertEqual(removed["active"], 0)     # 停用留痕，行保留
        self.assertEqual(removed["outline_version"], "2026")
        # 本轮核对状态全部清零，不把 removed 残留带进下一轮进度
        self.assertEqual(removed["review_status"], "")
        self.assertEqual(result["review_counts"],
                         {k: 0 for k in ("pending", "confirmed", "updated", "added", "removed")})
        live = self._kp_row(added)
        self.assertEqual(live["active"], 1)        # added 激活时上线
        self.assertEqual(live["review_status"], "")
        # 激活后可再次发起升版，from_version 取新主导版本
        again = self._start(to_version="2028")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["upgrade"]["from_version"], "2027")
        self.assertEqual(again.json()["upgrade"]["review_counts"]["pending"], 3)  # 仅启用中的 3 条
        self.assertEqual(again.json()["upgrade"]["review_counts"]["removed"], 0)
        status = self._status()
        self.assertEqual(len(status["history"]), 1)
        self.assertEqual(status["history"][0]["to_version"], "2027")

    def test_start_requires_existing_knowledge_and_versions(self):
        self._staff_login()
        # 无知识点证书直接拒绝
        empty_cert = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (empty_cert, "empty-x", "空证书", "", "[]", "", NOW, NOW))
        res = self.client.post(
            f"/api/training/staff/certificates/{empty_cert}/outline-upgrade/start",
            json={"to_version": "2027"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("知识点", res.json()["detail"])
        # 知识点未登记考纲版本时拒绝发起
        bare_cert = uuid.uuid4().hex
        bare_kp = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (bare_cert, "bare-x", "无版本证书", "", "[]", "", NOW, NOW))
            db.execute(
                "INSERT INTO knowledge_point (id,certificate_id,code,name,subject,"
                "outline_version,active,review_status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,'',1,'',?,?)",
                (bare_kp, bare_cert, "KP-B", "未登记版本", "", NOW, NOW))
        res = self.client.post(
            f"/api/training/staff/certificates/{bare_cert}/outline-upgrade/start",
            json={"to_version": "2027"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("outline_version", res.json()["detail"])
        # 不存在的证书 404
        missing = self.client.post(
            f"/api/training/staff/certificates/{uuid.uuid4().hex}/outline-upgrade/start",
            json={"to_version": "2027"})
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
