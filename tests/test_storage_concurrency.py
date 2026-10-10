# -*- coding: utf-8 -*-
"""连接层并发回归：Windows WAL 库多连接并发写曾以 readonly 偶发失败。

修复：Store.connect() 以 RLock 串行化库访问（webapp/storage.py）。
本测试用多线程并发开连接写事务，保证修复后零失败。
CI（Linux）上本就稳定，测试守护的是 Windows 平台行为。
"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from webapp.storage import Store


class StorageConcurrencyTest(unittest.TestCase):
    def test_parallel_write_transactions_never_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "conc.db")
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
                self.assertEqual(db.execute("SELECT COUNT(*) c FROM ping").fetchone()["c"], 100)


if __name__ == "__main__":
    unittest.main()
