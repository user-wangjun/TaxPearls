from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.ops_db import BackupError, create_backup, restore_backup, verify_backup
from webapp.storage import Store


class DatabaseOperationsTest(unittest.TestCase):
    def test_backup_restore_and_rollback_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "taxpearls.db"
            backup = root / "baseline.sqlite3"
            store = Store(database)
            store.create_user("admin", "correct-horse-2026", "管理员", "org_admin", "org-a")

            created = create_backup(database, backup)
            self.assertEqual(created["quick_check"], "ok")
            self.assertEqual(created["table_counts"]["users"], 1)
            self.assertTrue(Path(created["manifest"]).is_file())
            self.assertEqual(verify_backup(backup)["table_counts"]["users"], 1)

            store.create_user("teacher", "teacher-pass-2026", "教师", "teacher", "org-a")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

            restored = restore_backup(backup, database)
            safety_backup = Path(restored["safety_backup"])
            self.assertTrue(safety_backup.is_file())
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

            rolled_back = restore_backup(safety_backup, database)
            self.assertEqual(rolled_back["quick_check"], "ok")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_tampered_backup_is_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "taxpearls.db"
            backup = root / "backup.sqlite3"
            Store(database).create_user("admin", "correct-horse-2026", "管理员", "org_admin", "org-a")
            create_backup(database, backup)
            original = database.read_bytes()
            with backup.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(BackupError, "SHA-256"):
                restore_backup(backup, database)
            self.assertEqual(database.read_bytes(), original)

    def test_restore_refuses_active_wal_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "taxpearls.db"
            backup = root / "backup.sqlite3"
            Store(database).create_user("admin", "correct-horse-2026", "管理员", "org_admin", "org-a")
            create_backup(database, backup)
            Path(f"{database}-wal").write_bytes(b"active")
            with self.assertRaisesRegex(BackupError, "请先停止服务"):
                restore_backup(backup, database)


if __name__ == "__main__":
    unittest.main()
