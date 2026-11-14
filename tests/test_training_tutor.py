"""上下文 AI 答疑（考证线）接口测试。

模型传输入口整体替换为桩：覆盖正常回答（引用校验）、调用失败降级、
未配置降级、作答归属与判分前置校验、提问校验与本人隔离。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import settings  # noqa: F401 - 先加载 .env，Store 需要 FIELD_KEY
from webapp import training_tutor
from webapp.storage import Store
from webapp.training_portal import register
from webapp.training_tutor import register as register_tutor

_passwords = PasswordHasher()
STUDENT_COOKIE = "test_training_cookie"
NOW = "2026-10-09T23:30:00"


def _stub_settings(configured):
    return settings.AISettings(enabled=configured, api_key='synthetic-tutor-key' if configured else '',
                               model='stub-model', timeout=5, max_tokens=512)


class TrainingTutorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "tp.db")
        app = FastAPI()
        register(app, lambda: self.store, STUDENT_COOKIE)
        register_tutor(app, lambda: self.store, STUDENT_COOKIE)
        self.client = TestClient(app)
        self._seed()
        self._orig_chat, self._orig_settings = training_tutor.chat_fn, training_tutor.AISettings
        self.addCleanup(lambda: (setattr(training_tutor, "chat_fn", self._orig_chat),
                                 setattr(training_tutor, "AISettings", self._orig_settings)))

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        student_a, student_b, cert = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        kp, attempt = uuid.uuid4().hex, uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_a, "示例大学", "2026001", "李同学", None,
                        _passwords.hash("pass-a"), None, NOW))
            db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                       (student_b, "示例大学", "2026002", "王同学", None,
                        _passwords.hash("pass-b"), None, NOW))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", "[]", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,1,?,?)", (kp, cert, "KP-01", "收入确认", "", NOW, NOW))
            db.execute("INSERT INTO knowledge_point_mark (id,knowledge_point_id,mark_type,level,basis_ref,basis_version,created_by,created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, kp, "high_freq", "high", "考纲第三章", "2026大纲", None, NOW))
            # 学生 A 的一道已判分作答（结果含标准答案）、一道作答中作答；学生 B 无记录
            result = json.dumps({"score": 60.0, "perfect": False, "standard_answer": ["rule-1"],
                                 "missed": [{"name": "收入确认时点", "explanation": "期末应确认"}]})
            db.execute("""INSERT INTO training_self_practice_attempts
                       (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level, year,
                       digest, status, result_json, mode, created_at, scored_at)
                       VALUES (?,?,?,?,?,?, 'normal', 2026, 'd', 'scored', ?, 'new', ?, ?)""",
                       (attempt, student_a, cert, kp, "rule-1", 42,
                        db.field_codec.seal(result, "training_self_practice_attempts",
                                            "result_json", attempt), NOW, NOW))
            db.execute("""INSERT INTO training_self_practice_attempts
                       (id, student_id, certificate_id, knowledge_point_id, rule_id, seed, level, year,
                       digest, status, mode, created_at)
                       VALUES (?,?,?,?,?,?, 'normal', 2026, 'd2', 'open', 'new', ?)""",
                       (uuid.uuid4().hex, student_a, cert, kp, "rule-1", 7, NOW))
        self.student_a, self.student_b, self.cert = student_a, student_b, cert
        self.kp, self.attempt = kp, attempt

    def _login(self, student_no, password):
        res = self.client.post("/api/training/auth/login", json={
            "student_no": student_no, "password": password, "college": "示例大学"})
        self.assertEqual(res.status_code, 200, res.text)

    def _ask(self, **kwargs):
        body = {"certificate_id": self.cert, "question": "这个知识点常考什么？"}
        body.update(kwargs)
        return self.client.post("/api/training/my/tutor", json=body)

    def _stub(self, configured: bool):
        training_tutor.AISettings = SimpleNamespace(from_env=lambda: _stub_settings(configured))

    def test_large_policy_is_not_truncated_or_silently_treated_as_complete(self):
        reference = {'rule_version': '1', 'sources': [{'clause_id': 'oversized', 'title': '测试政策',
                     'clause_label': '第一条', 'content': '完整条款' * 10000}]}
        with self.store.connect() as db, patch('webapp.knowledge_sync.rule_knowledge', return_value=[reference]):
            nodes, _ = training_tutor._build_context(db, self.student_a, self.cert, self.kp, None, [])
        self.assertFalse(any(n['kind'] == 'policy_clause' for n in nodes))
        self.assertTrue(any(n['kind'] == 'context_limit' for n in nodes))
        self.assertLessEqual(len(json.dumps(nodes, ensure_ascii=False).encode('utf-8')), training_tutor.MAX_CONTEXT_BYTES)

    def test_attempt_tutor_history_uses_the_attempt_knowledge_point(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        res = self._ask(attempt_id=self.attempt)
        self.assertEqual(res.status_code, 200, res.text)
        with self.store.connect() as db:
            saved = db.execute('SELECT knowledge_point_id FROM training_tutor_messages WHERE id=?',
                               (res.json()['message']['id'],)).fetchone()
        self.assertEqual(saved['knowledge_point_id'], self.kp)

    def test_normal_answer_with_citation_validation(self):
        self._stub(True)
        training_tutor.chat_fn = lambda cfg, messages: json.dumps({
            "answer": "该知识点常考收入确认时点，注意期末截止。",
            "citations": [f"kp:{self.kp}", "fake:node"]})
        self._login("2026001", "pass-a")
        res = self._ask(knowledge_point_id=self.kp)
        self.assertEqual(res.status_code, 200, res.text)
        msg = res.json()["message"]
        self.assertEqual(msg["model"], "stub-model")
        self.assertEqual(msg["citations"], [f"kp:{self.kp}"])  # 越界引用被过滤
        self.assertEqual(msg["degraded"], "")
        self.assertEqual(msg["basis_summary"], ["KP-01 收入确认"])
        history = self.client.get("/api/training/my/tutor/history").json()["messages"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["question"], "这个知识点常考什么？")
        self.assertEqual(history[0]["basis_summary"], msg["basis_summary"])
        self.assertNotIn("grounding_json", history[0])

    def test_followups_and_restored_history_stay_with_the_student_and_attempt(self):
        self._stub(True)
        payloads = []

        def answer(cfg, messages):
            payloads.append(json.loads(messages[-1]["content"]))
            return json.dumps({"answer": "结合本题解析说明。", "citations": [f"kp:{self.kp}"]})

        training_tutor.chat_fn = answer
        self._login("2026001", "pass-a")
        self.assertEqual(self._ask(knowledge_point_id=self.kp, question="其它知识点对话").status_code, 200)
        self.assertEqual(self._ask(attempt_id=self.attempt, question="本题第一问").status_code, 200)
        self.assertEqual(payloads[-1]["previous_dialogue"], [])
        self.assertEqual(self._ask(attempt_id=self.attempt, question="本题继续追问").status_code, 200)
        self.assertEqual([r["question"] for r in payloads[-1]["previous_dialogue"]], ["本题第一问"])
        history = self.client.get("/api/training/my/tutor/history", params={"attempt_id": self.attempt}).json()["messages"]
        self.assertEqual([r["question"] for r in history], ["本题继续追问", "本题第一问"])
        self._login("2026002", "pass-b")
        self.assertEqual(self.client.get("/api/training/my/tutor/history", params={"attempt_id": self.attempt}).json()["messages"], [])
        self.assertEqual(self._ask(knowledge_point_id=self.kp, question="另一学生提问").status_code, 200)
        self.assertEqual(payloads[-1]["previous_dialogue"], [])

    def test_followup_context_is_bounded_and_does_not_truncate_saved_answers(self):
        self._stub(True)
        payloads = []

        def answer(cfg, messages):
            payloads.append(json.loads(messages[-1]["content"]))
            return json.dumps({"answer": "解题说明" * 800, "citations": [f"kp:{self.kp}"]})

        training_tutor.chat_fn = answer
        self._login("2026001", "pass-a")
        for index in range(5):
            self.assertEqual(self._ask(attempt_id=self.attempt, question=f"追问{index}").status_code, 200)
        previous = payloads[-1]["previous_dialogue"]
        self.assertEqual([row["question"] for row in previous], ["追问1", "追问2", "追问3"])
        self.assertTrue(all(len(row["answer"]) == 1500 for row in previous))
        history = self.client.get("/api/training/my/tutor/history").json()["messages"]
        self.assertTrue(all(len(row["answer"]) == 3200 for row in history))

    def test_degrades_on_model_failure_without_blocking_evidence(self):
        self._stub(True)

        def boom(cfg, messages):
            from src.ai_transport import TransportError
            raise TransportError("http", 500)

        training_tutor.chat_fn = boom
        self._login("2026001", "pass-a")
        res = self._ask(attempt_id=self.attempt)
        self.assertEqual(res.status_code, 200, res.text)
        msg = res.json()["message"]
        self.assertIn("模型调用失败", msg["degraded"])
        self.assertIn("依据要点", msg["answer"])
        self.assertTrue(msg["citations"])
        # 标准答案原样返回，模型未改写
        self.assertEqual(msg["standard_answer"], ["rule-1"])

    def test_degrades_when_model_unconfigured(self):
        self._stub(False)
        self._login("2026001", "pass-a")
        res = self._ask(knowledge_point_id=self.kp)
        self.assertEqual(res.status_code, 200, res.text)
        msg = res.json()["message"]
        self.assertIn("模型未配置", msg["degraded"])
        self.assertEqual(msg["model"], "")

    def _session(self, **overrides):
        body = {'certificate_id': self.cert, 'attempt_id': self.attempt}
        body.update(overrides)
        response = self.client.post('/api/training/my/tutor/sessions', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['session']

    def test_manual_sessions_keep_followups_separate_and_survive_store_reopen(self):
        self._stub(True)
        payloads = []

        def answer(cfg, messages):
            payloads.append(json.loads(messages[-1]['content']))
            return json.dumps({'answer': '解题说明', 'citations': [f'kp:{self.kp}']})

        training_tutor.chat_fn = answer
        self._login('2026001', 'pass-a')
        first = self._session()
        self.assertEqual(first['id'], self._session()['id'])
        self._ask(attempt_id=self.attempt, session_id=first['id'], question='第一段会话的问题')
        second = self._session(new_session=True)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['id'], self._session()['id'])
        self._ask(attempt_id=self.attempt, session_id=second['id'], question='第二段会话的问题')
        self.assertEqual(second['id'], self._session()['id'])
        self.assertEqual(payloads[-1]['previous_dialogue'], [])
        self._ask(attempt_id=self.attempt, session_id=first['id'], question='回到第一段追问')
        self.assertEqual([x['question'] for x in payloads[-1]['previous_dialogue']], ['第一段会话的问题'])
        self.assertEqual(first['id'], self._session()['id'])
        direct = self._session(attempt_id=None, knowledge_point_id=self.kp, new_session=True)
        self.assertEqual(direct['id'], self._session(attempt_id=None, knowledge_point_id=self.kp)['id'])
        reopened = Store(Path(self._tmp.name) / 'tp.db')
        history = training_tutor.tutor_history(reopened, self.student_a, session_id=second['id'])
        self.assertEqual([x['question'] for x in history], ['第二段会话的问题'])
        self.assertEqual(history[0]['session_id'], second['id'])

    def test_different_attempts_have_different_sessions_and_cannot_share_context(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        first = self._session()
        other = uuid.uuid4().hex
        with self.store.connect() as db:
            result = db.field_codec.seal(json.dumps({'score': 80, 'standard_answer': []}),
                                         'training_self_practice_attempts', 'result_json', other)
            db.execute('INSERT INTO training_self_practice_attempts '
                       '(id,student_id,certificate_id,knowledge_point_id,rule_id,seed,level,year,digest,status,result_json,mode,created_at,scored_at) '
                       "VALUES (?,?,?,?,?,1,'normal',2026,'other','scored',?,'new',?,?)",
                       (other, self.student_a, self.cert, self.kp, 'rule-1', result, NOW, NOW))
        second = self._session(attempt_id=other)
        self.assertNotEqual(first['id'], second['id'])
        mixed = self._ask(attempt_id=other, session_id=first['id'])
        self.assertEqual(mixed.status_code, 422)
        self.assertEqual(self._ask(attempt_id=other, session_id=second['id']).status_code, 200)
        self.assertEqual(self.client.get('/api/training/my/tutor/history', params={'session_id': first['id']}).json()['messages'], [])

    def test_sessions_are_private_and_require_an_active_student(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        saved = self._session()
        self._ask(attempt_id=self.attempt, session_id=saved['id'])
        self._login('2026002', 'pass-b')
        self.assertEqual(self.client.get('/api/training/my/tutor/sessions').json()['sessions'], [])
        self.assertEqual(self.client.get('/api/training/my/tutor/history', params={'session_id': saved['id']}).status_code, 404)
        self.assertEqual(self._ask(knowledge_point_id=self.kp, session_id=saved['id']).status_code, 404)
        self.assertEqual(self.client.post('/api/training/my/tutor/sessions', json={
            'certificate_id': self.cert, 'attempt_id': self.attempt}).status_code, 404)
        with self.store.connect() as db:
            db.execute('UPDATE student_info SET active=0 WHERE id=?', (self.student_b,))
        self.assertEqual(self.client.get('/api/training/my/tutor/sessions').status_code, 401)

    def test_legacy_messages_are_grouped_without_losing_history_or_evidence(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        message = self._ask(attempt_id=self.attempt, question='原有历史问题').json()['message']
        with self.store.connect() as db:
            db.execute('UPDATE training_tutor_messages SET session_id=NULL,knowledge_point_id=NULL WHERE id=?', (message['id'],))
            db.execute('DELETE FROM training_tutor_sessions WHERE id=?', (message['session_id'],))
        first = self.client.get('/api/training/my/tutor/sessions').json()['sessions']
        second = self.client.get('/api/training/my/tutor/sessions').json()['sessions']
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['title'], '原有历史问题')
        self.assertEqual(first[0]['knowledge_point_id'], self.kp)
        history = self.client.get('/api/training/my/tutor/history', params={'session_id': first[0]['id']}).json()['messages']
        self.assertEqual(history[0]['answer'], message['answer'])
        self.assertEqual(history[0]['citations'], message['citations'])

    def test_session_creation_rejects_unscored_attempt_and_foreign_certificate(self):
        self._login('2026001', 'pass-a')
        with self.store.connect() as db:
            pending = db.execute("SELECT id FROM training_self_practice_attempts WHERE status='open'").fetchone()['id']
        response = self.client.post('/api/training/my/tutor/sessions', json={'certificate_id': self.cert, 'attempt_id': pending})
        self.assertEqual(response.status_code, 409)
        response = self.client.post('/api/training/my/tutor/sessions', json={'certificate_id': 'missing', 'attempt_id': self.attempt})
        self.assertEqual(response.status_code, 404)

    def test_sessions_and_messages_have_non_overlapping_history_pages(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        saved = self._session()
        for index in range(4):
            self._ask(attempt_id=self.attempt, session_id=saved['id'], question=f'问题{index}')
            another = self._session(new_session=True)
            self._ask(attempt_id=self.attempt, session_id=another['id'], question=f'另一会话{index}')
        page_one = self.client.get('/api/training/my/tutor/sessions', params={'limit': 2}).json()
        page_two = self.client.get('/api/training/my/tutor/sessions', params={'limit': 2, 'offset': 2}).json()
        self.assertTrue(page_one['has_more'])
        self.assertFalse({x['id'] for x in page_one['sessions']} & {x['id'] for x in page_two['sessions']})
        history = self.client.get('/api/training/my/tutor/history', params={'session_id': saved['id'], 'limit': 2, 'offset': 2}).json()['messages']
        self.assertEqual([x['question'] for x in history], ['问题1', '问题0'])

    def test_empty_sessions_are_hidden_until_the_first_saved_message(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        drafts = [self._session(new_session=True) for _ in range(3)]
        listing = self.client.get('/api/training/my/tutor/sessions', params={'limit': 1}).json()
        self.assertEqual(listing, {'sessions': [], 'has_more': False})
        self._ask(attempt_id=self.attempt, session_id=drafts[1]['id'], question='第一条有效对话')
        listing = self.client.get('/api/training/my/tutor/sessions', params={'limit': 1}).json()
        self.assertEqual([x['id'] for x in listing['sessions']], [drafts[1]['id']])
        self.assertEqual(listing['sessions'][0]['message_count'], 1)
        self.assertFalse(listing['has_more'])

    def test_return_to_attempt_uses_latest_dialogue_not_new_drafts_or_restore_time(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        first = self._session()
        self._ask(attempt_id=self.attempt, session_id=first['id'], question='较早对话')
        second = self._session(new_session=True)
        self._ask(attempt_id=self.attempt, session_id=second['id'], question='最近对话')
        for _ in range(3):
            self._session(new_session=True)
        self.assertEqual(self._session()['id'], second['id'])
        url = '/api/training/my/tutor/sessions/' + first['id']
        self.client.patch(url, json={'archived': True})
        self.client.patch(url, json={'archived': False})
        self.assertEqual(self._session()['id'], second['id'])
        self._ask(attempt_id=self.attempt, session_id=first['id'], question='回到旧会话后的最新追问')
        self.assertEqual(self._session()['id'], first['id'])
        self.client.patch(url, json={'archived': True})
        self.assertEqual(self._session()['id'], second['id'])

    def test_return_to_attempt_recovers_legacy_history_before_newer_empty_drafts(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        message = self._ask(attempt_id=self.attempt, question='此前保存的对话').json()['message']
        with self.store.connect() as db:
            db.execute('UPDATE training_tutor_messages SET session_id=NULL,knowledge_point_id=NULL WHERE id=?',
                       (message['id'],))
            db.execute('DELETE FROM training_tutor_sessions WHERE id=?', (message['session_id'],))
        draft = self._session(new_session=True)
        restored = self._session()
        self.assertNotEqual(draft['id'], restored['id'])
        self.assertEqual(restored['message_count'], 1)
        history = self.client.get('/api/training/my/tutor/history', params={'session_id': restored['id']}).json()['messages']
        self.assertEqual(history[0]['question'], '此前保存的对话')

    def test_archive_restore_preserves_history_and_stays_out_of_default_chat(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        saved = self._session()
        original = self._ask(attempt_id=self.attempt, session_id=saved['id']).json()['message']
        url = '/api/training/my/tutor/sessions/' + saved['id']
        archived = self.client.patch(url, json={'archived': True})
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertTrue(archived.json()['session']['archived_at'])
        self.assertEqual(self.client.get('/api/training/my/tutor/sessions').json()['sessions'], [])
        listing = self.client.get('/api/training/my/tutor/sessions', params={'archived': 'true'}).json()['sessions']
        self.assertEqual([x['id'] for x in listing], [saved['id']])
        self.assertEqual(self._ask(attempt_id=self.attempt, session_id=saved['id']).status_code, 409)
        replacement = self._session()
        self.assertNotEqual(replacement['id'], saved['id'])
        self.assertIsNone(replacement['archived_at'])
        reopened = Store(Path(self._tmp.name) / 'tp.db')
        history = training_tutor.tutor_history(reopened, self.student_a, session_id=saved['id'])
        self.assertEqual([x['answer'] for x in history], [original['answer']])
        restored = self.client.patch(url, json={'archived': False})
        self.assertEqual(restored.status_code, 200)
        self.assertIsNone(restored.json()['session']['archived_at'])
        self.assertEqual(self.client.get('/api/training/my/tutor/sessions', params={'archived': 'true'}).json()['sessions'], [])
        self.assertEqual(self._ask(attempt_id=self.attempt, session_id=saved['id']).status_code, 200)

    def test_archive_is_owner_only_and_idempotent(self):
        self._login('2026001', 'pass-a')
        saved = self._session()
        url = '/api/training/my/tutor/sessions/' + saved['id']
        first = self.client.patch(url, json={'archived': True}).json()['session']
        second = self.client.patch(url, json={'archived': True}).json()['session']
        self.assertEqual(first['archived_at'], second['archived_at'])
        self._login('2026002', 'pass-b')
        self.assertEqual(self.client.patch(url, json={'archived': False}).status_code, 404)
        self.assertEqual(self.client.get('/api/training/my/tutor/sessions', params={'archived': 'true'}).json()['sessions'], [])

    def test_archive_during_model_call_does_not_append_a_late_answer(self):
        self._stub(False)
        self._login('2026001', 'pass-a')
        saved = self._session()
        self._ask(attempt_id=self.attempt, session_id=saved['id'], question='已保存内容')
        self._stub(True)

        def answer(cfg, messages):
            training_tutor.archive_tutor_session(self.store, self.student_a, saved['id'], archived=True)
            return json.dumps({'answer': '迟到回答', 'citations': [f'kp:{self.kp}']})

        training_tutor.chat_fn = answer
        response = self._ask(attempt_id=self.attempt, session_id=saved['id'], question='不应追加的问题')
        self.assertEqual(response.status_code, 409)
        history = self.client.get('/api/training/my/tutor/history', params={'session_id': saved['id']}).json()['messages']
        self.assertEqual([x['question'] for x in history], ['已保存内容'])

    def test_attempt_must_be_owned_and_scored(self):
        self._stub(True)
        training_tutor.chat_fn = lambda cfg, messages: json.dumps(
            {"answer": "ok", "citations": [f"attempt:{self.attempt}"]})
        self._login("2026001", "pass-a")
        # 他人/不存在的作答不可见
        other = self.client.post("/api/training/my/tutor", json={
            "certificate_id": self.cert, "question": "q", "attempt_id": "no-such"})
        self.assertEqual(other.status_code, 404)
        # 未判分作答不可答疑（夹具中已播种一道 open 作答）
        with self.store.connect() as db:
            open_id = db.execute(
                "SELECT id FROM training_self_practice_attempts WHERE status='open'").fetchone()["id"]
        open_res = self.client.post("/api/training/my/tutor", json={
            "certificate_id": self.cert, "question": "q", "attempt_id": open_id})
        self.assertEqual(open_res.status_code, 409)
        self.assertIn("判分", open_res.json()["detail"])

    def test_question_validation_and_history_isolation(self):
        self._stub(True)
        training_tutor.chat_fn = lambda cfg, messages: json.dumps(
            {"answer": "ok", "citations": [f"kp:{self.kp}"]})
        self._login("2026001", "pass-a")
        self.assertEqual(self._ask(question="   ").status_code, 422)
        self.assertEqual(self._ask(question="长" * 501).status_code, 422)
        self._ask(knowledge_point_id=self.kp)
        # 本人隔离：学生 B 看不到学生 A 的答疑历史
        self._login("2026002", "pass-b")
        other_history = self.client.get("/api/training/my/tutor/history").json()["messages"]
        self.assertEqual(other_history, [])


if __name__ == "__main__":
    unittest.main()
