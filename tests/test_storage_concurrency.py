# -*- coding: utf-8 -*-
"""连接层并发回归：Windows WAL 库多连接并发写曾以 readonly 偶发失败。

根因：每请求开关连接使 -shm/-wal 反复删建，删建与并发连接的映射重叠时
SQLite 以 SQLITE_READONLY 打断写事务（Windows 特有，CI Linux 不复现）。

修复：长生命周期服务实例以 Store(keepalive=True) 持有哨兵空闲连接，
-shm 常驻后竞态消失（keep-alive 对照实验：无哨兵 160 写失败 3 次，
有哨兵 0 次）。本测试守护 keepalive=True 路径的并发写零失败。
"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from webapp.storage import Store


class StorageConcurrencyTest(unittest.TestCase):
    def test_parallel_write_transactions_never_readonly_with_keepalive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "conc.db", keepalive=True)
            try:
                with store.connect() as db:
                    db.execute("CREATE TABLE ping (id INTEGER PRIMARY KEY, v TEXT)")
                failures: list[str] = []
                started = threading.Event()

                def worker(tag: int) -> None:
                    started.wait()
                    for i in range(25):
                        try:
                            with store.connect() as db:
                                db.execute("BEGIN IMMEDIATE")
                                db.execute("INSERT INTO ping (v) VALUES (?)", (f"w{tag}-{i}",))
                        except sqlite3.OperationalError as exc:  # readonly / locked
                            failures.append(f"w{tag}-{i}: {exc}")

                threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
                for t in threads:
                    t.start()
                started.set()
                for t in threads:
                    t.join()
                self.assertEqual(failures, [])
                with store.connect() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) c FROM ping").fetchone()["c"], 100)
            finally:
                store.close()

    def test_default_store_has_no_keepalive_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "plain.db")
            self.assertIsNone(store._keepalive)
            with store.connect() as db:
                db.execute("SELECT 1").fetchone()
            store.close()
            self.assertIsNone(store._keepalive)


if __name__ == "__main__":
    unittest.main()
