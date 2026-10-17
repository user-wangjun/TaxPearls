"""判题链路增强测试（FR-J05 队列 + J06 查重 + J07 质量分 + J08 评分对接）。

覆盖：队列入队/认领/重试状态机（同步 run_pending，不起后台线程）、
查重归一化与同题跨学生比对（同班/跨班标记、阈值）、质量分扣分项、
判题后自动落评分记录、教师调分留痕与生效分、教师端 API 鉴权与院校隔离。
LoginGuard 惯例：setUp 换全新实例、tearDown 还原（CI 同进程累计限流防护）。
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
from webapp import code_judge_queue, code_plagiarism, code_quality
from webapp.login_guard import LoginGuard
from webapp.storage import Store
from webapp.training_portal import register

_passwords = PasswordHasher()
NOW = "2026-10-17T20:00:00"
COLLEGE = "示例大学"
STUDENT_COOKIE = "test_judge_pipeline_student"
STAFF_COOKIE = "test_judge_pipeline_staff"


class CodeJudgePipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "jp.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        # CI 惯例：换全新 LoginGuard，避免跨测试文件累计失败登录触发 429
        import webapp.training_portal as tp
        self._orig_guards = [(tp, "login_guard", tp.login_guard),
                             (tp, "staff_login_guard", tp.staff_login_guard)]
        tp.login_guard = LoginGuard()
        tp.staff_login_guard = LoginGuard()
        self._seed()

    def tearDown(self):
        for module, name, obj in self._orig_guards:
            setattr(module, name, obj)
        self._tmp.cleanup()

    def _seed(self):
        with self.store.connect() as db:
            self.admin_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (self.admin_id, "admin1", _passwords.hash("staff-pass"), "管理员",
                 COLLEGE, None, None, None, "admin", NOW))
            db.execute(
                "INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, "other-admin", _passwords.hash("staff-pass"), "他校管理",
                 "其他大学", None, None, None, "admin", NOW))
            for no, name, cls in (("2026001", "学生甲", "会计2601"),
                                  ("2026002", "学生乙", "会计2601"),
                                  ("2026003", "学生丙", "会计2602")):
                db.execute(
                    "INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                    (uuid.uuid4().hex, COLLEGE, no, name, cls,
                     _passwords.hash("Stu2026x!"), None, NOW))
            db.execute(
                "INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, "其他大学", "2026001", "外校生", "外班",
                 _passwords.hash("Stu2026x!"), None, NOW))

    def _student_login(self, no="2026001"):
        res = self.client.post("/api/training/auth/login",
                               json={"student_no": no, "password": "Stu2026x!",
                                     "college": COLLEGE})
        self.assertEqual(res.status_code, 200, res.text)

    def _staff_login(self, username="admin1"):
        res = self.client.post("/api/training/staff/auth/login",
                               json={"username": username, "password": "staff-pass"})
        self.assertEqual(res.status_code, 200, res.text)

    def _create_problem(self, cases=None):
        self._staff_login()
        res = self.client.post("/api/training/staff/code-problems", json={
            "college": COLLEGE, "title": "两数之和", "statement": "读入 a b，输出 a+b",
            "time_limit_ms": 5000,
            "cases": cases or [{"input_text": "1 2\n", "expected_output": "3\n"}]})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["problem"]["id"]

    def _submit(self, pid, code, student_no="2026001"):
        self._student_login(student_no)
        res = self.client.post(f"/api/training/my/code/{pid}/submit",
                               json={"code_text": code})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["result"]

    # ---- J05 队列 ----

    def test_queue_enqueue_claim_retry_state_machine(self):
        pid = self._create_problem()
        self._student_login()
        res = self.client.post(f"/api/training/my/code/{pid}/submit",
                               json={"code_text": "print(input() and 'x')"})
        self.assertEqual(res.status_code, 200, res.text)
        with self.store.connect() as db:
            sid = db.execute("SELECT id FROM code_submission ORDER BY submitted_at DESC"
                             ).fetchone()["id"]
        task_id = code_judge_queue.enqueue(self.store, sid)
        code_judge_queue.enqueue(self.store, sid)  # 幂等
        items = code_judge_queue.run_pending(self.store)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "done")
        tasks = code_judge_queue.list_tasks(self.store)
        self.assertEqual(tasks[0]["state"], "done")
        self.assertIsNotNone(tasks[0]["judge_status"])

    def test_queue_failed_task_retries_then_manual_retry(self):
        from unittest.mock import patch
        pid = self._create_problem()
        result = self._submit(pid, "print('ok')\n")  # 正常同步判题一次
        sid = result["submission_id"]
        task_id = code_judge_queue.enqueue(self.store, sid)
        with self.store.connect() as db:
            db.execute("UPDATE code_judge_task SET max_attempts=2 WHERE id=?", (task_id,))
            # 模拟"提交未判"的排队场景
            db.execute("UPDATE code_submission SET status='pending' WHERE id=?", (sid,))
        with patch("webapp.code_sandbox.run_case",
                   side_effect=RuntimeError("sandbox boom")):
            items = code_judge_queue.run_pending(self.store)
            self.assertEqual(items[0]["state"], "retry_or_failed")
            state1 = code_judge_queue.list_tasks(self.store)[0]["state"]
            self.assertEqual(state1, "queued")  # attempts<max → 退避重试
            # 拨快退避时钟（next_run_at 到期）再跑第二次，验证次数耗尽转 failed
            with self.store.connect() as db:
                db.execute("UPDATE code_judge_task SET next_run_at=? WHERE id=?",
                           ("2020-01-01T00:00:00", task_id))
            code_judge_queue.run_pending(self.store)
            state2 = code_judge_queue.list_tasks(self.store)[0]["state"]
            self.assertEqual(state2, "failed")  # 次数耗尽
            self.assertIn("sandbox boom", code_judge_queue.list_tasks(self.store)[0]["last_error"])
        # 教师手动重试置回 queued，故障解除后判题完成
        task = code_judge_queue.retry_task(self.store, task_id)
        self.assertEqual(task["state"], "queued")
        with self.store.connect() as db:
            db.execute("UPDATE code_submission SET code_text=? WHERE id=?",
                       (db.field_codec.seal("a, b = map(int, input().split())\nprint(a + b)\n",
                                            "code_submission", "code_text", sid), sid))
        items = code_judge_queue.run_pending(self.store)
        self.assertEqual(items[0]["state"], "done")
        tasks = code_judge_queue.list_tasks(self.store)
        self.assertEqual(tasks[0]["state"], "done")
        self.assertEqual(tasks[0]["judge_status"], "accepted")

    # ---- J06 查重 ----

    def test_plagiarism_similarity_and_flags(self):
        sim = code_plagiarism.similarity(
            "a, b = map(int, input().split())\nprint(a + b)  # 注释",
            "a,b=map(int,input().split())\nprint(a+b)\n")
        self.assertGreater(sim, 0.95)
        self.assertLess(code_plagiarism.similarity("print(1)", "print(2)\nprint(3)"), 0.6)

        pid = self._create_problem()
        base = "a, b = map(int, input().split())\nprint(a + b)\n"
        variant = "x, y = map(int, input().split())\nprint(x + y)\n"
        different = "print(sum(int(v) for v in input().split()))\n"
        self._submit(pid, base, "2026001")
        self._submit(pid, variant, "2026002")     # 同班，结构相同（仅改名，v1 会命中）
        self._submit(pid, different, "2026003")   # 写法不同
        scan = code_plagiarism.scan_problem(self.store, pid, threshold=0.85)
        self.assertEqual(scan["pairs_checked"], 3)
        self.assertGreaterEqual(scan["flagged"], 1)
        flags = code_plagiarism.list_flags(self.store, pid)
        pair = {(f["student_no_a"], f["student_no_b"]) for f in flags}
        self.assertIn(("2026001", "2026002"), pair)
        for f in flags:
            if {f["student_no_a"], f["student_no_b"]} == {"2026001", "2026002"}:
                self.assertEqual(f["same_class"], 1)
        self.assertNotIn(("2026001", "2026003"), pair)  # 写法不同不落标记

    # ---- J07 质量分 ----

    def test_quality_assess_and_recorded_on_judge(self):
        good = code_quality.assess("def add(a, b):\n    return a + b\n\nprint(add(1, 2))\n")
        self.assertEqual(good["score"], 100.0)
        bad_code = (
            "def BadFunc( x ):\n"                       # 命名不规范
            "    try:\n"
            "        if x and x or x:\n"
            "            return x\n"
            "    except:\n"                              # 裸 except
            "        pass\n"
            + "    x = x\n" * 45 +                       # 函数超 40 行
            "    return x\n"
            "print('this line is intentionally far longer than one hundred characters to trigger the long line check')\n")
        bad = code_quality.assess(bad_code)
        self.assertLess(bad["score"], 100.0)
        names = {c["name"] for c in bad["checks"]}
        self.assertIn("func_naming", names)
        self.assertIn("bare_except", names)
        self.assertIn("func_length", names)
        self.assertIn("long_line", names)
        syntax = code_quality.assess("def broken(:\n")
        self.assertEqual(syntax["score"], 0.0)

        pid = self._create_problem()
        result = self._submit(pid, "a, b = map(int, input().split())\nprint(a + b)\n")
        self.assertIn("quality", result)
        self.assertIsNotNone(result["quality"])
        self.assertGreaterEqual(result["quality"]["score"], 80.0)

    # ---- J08 评分对接 ----

    def test_score_record_auto_and_teacher_adjust(self):
        pid = self._create_problem()
        result = self._submit(pid, "a, b = map(int, input().split())\nprint(a + b)\n")
        sid = result["submission_id"]
        from webapp.training_portal import get_code_score, adjust_code_score
        score = get_code_score(self.store, sid)
        self.assertEqual(score["auto_score"], 100.0)
        self.assertEqual(score["effective_score"], 100.0)
        self.assertEqual(score["adjust_log"], [])

        adjusted = adjust_code_score(self.store, sid, 85.0, "命名不规范，酌情扣分", self.admin_id)
        self.assertEqual(adjusted["effective_score"], 85.0)
        self.assertEqual(len(adjusted["adjust_log"]), 1)
        self.assertEqual(adjusted["adjust_log"][0]["from"], 100.0)
        # 再调一次：留痕累计，from = 上次生效分
        adjusted2 = adjust_code_score(self.store, sid, 90.0, "复核后回调", self.admin_id)
        self.assertEqual(adjusted2["adjust_log"][1]["from"], 85.0)
        self.assertEqual(adjusted2["effective_score"], 90.0)

        # 学生视角：生效分出现在提交列表
        self._student_login()
        res = self.client.get("/api/training/my/code/submissions")
        subs = res.json()["submissions"]
        self.assertEqual(subs[0]["effective_score"], 90.0)

    def test_adjust_requires_reason_and_range(self):
        from webapp.training_portal import TrainingPortalError, adjust_code_score
        pid = self._create_problem()
        result = self._submit(pid, "print(input())\n")
        with self.assertRaises(TrainingPortalError):
            adjust_code_score(self.store, result["submission_id"], 50.0, "  ", "op-1")
        with self.assertRaises(TrainingPortalError):
            adjust_code_score(self.store, result["submission_id"], 150.0, "超范围", "op-1")

    # ---- 教师端 API：鉴权 + 院校隔离 ----

    def test_staff_endpoints_scope_and_auth(self):
        pid = self._create_problem()
        result = self._submit(pid, "a, b = map(int, input().split())\nprint(a + b)\n")
        sid = result["submission_id"]
        # 未登录 401
        client2 = TestClient(app2 := FastAPI())
        register(app2, lambda: self.store, STUDENT_COOKIE)
        self.assertEqual(client2.get(
            f"/api/training/staff/code-submissions/{sid}/score").status_code, 401)
        # 本校教师可见
        self._staff_login()
        res = self.client.get(f"/api/training/staff/code-submissions/{sid}/score")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["score"]["auto_score"], 100.0)
        res = self.client.get(f"/api/training/staff/code-submissions/{sid}/quality")
        self.assertEqual(res.status_code, 200, res.text)
        # 队列视图（本校）
        code_judge_queue.enqueue(self.store, sid)
        res = self.client.get("/api/training/staff/code-judge/tasks")
        self.assertEqual(res.status_code, 200, res.text)
        for t in res.json()["tasks"]:
            self.assertTrue(t["submission_id"])
        # 调分接口
        res = self.client.put(f"/api/training/staff/code-submissions/{sid}/score",
                              json={"score": 88.0, "reason": "样例核对后微调"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["score"]["effective_score"], 88.0)
        # 查重扫描 + 列表
        res = self.client.post(f"/api/training/staff/code-problems/{pid}/plagiarism-scan",
                               json={"threshold": 0.9})
        self.assertEqual(res.status_code, 200, res.text)
        res = self.client.get(f"/api/training/staff/code-problems/{pid}/plagiarism")
        self.assertEqual(res.status_code, 200, res.text)
        # 他校教师不可见本校提交
        self._staff_login("other-admin")
        res = self.client.get(f"/api/training/staff/code-submissions/{sid}/score")
        self.assertEqual(res.status_code, 404, res.text)


if __name__ == "__main__":
    unittest.main()
