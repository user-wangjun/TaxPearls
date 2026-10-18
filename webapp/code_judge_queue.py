"""判题任务队列与并发调度（FR-J05 v1，教学部署级）。

设计（与 material_jobs 同思路的本地队列，无外部 broker）：
- 学生提交先落 code_submission（pending），再落 code_judge_task（queued）；
- worker 逐个认领（queued→running）→ 调用 training_portal.judge_submission
  （逐用例受限子进程运行）→ done；异常→failed 并按退避重试，超过
  max_attempts 停在 failed 等教师介入（手动 retry 置回 queued）。
- 边界如实声明：单机 SQLite 队列，worker 与 webapp 同进程部署即可参与；
  多副本部署共享同一库也能工作（认领用 BEGIN IMMEDIATE 原子置换状态），
  但判题本身是本机子进程执行，判题吞吐随部署副本数线性扩展，跨机判题池
  不在本版范围。默认同步判题不变，队列模式由环境变量显式开启。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from src.settings import flag
from webapp.storage import _now

DEFAULT_MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5  # 第 n 次失败后 next_run_at = now + 5*n 秒
ERROR_TRUNCATE = 500

QUEUE_ENV = "TAXPEARLS_CODE_JUDGE_QUEUE_ENABLED"


def queue_enabled() -> bool:
    """判题队列开关（默认关：保持同步判题契约；开=提交入队由 worker 消化）。"""
    return flag(QUEUE_ENV, False)


def enqueue(store, submission_id: str, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> str:
    """为一份 pending 提交落队任务。已有任务则原样返回任务 id（幂等）。"""
    task_id = str(uuid.uuid4())
    now = _now()
    with store.connect() as db:
        row = db.execute(
            "SELECT id FROM code_judge_task WHERE submission_id=?", (submission_id,)).fetchone()
        if row:
            return row["id"]
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO code_judge_task (id, submission_id, state, attempts, max_attempts,
                                            next_run_at, created_at, updated_at)
               VALUES (?,?, 'queued', 0, ?, ?, ?, ?)""",
            (task_id, submission_id, max(1, int(max_attempts)), now, now, now))
    return task_id


def _claim(store) -> dict[str, Any] | None:
    """原子认领一个到期任务（queued→running）。无任务返回 None。"""
    now = _now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT id, submission_id, attempts, max_attempts FROM code_judge_task
               WHERE state='queued' AND next_run_at<=? ORDER BY next_run_at, created_at LIMIT 1""",
            (now,)).fetchone()
        if not row:
            return None
        db.execute(
            """UPDATE code_judge_task SET state='running', attempts=attempts+1,
                    picked_at=?, updated_at=? WHERE id=? AND state='queued'""",
            (now, now, row["id"]))
        return dict(row)


def _finish(store, task_id: str, *, ok: bool, error: str = "") -> None:
    now = _now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if ok:
            db.execute(
                """UPDATE code_judge_task SET state='done', last_error='', finished_at=?,
                       updated_at=? WHERE id=?""", (now, now, task_id))
            return
        row = db.execute(
            "SELECT attempts, max_attempts FROM code_judge_task WHERE id=?", (task_id,)).fetchone()
        attempts = int(row["attempts"]) if row else 1
        max_attempts = int(row["max_attempts"]) if row else 1
        if attempts < max_attempts:
            backoff = (datetime.fromisoformat(now)
                       + timedelta(seconds=RETRY_BACKOFF_SECONDS * attempts)).isoformat()
            db.execute(
                """UPDATE code_judge_task SET state='queued', last_error=?, next_run_at=?,
                       updated_at=? WHERE id=?""", (error[:ERROR_TRUNCATE], backoff, now, task_id))
        else:
            db.execute(
                """UPDATE code_judge_task SET state='failed', last_error=?, finished_at=?,
                       updated_at=? WHERE id=?""", (error[:ERROR_TRUNCATE], now, now, task_id))
            # 判题彻底失败时把提交停在可辨识状态（教师可见，可重试）
            db.execute(
                """UPDATE code_submission SET status='judgement_failed'
                   WHERE id=(SELECT submission_id FROM code_judge_task WHERE id=?)""",
                (task_id,))


def run_one(store) -> dict[str, Any] | None:
    """认领并执行一个判题任务。无任务返回 None，否则返回执行摘要。"""
    from webapp import training_portal
    task = _claim(store)
    if not task:
        return None
    with store.connect() as db:
        row = db.execute("SELECT status FROM code_submission WHERE id=?",
                         (task["submission_id"],)).fetchone()
    if row and row["status"] != "pending":
        # 已被判题（如重试前学生已重新提交触发同步判题）：直接完结，不重复判。
        _finish(store, task["id"], ok=True)
        return {"task_id": task["id"], "submission_id": task["submission_id"],
                "state": "done", "attempts": task["attempts"], "already_judged": True}
    try:
        result = training_portal.judge_submission(store, task["submission_id"])
        _finish(store, task["id"], ok=True)
        return {"task_id": task["id"], "submission_id": task["submission_id"],
                "state": "done", "attempts": task["attempts"] + 1,
                "judge_status": result["status"], "total_score": result["total_score"]}
    except Exception as exc:  # 判题失败须入重试状态机，不允许打断 worker
        _finish(store, task["id"], ok=False, error=f"{type(exc).__name__}: {exc}")
        return {"task_id": task["id"], "submission_id": task["submission_id"],
                "state": "retry_or_failed", "attempts": task["attempts"] + 1}


def run_pending(store, *, limit: int = 20) -> list[dict[str, Any]]:
    """同步消化至多 limit 个到期任务（测试与手动补跑入口）。"""
    out = []
    for _ in range(max(1, min(int(limit), 100))):
        item = run_one(store)
        if item is None:
            break
        out.append(item)
    return out


def retry_task(store, task_id: str) -> dict[str, Any]:
    """教师手动重试：failed/running 卡死任务置回 queued（从头计次可选）。"""
    now = _now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT state, attempts FROM code_judge_task WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise training_portal_error("判题任务不存在。", 404)
        if row["state"] == "done":
            raise training_portal_error("任务已完成判题，无需重试。")
        db.execute(
            """UPDATE code_judge_task SET state='queued', next_run_at=?, finished_at=NULL,
                   updated_at=? WHERE id=?""", (now, now, task_id))
        row = db.execute("SELECT * FROM code_judge_task WHERE id=?", (task_id,)).fetchone()
        db.execute(
            """UPDATE code_submission SET status='pending'
               WHERE id=? AND status='judgement_failed'""", (row["submission_id"],))
    return dict(row)


def training_portal_error(message: str, status: int = 400) -> Exception:
    from webapp.training_portal import TrainingPortalError
    return TrainingPortalError(message, status)


def list_tasks(store, *, state: str | None = None, college: str | None = None,
               limit: int = 50) -> list[dict[str, Any]]:
    """任务视图（教师端按院校过滤；state 可选过滤）。"""
    clauses, params = [], []
    if state:
        clauses.append("t.state=?")
        params.append(state)
    if college:
        clauses.append("st.college=?")
        params.append(college)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 200)))
    with store.connect() as db:
        rows = db.execute(
            f"""SELECT t.id, t.submission_id, t.state, t.attempts, t.max_attempts,
                       t.last_error, t.picked_at, t.finished_at, t.next_run_at, t.created_at,
                       t.updated_at, s.problem_id, s.student_id, s.status AS submission_status,
                       r.status AS judge_status, r.total_score
                FROM code_judge_task t
                JOIN code_submission s ON s.id=t.submission_id
                JOIN student_info st ON st.id=s.student_id
                LEFT JOIN code_judge_result r ON r.submission_id=t.submission_id
                {where} ORDER BY t.created_at DESC LIMIT ?""", params).fetchall()
    return [dict(r) for r in rows]


class JudgeWorker:
    """后台判题线程（与 webapp 同进程；interval 秒轮询一次到期任务）。"""

    def __init__(self, store_provider: Callable[[], Any], *, interval: float = 1.0):
        self.store_provider = store_provider
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="code-judge-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = run_one(self.store_provider()) is not None
            except Exception:
                worked = False
            if not worked:
                time.sleep(self.interval)
