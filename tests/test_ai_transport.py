"""Failover checks use synthetic keys and a local HTTP server only."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from fastapi import HTTPException

from src import ai_transport
from src.ai_extraction import ExtractionError, call_model
from src.ai_transport import TransportError, chat_content
from src.settings import AISettings
from webapp.knowledge import ask_graph


SETTINGS = AISettings(enabled=True, api_key="synthetic-main",
                      backup_api_keys=("synthetic-backup-1", "synthetic-backup-2"))


def envelope(content="{}", finish="stop"):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode()


def rejection(code):
    return HTTPError("https://provider.example", code, "synthetic-private-error", {}, BytesIO(b"private body"))


@contextmanager
def provider(statuses):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers["Authorization"].removeprefix("Bearer ")
            observed.append((key, self.path, payload))
            status = statuses.get(key, 200)
            if status == 200:
                # Exercise the two production consumers through the same key pool.
                content = {"company": {}, "rows": [], "warnings": []} if payload["temperature"] == 0 else {
                    "answer": "请核对原件。", "citations": ["test-node"]}
                data = envelope(json.dumps(content))
            else:
                data = b'{"error":"synthetic-private-error"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class AIKeyFailoverTests(unittest.TestCase):
    def setUp(self):
        state = patch("src.ai_transport._key_failures", {})
        state.start()
        self.addCleanup(state.stop)

    def test_env_order_deduplication_validation_and_secret_redaction(self):
        with patch.dict("os.environ", {
            "TAXPEARLS_AI_ENABLED": "1", "TAXPEARLS_AI_API_KEY": "synthetic-main",
            "TAXPEARLS_AI_BACKUP_API_KEYS": " synthetic-backup-1, ,synthetic-main,synthetic-backup-1,synthetic-backup-2,",
        }, clear=True):
            settings = AISettings.from_env()
        self.assertEqual(settings.api_keys, SETTINGS.api_keys)
        self.assertFalse(settings.problem())
        self.assertEqual(settings.public_status()["backup_key_count"], 2)
        for key in settings.api_keys:
            self.assertNotIn(key, repr(settings))
            self.assertNotIn(key, json.dumps(settings.public_status()))
        self.assertFalse(replace(settings, backup_api_keys=tuple(f"synthetic-{i}" for i in range(9))).problem())
        too_many = replace(settings, backup_api_keys=tuple(f"synthetic-{i}" for i in range(10)))
        self.assertIn("10", too_many.problem())
        for bad in ("synthetic key", "synthetic\nkey", "中文key", "synthetic\x7fkey"):
            invalid = replace(settings, backup_api_keys=(bad,))
            self.assertTrue(invalid.problem())
            self.assertNotIn(bad, invalid.problem())
        self.assertTrue(replace(settings, api_key="").problem())

    def test_real_http_extraction_then_graph_share_cooldown_and_never_change_provider_or_model(self):
        with provider({"synthetic-main": 402, "synthetic-backup-1": 401}) as (base, observed):
            settings = replace(SETTINGS, base_url=base)
            with self.assertLogs("src.ai_transport", level="WARNING") as captured:
                self.assertEqual(call_model(settings, [], 5).rows, [])
            graph = {"nodes": [{"id": "test-node", "label": "test"}], "edges": []}
            with patch("webapp.knowledge.AISettings.from_env", return_value=settings):
                result = ask_graph(graph, "test-node", "解释")
            self.assertEqual(result["answer"], "请核对原件。")
            self.assertEqual([item[0] for item in observed], [*settings.api_keys, "synthetic-backup-2"])
            for _, path, payload in observed:
                self.assertEqual(path, "/v1/chat/completions")
                self.assertEqual(payload["model"], settings.effective_model)
                self.assertFalse(payload["stream"])
                self.assertEqual(payload["response_format"], {"type": "json_object"})
            log = " ".join(captured.output)
            self.assertIn("HTTP 402", log)
            self.assertIn("HTTP 401", log)
            for secret in (*settings.api_keys, "synthetic-private-error"):
                self.assertNotIn(secret, log)

    def test_primary_success_does_not_use_backups(self):
        opener = Mock()
        opener.open.return_value = BytesIO(envelope())
        with patch("src.ai_transport.build_opener", return_value=opener):
            self.assertEqual(chat_content(SETTINGS, [], 5), "{}")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer synthetic-main")

    def test_all_rejected_keys_stop_and_subsequent_request_makes_no_http_call(self):
        opener = Mock()
        opener.open.side_effect = [rejection(402), rejection(401), rejection(402)]
        with patch("src.ai_transport.build_opener", return_value=opener), self.assertLogs("src.ai_transport"):
            with self.assertRaises(ExtractionError) as caught:
                call_model(SETTINGS, [], 5)
            self.assertEqual(caught.exception.code, "http")
            self.assertIn("均暂不可用", str(caught.exception))
            self.assertEqual(opener.open.call_count, 3)
            with patch("webapp.knowledge.AISettings.from_env", return_value=SETTINGS):
                graph = {"nodes": [{"id": "test-node", "label": "test"}], "edges": []}
                with self.assertRaises(HTTPException) as failed:
                    ask_graph(graph, "test-node", "解释")
            self.assertIn("余额", failed.exception.detail)
            self.assertEqual(opener.open.call_count, 3)
            for key in SETTINGS.api_keys:
                self.assertNotIn(key, str(caught.exception))
                self.assertNotIn(key, failed.exception.detail)

    def test_expired_cooldown_returns_to_primary_and_new_key_is_immediately_usable(self):
        opener = Mock()
        opener.open.side_effect = [rejection(402), BytesIO(envelope()), BytesIO(envelope()), BytesIO(envelope())]
        now = [100.0]
        with patch("src.ai_transport.time.monotonic", side_effect=lambda: now[0]), \
                patch("src.ai_transport.build_opener", return_value=opener), self.assertLogs("src.ai_transport"):
            chat_content(SETTINGS, [], 5)
            # Recreating settings, as request handlers do, must not reset cooldown.
            chat_content(replace(SETTINGS, api_key="synthetic-new"), [], 5)
            now[0] = 401.0
            chat_content(replace(SETTINGS), [], 5)
        self.assertEqual([call.args[0].get_header("Authorization") for call in opener.open.call_args_list],
                         ["Bearer synthetic-main", "Bearer synthetic-backup-1", "Bearer synthetic-new", "Bearer synthetic-main"])

    def test_same_key_at_another_provider_does_not_share_failure_state(self):
        opener = Mock()
        opener.open.side_effect = [rejection(402), BytesIO(envelope()), BytesIO(envelope())]
        with patch("src.ai_transport.build_opener", return_value=opener), self.assertLogs("src.ai_transport"):
            chat_content(SETTINGS, [], 5)
            chat_content(replace(SETTINGS, base_url="https://another.example/v1"), [], 5)
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer synthetic-main")

    def test_single_key_rejection_retains_no_retry_behavior(self):
        settings = replace(SETTINGS, backup_api_keys=())
        opener = Mock()
        def reject(*_args, **_kwargs):
            raise rejection(402)

        opener.open.side_effect = reject
        with patch("src.ai_transport.build_opener", return_value=opener):
            for _ in range(2):
                with self.assertRaises(TransportError) as caught:
                    chat_content(settings, [], 5)
                self.assertEqual((caught.exception.kind, caught.exception.status), ("http", 402))
        self.assertEqual(opener.open.call_count, 2)

    def test_no_failover_on_ambiguous_errors_or_invalid_output(self):
        errors = [rejection(code) for code in (400, 403, 404, 422, 429, 500, 503, 302)]
        errors += [TimeoutError("synthetic-main"), URLError("synthetic-main")]
        responses = [b"bad json", envelope("", "stop"), envelope("{}", "length"), b"x" * (2 * 1024 * 1024 + 1)]
        for value in [*errors, *responses]:
            opener = Mock()
            if isinstance(value, Exception):
                opener.open.side_effect = value
            else:
                opener.open.return_value = BytesIO(value)
            with self.subTest(value=type(value).__name__), patch("src.ai_transport.build_opener", return_value=opener):
                with self.assertRaises(TransportError) as caught:
                    chat_content(SETTINGS, [], 5)
                self.assertEqual(opener.open.call_count, 1)
                self.assertNotIn("synthetic-main", str(caught.exception))
                self.assertEqual(ai_transport._key_failures, {})

    def test_schema_failure_after_http_success_never_uses_another_key(self):
        opener = Mock()
        opener.open.return_value = BytesIO(envelope('{"rows":"invalid"}'))
        with patch("src.ai_transport.build_opener", return_value=opener):
            with self.assertRaises(ExtractionError):
                call_model(SETTINGS, [], 5)
        self.assertEqual(opener.open.call_count, 1)

    def test_failover_uses_remaining_timeout_budget(self):
        now = [100.0]
        opener = Mock()

        def respond(_request, *, timeout):
            if opener.open.call_count == 1:
                self.assertEqual(timeout, 5)
                now[0] += 3
                raise rejection(402)
            self.assertEqual(timeout, 2)
            return BytesIO(envelope())

        opener.open.side_effect = respond
        with patch("src.ai_transport.time.monotonic", side_effect=lambda: now[0]), \
                patch("src.ai_transport.build_opener", return_value=opener), self.assertLogs("src.ai_transport"):
            self.assertEqual(chat_content(SETTINGS, [], 5), "{}")
        self.assertEqual(opener.open.call_count, 2)

    def test_exhausted_timeout_budget_does_not_start_backup(self):
        now = [100.0]
        opener = Mock()

        def respond(*_args, **_kwargs):
            now[0] += 5
            raise rejection(402)

        opener.open.side_effect = respond
        with patch("src.ai_transport.time.monotonic", side_effect=lambda: now[0]), \
                patch("src.ai_transport.build_opener", return_value=opener), self.assertLogs("src.ai_transport"):
            with self.assertRaises(TransportError) as caught:
                chat_content(SETTINGS, [], 5)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(opener.open.call_count, 1)

    def test_material_guard_and_failover_share_the_original_deadline(self):
        now, admitted, released, timeouts = [100.0], [], [], []

        @contextmanager
        def guard(remaining):
            admitted.append(remaining)
            now[0] += 1
            try:
                yield
            finally:
                released.append(True)

        opener = Mock()

        def respond(_request, *, timeout):
            timeouts.append(timeout)
            if opener.open.call_count == 1:
                now[0] += 1
                raise rejection(402)
            return BytesIO(envelope())

        opener.open.side_effect = respond
        token = ai_transport.REQUEST_GUARD.set(guard)
        try:
            with patch("src.ai_transport.time.monotonic", side_effect=lambda: now[0]), \
                    patch("src.ai_transport.build_opener", return_value=opener), self.assertLogs("src.ai_transport"):
                self.assertEqual(chat_content(SETTINGS, [], 5), "{}")
        finally:
            ai_transport.REQUEST_GUARD.reset(token)
        self.assertEqual(admitted, [5, 3])
        self.assertEqual(timeouts, [4, 2])
        self.assertEqual(released, [True, True])
        self.assertEqual(opener.open.call_count, 2)

    def test_material_guard_exhaustion_does_not_send_or_fail_over(self):
        now, released = [100.0], []

        @contextmanager
        def guard(_remaining):
            now[0] += 6
            try:
                yield
            finally:
                released.append(True)

        opener = Mock()
        token = ai_transport.REQUEST_GUARD.set(guard)
        try:
            with patch("src.ai_transport.time.monotonic", side_effect=lambda: now[0]), \
                    patch("src.ai_transport.build_opener", return_value=opener):
                with self.assertRaises(TransportError) as caught:
                    chat_content(SETTINGS, [], 5)
        finally:
            ai_transport.REQUEST_GUARD.reset(token)
        self.assertEqual(caught.exception.kind, "timeout")
        opener.open.assert_not_called()
        self.assertEqual(released, [True])
        self.assertEqual(ai_transport._key_failures, {})

    def test_material_guard_wait_timeout_is_a_transport_timeout_without_http(self):
        @contextmanager
        def guard(_remaining):
            raise TimeoutError("synthetic guard wait")
            yield

        opener = Mock()
        token = ai_transport.REQUEST_GUARD.set(guard)
        try:
            with patch("src.ai_transport.build_opener", return_value=opener):
                with self.assertRaises(TransportError) as caught:
                    chat_content(SETTINGS, [], 5)
        finally:
            ai_transport.REQUEST_GUARD.reset(token)
        self.assertEqual(caught.exception.kind, "timeout")
        opener.open.assert_not_called()
        self.assertEqual(ai_transport._key_failures, {})


if __name__ == "__main__":
    unittest.main()
