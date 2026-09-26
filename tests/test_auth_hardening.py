from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from webapp import app as app_module
from webapp.login_guard import LoginGuard
from webapp.storage import SetupAlreadyInitialized, Store


class AuthHardeningTests(unittest.TestCase):
    def test_setup_is_atomic_across_connections_and_unique_in_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.db"
            first, second = Store(path), Store(path)

            def attempt(args):
                store, name = args
                try:
                    return store.create_initial_admin(name, "strong-pass-2026", name, "default")["username"]
                except SetupAlreadyInitialized:
                    return "already initialized"

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, [(first, "owner-one"), (second, "owner-two")]))
            self.assertEqual(results.count("already initialized"), 1)
            self.assertEqual(len(first.list_users()), 1)
            with self.assertRaises(sqlite3.IntegrityError):
                first.create_user("another-admin", "strong-pass-2026", "其他管理员", "platform_admin", "default")
            self.assertEqual(len(Store(path).list_users()), 1)

    def test_legacy_multiple_admins_require_manual_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
                           "password_hash TEXT NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL, "
                           "org_id TEXT NOT NULL, active INTEGER NOT NULL, created_at TEXT NOT NULL)")
                db.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)", [
                    (str(index), f"old{index}", "legacy", "旧管理员", "platform_admin", "default", 1, "2026-01-01")
                    for index in (1, 2)
                ])
                db.commit()
            with self.assertRaisesRegex(RuntimeError, "未自动删除账号"):
                Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_password_policy_applies_to_setup_and_all_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            old = app_module.store
            app_module.store = Store(Path(directory) / "auth.db")
            try:
                with TestClient(app_module.app) as client:
                    for password in ("a", "password123!", "alllowercase2026"):
                        response = client.post("/api/setup", json={"username": "admin", "password": password})
                        self.assertEqual(response.status_code, 422, response.text)
                    response = client.post("/api/setup", json={"username": "admin", "password": "strong-pass-2026"})
                    self.assertEqual(response.status_code, 200, response.text)
                    client.post("/api/login", json={"username": "admin", "password": "strong-pass-2026"})
                    for role in ("student", "accountant"):
                        response = client.post("/api/users", json={
                            "username": role, "password": "a", "display_name": role,
                            "role": role, "org_id": "default",
                        })
                        self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(len(app_module.store.list_users()), 1)
            finally:
                app_module.store = old

    def test_login_guard_delays_and_locks_both_dimensions(self):
        now = [1000.0]
        guard = LoginGuard(clock=lambda: now[0])
        for attempt in range(1, 11):
            self.assertEqual(guard.retry_after("target", "192.0.2.1"), 0)
            self.assertEqual(guard.record_failure("target", "192.0.2.1"), attempt)
            wait = guard.retry_after("target", "192.0.2.1")
            if attempt >= 5:
                self.assertGreater(wait, 0)
            if attempt < 10:
                now[0] += wait
        self.assertEqual(guard.retry_after("other", "192.0.2.1"), 900)
        self.assertEqual(guard.retry_after("target", "192.0.2.2"), 900)
        guard.record_success("target")
        self.assertEqual(guard.retry_after("target", "192.0.2.2"), 0)
        now[0] += 901
        self.assertEqual(guard.retry_after("other", "192.0.2.1"), 0)

    def test_login_guard_prevents_parallel_attempts_per_account_or_ip(self):
        guard = LoginGuard()
        with guard.reserve("target", "192.0.2.1") as first:
            self.assertEqual(first, 0)
            with guard.reserve("target", "192.0.2.2") as account_overlap:
                self.assertEqual(account_overlap, 1)
            with guard.reserve("other", "192.0.2.1") as ip_overlap:
                self.assertEqual(ip_overlap, 1)
            with guard.reserve("other", "192.0.2.2") as independent:
                self.assertEqual(independent, 0)
        with guard.reserve("target", "192.0.2.1") as free_again:
            self.assertEqual(free_again, 0)

    def test_login_route_limits_and_audits_failures_without_plain_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            old_store, old_guard = app_module.store, app_module.login_guard
            app_module.store = Store(Path(directory) / "auth.db")
            app_module.login_guard = LoginGuard()
            try:
                app_module.store.create_initial_admin("rootadmin", "strong-pass-2026", "管理", "default")
                with TestClient(app_module.app) as client:
                    for _ in range(5):
                        result = client.post("/api/login", json={"username": "rootadmin", "password": "wrong-password"})
                        self.assertEqual(result.status_code, 401, result.text)
                    blocked = client.post("/api/login", json={"username": "rootadmin", "password": "strong-pass-2026"})
                    self.assertEqual(blocked.status_code, 429, blocked.text)
                    self.assertGreaterEqual(int(blocked.headers["Retry-After"]), 1)
                with app_module.store.connect() as db:
                    rows = db.execute("SELECT action,target_id,detail FROM audit_log ORDER BY id").fetchall()
                self.assertEqual(len(rows), 5)
                self.assertTrue(all(row["action"] == "login_failed" for row in rows))
                self.assertNotIn("rootadmin", str([tuple(row) for row in rows]))
                self.assertNotIn("wrong-password", str([tuple(row) for row in rows]))
            finally:
                app_module.store, app_module.login_guard = old_store, old_guard


if __name__ == "__main__":
    unittest.main()
