"""独立在线考试（FR-U07 v1）· E1：出卷、题目、名单、发布冻结。

三条纪律：
1. **不进作业主链。** ``assignments.audit_id NOT NULL``（schema.py:57-63）、组卷每题也要审计案例
   （classroom.py:162-175）、判分口径是"命中/漏检/误报加权"（src/training.py:9-72）——在线考试装不进
   这套模型，所以考试自己一套表，**不写** assignments / submissions / training_papers /
   training_assignment_settings，也不把自己的分数混进学情统计与 FR-U05 报表。
2. **题目进卷即快照。** 题目复制到 ``online_exam_question``，发布后禁写；之后题库怎么改，
   已经发布的卷面不漂移（与 ``training_task_version`` 同思路，这里不需要额外版本表，
   因为答卷自带题目副本，指纹用于追溯"这份卷到底长什么样"）。
3. **名单以 users.id 为准。** 三条身份线不对称（users / student_info / university_identity_links），
   导入时查不到的学号要显式列出，不静默丢弃。

E1 出卷与发布 / E2 学生机考与监考台 / E3 判分复核、成绩发布与成绩单导出。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any
from urllib.parse import quote

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

# draft=草稿 published=已发布（到点即开，见 phase） closed=已结束
# archived=已归档（软删，不删学生记录）
STATUS = ("draft", "published", "closed", "archived")
KINDS = ("single", "multiple")
RELEASE_POLICY = ("manual", "after_submit", "after_close")
MAX_QUESTIONS = 200
MAX_DURATION = 600


class ExamError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _recreate_if_stale(db, table: str, marker: str) -> None:
    """开发期迭代过外键方向时，把**还没有数据**的旧表推倒重建。

    只对学生为空的表动手；一旦有真实考试数据就保持原样（宁可留着旧定义，也不动数据）。
    """
    stub = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not stub or not stub[0] or marker not in stub[0]:
        return
    if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
        return
    db.execute(f"DROP TABLE {table}")


def migrate(db) -> None:
    """考试六表。建表顺序：online_exam_paper 先，其余外键引用它。

    教师/管理员的操作身份是 ``college_user.id``（教学会话），不是平台 ``users.id``；
    与学生名单不同——名单必须是平台账号（学生用学号登录），所以只有 roster/session
    的学生列指向 users(id)。首次落地时 owner_id / enrolled_by / manual_by 误指向 users(id)，
    这里对空表直接重建，有数据的库留原样并在 README 的已知限制里说明。
    """
    _recreate_if_stale(db, "online_exam_paper", "owner_id TEXT NOT NULL REFERENCES users(id)")
    _recreate_if_stale(db, "online_exam_roster", "enrolled_by TEXT REFERENCES users(id)")
    _recreate_if_stale(db, "online_exam_answer", "manual_by TEXT REFERENCES users(id)")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS online_exam_paper (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,                 -- 机构键（沿用课堂/作业线的隔离口径）
            college TEXT,                         -- 院校名（教师身份带出，供院校维度筛选）
            course_id TEXT REFERENCES course(id), -- 关联课程（可空）
            owner_id TEXT NOT NULL REFERENCES college_user(id),
            title TEXT NOT NULL,
            description TEXT,
            duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
            open_at TEXT,                         -- 作答窗口开始（空=发布即可考）
            close_at TEXT,                        -- 作答窗口结束（空=不限时）
            late_entry_minutes INTEGER NOT NULL DEFAULT 0,  -- 迟到仍可进入的宽限分钟
            shuffle_questions INTEGER NOT NULL DEFAULT 0,
            shuffle_options INTEGER NOT NULL DEFAULT 0,
            pass_score REAL,                      -- 及格线（空=不判及格）
            total_points REAL NOT NULL DEFAULT 0, -- 题目分值合计（发布时落定）
            answer_release TEXT NOT NULL DEFAULT 'manual'
                CHECK (answer_release IN ('manual','after_submit','after_close')),
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','published','closed','archived')),
            fingerprint TEXT,                     -- 发布时卷面指纹（sha256），用于追溯
            released_at TEXT,                     -- 成绩/解析发布时间（E3 使用）
            released_by TEXT REFERENCES college_user(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by TEXT REFERENCES college_user(id)
        );
        CREATE INDEX IF NOT EXISTS idx_online_exam_paper_scope
            ON online_exam_paper(college, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_online_exam_paper_org ON online_exam_paper(org_id, status);

        -- 卷内题目快照：发布后禁写，题库变化不影响已发布卷面
        CREATE TABLE IF NOT EXISTS online_exam_question (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES online_exam_paper(id),
            position INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('single','multiple')),
            stem TEXT NOT NULL,
            options_json TEXT NOT NULL,           -- [{"key":"A","text":"…"},…]
            answer_keys_json TEXT NOT NULL,       -- ["A"] 或多选 ["A","C"]
            points REAL NOT NULL CHECK(points > 0),
            explanation TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','bank','ai')),
            source_question_id TEXT,
            fingerprint TEXT NOT NULL,            -- 单题内容指纹
            created_at TEXT NOT NULL,
            UNIQUE(paper_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_oexam_question_paper
            ON online_exam_question(paper_id, position);

        CREATE TABLE IF NOT EXISTS online_exam_roster (
            paper_id TEXT NOT NULL REFERENCES online_exam_paper(id),
            student_id TEXT NOT NULL REFERENCES users(id),
            student_no TEXT,                      -- 导入时快照，便于核对
            name TEXT,                            -- 导入时快照
            source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('class','manual')),
            enrolled_by TEXT REFERENCES college_user(id),
            enrolled_at TEXT NOT NULL,
            PRIMARY KEY (paper_id, student_id)
        );
        CREATE INDEX IF NOT EXISTS idx_online_exam_roster_paper ON online_exam_roster(paper_id);

        -- 考生场次（E2 使用，E1 先建表避免后续再迁移）。
        -- id 是主键给 online_exam_answer / online_exam_event 做外键；一人一场靠 UNIQUE 兜，
        -- 不把 (paper_id, student_id) 当主键，否则 online_exam_answer 的外键没有唯一列可指。
        CREATE TABLE IF NOT EXISTS online_exam_session (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES online_exam_paper(id),
            student_id TEXT NOT NULL REFERENCES users(id),
            started_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,                -- 服务端计算：started_at + duration，不超 close_at
            submitted_at TEXT,
            status TEXT NOT NULL DEFAULT 'in_progress'
                CHECK (status IN ('in_progress','submitted','forced')),
            violations INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_at TEXT,
            ip TEXT, user_agent TEXT,
            score REAL, adjusted_score REAL,
            UNIQUE (paper_id, student_id)
        );
        CREATE INDEX IF NOT EXISTS idx_online_exam_session_paper ON online_exam_session(paper_id, status);

        CREATE TABLE IF NOT EXISTS online_exam_answer (
            session_id TEXT NOT NULL REFERENCES online_exam_session(id),
            question_id TEXT NOT NULL REFERENCES online_exam_question(id),
            picked_json TEXT NOT NULL DEFAULT '[]',
            saved_at TEXT NOT NULL,
            correct INTEGER,                      -- 判分后填，按发布策略决定是否下发
            points_gained REAL,
            manual_score REAL, manual_by TEXT REFERENCES college_user(id), manual_note TEXT,
            PRIMARY KEY (session_id, question_id)
        );

        CREATE TABLE IF NOT EXISTS online_exam_event (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES online_exam_session(id),
            kind TEXT NOT NULL,                   -- blur/visibility/copy/paste/contextmenu/heartbeat/resume/concurrent
            at TEXT NOT NULL,
            detail_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_oexam_event_session
            ON online_exam_event(session_id, at);
    """)


# --------------------------------------------------------------------------- 工具

def _college_org(db, college: str) -> str:
    from webapp import university_members
    return university_members.school_org(db, college)


def _clean(value: Any, limit: int = 2000) -> str | None:
    text = ("" if value is None else str(value)).strip()
    return (text[:limit] or None) if text else None


def _iso(value: Any, label: str) -> str | None:
    """把时间文本归一化成 ISO 8601；不接受就算了，直接 422 说清楚。"""
    text = (value or "").strip() if isinstance(value, str) else None
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError as exc:
        raise ExamError(f"{label}不是合法时间，形如 2026-11-05T09:00:00。") from exc


def _phase(row: sqlite3.Row, *, moment: str | None = None) -> str:
    """阶段是派生的（不落库），避免靠定时任务改状态。"""
    if row["status"] == "draft":
        return "draft"
    if row["status"] == "archived":
        return "archived"
    if row["status"] == "closed":
        return "closed"
    stamp = moment or now()
    if row["open_at"] and stamp < row["open_at"]:
        return "scheduled"
    if row["close_at"] and stamp >= row["close_at"]:
        return "ended"
    return "open"


def _question_digest(row: sqlite3.Row) -> dict[str, Any]:
    return {"stem": row["stem"], "options": row["options_json"],
            "answer_keys": row["answer_keys_json"], "points": row["points"],
            "kind": row["kind"]}


def _fingerprint(rows: list[sqlite3.Row]) -> str:
    payload = json.dumps([_question_digest(r) for r in rows], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 读取

def _paper_row(db, paper_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM online_exam_paper WHERE id=?", (paper_id,)).fetchone()
    if not row:
        raise ExamError("考试不存在。", 404)
    return row


def _require(db, paper_id: str, college: str) -> sqlite3.Row:
    row = _paper_row(db, paper_id)
    if row["college"] != college:
        raise ExamError("考试不存在。", 404)
    return row


def _counts(db, paper_id: str) -> dict[str, Any]:
    questions = db.execute(
        "SELECT count(*) n, IFNULL(sum(points),0) p FROM online_exam_question WHERE paper_id=?",
        (paper_id,)).fetchone()
    roster = db.execute("SELECT count(*) n FROM online_exam_roster WHERE paper_id=?", (paper_id,)).fetchone()
    sessions = db.execute(
        """SELECT count(*) used, sum(status IN ('submitted','forced')) done
             FROM online_exam_session WHERE paper_id=?""", (paper_id,)).fetchone()
    return {"question_count": int(questions["n"] or 0), "total_points": float(questions["p"] or 0),
            "roster_count": int(roster["n"] or 0),
            "session_count": int(sessions["used"] or 0), "submitted_count": int(sessions["done"] or 0)}


def _payload(db, row: sqlite3.Row, *, with_questions: bool = True) -> dict[str, Any]:
    paper = dict(row)
    paper["phase"] = _phase(row)
    paper["can_edit"] = paper["status"] == "draft"
    paper.update(_counts(db, row["id"]))
    if paper["total_points"] and not row["total_points"]:
        paper["total_points"] = float(paper["total_points"])
    owner = db.execute("SELECT display_name FROM college_user WHERE id=?", (row["owner_id"],)).fetchone()
    paper["owner_name"] = owner["display_name"] if owner else None
    paper["course"] = None
    if row["course_id"]:
        course = db.execute("SELECT name, semester FROM course WHERE id=?", (row["course_id"],)).fetchone()
        if course:
            paper["course"] = {"id": row["course_id"], "name": course["name"], "semester": course["semester"]}
    paper["questions"] = []
    if with_questions:
        rows = db.execute(
            "SELECT * FROM online_exam_question WHERE paper_id=? ORDER BY position", (row["id"],)).fetchall()
        paper["questions"] = [{
            "id": q["id"], "position": q["position"], "kind": q["kind"], "stem": q["stem"],
            "options": json.loads(q["options_json"]), "answer_keys": json.loads(q["answer_keys_json"]),
            "points": q["points"], "explanation": q["explanation"], "source": q["source"],
            "source_question_id": q["source_question_id"]} for q in rows]
    return paper


def list_papers(store, college: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    with store.connect() as db:
        where = "" if include_archived else " AND status!='archived'"
        rows = db.execute(
            f"SELECT * FROM online_exam_paper WHERE college=?{where} ORDER BY created_at DESC",
            (college,)).fetchall()
        return [_payload(db, r, with_questions=False) for r in rows]


def paper_detail(store, paper_id: str, college: str) -> dict[str, Any]:
    with store.connect() as db:
        row = _require(db, paper_id, college)
        paper = _payload(db, row)
        roster = db.execute(
            "SELECT * FROM online_exam_roster WHERE paper_id=? ORDER BY student_no, name", (paper_id,)).fetchall()
        paper["roster"] = [dict(r) for r in roster]
        return paper


def bank_questions(store, *, certificate_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """可选做题库（只列已审核；题库是全平台共享，无院校隔离——已知限制，见 notes）。"""
    limit = max(1, min(int(limit or 50), 200))
    with store.connect() as db:
        params: list[Any] = ["approved"]
        where = ""
        if certificate_id:
            where = " AND certificate_id=?"
            params.append(certificate_id)
        rows = db.execute(
            f"""SELECT q.* FROM training_bank_question q
                WHERE q.status=?{where} ORDER BY q.year DESC, q.question_no LIMIT ?""",
            (*params, limit)).fetchall()
        return [{"id": r["id"], "certificate_id": r["certificate_id"], "year": r["year"],
                 "question_no": r["question_no"], "stem": r["stem"],
                 "options": json.loads(r["options_json"]),
                 "kind": "multiple" if len(json.loads(r["answer_keys_json"])) > 1 else "single",
                 "source_ref": r["source_ref"]} for r in rows]


# --------------------------------------------------------------------------- 写入

def create_paper(store, actor: dict[str, Any], college: str, *, title: str,
                 description: str | None = None, course_id: str | None = None,
                 duration_minutes: int = 60, open_at: str | None = None,
                 close_at: str | None = None, late_entry_minutes: int = 0,
                 shuffle_questions: bool = False, shuffle_options: bool = False,
                 pass_score: float | None = None,
                 answer_release: str = "manual") -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise ExamError("考试名称必填。")
    if not college:
        raise ExamError("院校必填。")
    duration = int(duration_minutes or 0)
    if not 1 <= duration <= MAX_DURATION:
        raise ExamError(f"考试时长需在 1-{MAX_DURATION} 分钟之间。")
    late = int(late_entry_minutes or 0)
    if late < 0:
        raise ExamError("迟到宽限分钟不能为负。")
    if answer_release not in RELEASE_POLICY:
        raise ExamError("成绩开放策略不合法。")
    if pass_score is not None and float(pass_score) < 0:
        raise ExamError("及格线不能为负。")
    opened, closed = _iso(open_at, "开始时间"), _iso(close_at, "结束时间")
    if opened and closed and closed <= opened:
        raise ExamError("作答窗口的结束时间须晚于开始时间。")
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        org_id = _college_org(db, college)
        if course_id:
            if not db.execute("SELECT 1 FROM course WHERE id=? AND college=?", (course_id, college)).fetchone():
                raise ExamError("课程不存在或不属于本院校。", 404)
        pid = str(uuid.uuid4())
        db.execute(
            """INSERT INTO online_exam_paper (id, org_id, college, course_id, owner_id, title, description,
                   duration_minutes, open_at, close_at, late_entry_minutes, shuffle_questions,
                   shuffle_options, pass_score, total_points, answer_release, status,
                   created_at, updated_at, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,'draft',?,?,?)""",
            (pid, org_id, college, course_id, actor["id"], title, _clean(description, 4000),
             duration, opened, closed, late, int(bool(shuffle_questions)), int(bool(shuffle_options)),
             pass_score, answer_release, stamp, stamp, actor["id"]))
        row = _paper_row(db, pid)
        return _payload(db, row)


def update_paper(store, actor: dict[str, Any], paper_id: str, college: str,
                 fields: dict[str, Any]) -> dict[str, Any]:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _require(db, paper_id, college)
        if row["status"] != "draft":
            # 已发布的卷只能改说明：题目、时长、策略动了就等于改卷面，必须新建一份
            editable = {"description"}
            if set(fields) - editable:
                raise ExamError("已发布的考试只能修改考试说明；题目、时长与策略不可再改，请新建考试。", 409)
        if "course_id" in fields and fields["course_id"]:
            if not db.execute("SELECT 1 FROM course WHERE id=? AND college=?",
                              (fields["course_id"], college)).fetchone():
                raise ExamError("课程不存在或不属于本院校。", 404)
        sets, params = [], []
        if "title" in fields:
            title = (fields["title"] or "").strip()
            if not title:
                raise ExamError("考试名称必填。")
            sets.append("title=?")
            params.append(title)
        for key in ("description", "course_id"):
            if key in fields:
                value = fields[key]
                sets.append(f"{key}=?")
                params.append(_clean(value, 4000) if key == "description" else (value or None))
        if sets:
            params.extend([now(), paper_id])
            db.execute(f"UPDATE online_exam_paper SET {', '.join(sets)}, updated_at=? WHERE id=?", params)
        return _payload(db, _paper_row(db, paper_id))


def _normalize_question(item: dict[str, Any]) -> dict[str, Any]:
    kind = (item.get("kind") or "single").strip()
    if kind not in KINDS:
        raise ExamError(f"题型「{kind}」不支持，v1 只做 single/multiple。")
    stem = (item.get("stem") or "").strip()
    if not stem:
        raise ExamError("题干不能为空。")
    options = item.get("options") or []
    if len(options) < 2:
        raise ExamError(f"题目「{stem[:20]}…」至少要有两个选项。")
    keys = [str(o.get("key") or "").strip() for o in options]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        raise ExamError(f"题目「{stem[:20]}…」的选项编号不能重复或为空。")
    answers = [str(k).strip() for k in (item.get("answer_keys") or []) if str(k).strip()]
    if not answers:
        raise ExamError(f"题目「{stem[:20]}…」必须给标准答案。")
    unknown = [k for k in answers if k not in keys]
    if unknown:
        raise ExamError(f"题目「{stem[:20]}…」的标准答案 {unknown} 不在选项里。")
    if kind == "single" and len(answers) != 1:
        raise ExamError(f"单选题「{stem[:20]}…」只能有一个标准答案。")
    if kind == "multiple" and len(answers) < 2:
        raise ExamError(f"多选题「{stem[:20]}…」至少要有两个标准答案。")
    points = float(item.get("points") or 0)
    if points <= 0:
        raise ExamError(f"题目「{stem[:20]}…」的分值必须大于 0。")
    return {"kind": kind, "stem": stem,
            "options": [{"key": k, "text": (str(o.get("text") or "").strip())} for k, o in zip(keys, options)],
            "answer_keys": answers, "points": points,
            "explanation": str(item.get("explanation") or "").strip()[:4000],
            "source": (item.get("source") or "manual").strip(),
            "source_question_id": item.get("source_question_id") or None}


def replace_questions(store, actor: dict[str, Any], paper_id: str, college: str,
                      questions: list[dict[str, Any]]) -> dict[str, Any]:
    """整份替换卷内题目（只在草稿状态可用）。"""
    if len(questions) > MAX_QUESTIONS:
        raise ExamError(f"一份卷最多 {MAX_QUESTIONS} 道题。")
    normalized = [_normalize_question(q) for q in questions]
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _require(db, paper_id, college)
        if row["status"] != "draft":
            raise ExamError("已发布的考试不能再改题目，请新建考试。", 409)
        if db.execute("SELECT 1 FROM online_exam_session WHERE paper_id=? LIMIT 1", (paper_id,)).fetchone():
            raise ExamError("已有学生进入考试，不能再改题目。", 409)
        db.execute("DELETE FROM online_exam_question WHERE paper_id=?", (paper_id,))
        for position, item in enumerate(normalized, start=1):
            qid = str(uuid.uuid4())
            db.execute(
                """INSERT INTO online_exam_question (id, paper_id, position, kind, stem, options_json,
                       answer_keys_json, points, explanation, source, source_question_id,
                       fingerprint, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (qid, paper_id, position, item["kind"], item["stem"],
                 json.dumps(item["options"], ensure_ascii=False),
                 json.dumps(item["answer_keys"], ensure_ascii=False), item["points"],
                 item["explanation"], item["source"], item["source_question_id"],
                 hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                        .hexdigest(), stamp))
        db.execute("UPDATE online_exam_paper SET total_points=?, updated_at=? WHERE id=?",
                   (sum(i["points"] for i in normalized), stamp, paper_id))
        return _payload(db, _paper_row(db, paper_id))


def add_bank_questions(store, actor: dict[str, Any], paper_id: str, college: str,
                       question_ids: list[str]) -> dict[str, Any]:
    """从已审核题库批量选题，追加到卷尾（只在草稿状态可用）。"""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require(db, paper_id, college)
        row = _paper_row(db, paper_id)
        if row["status"] != "draft":
            raise ExamError("已发布的考试不能再加题，请新建考试。", 409)
        position = db.execute(
            "SELECT IFNULL(max(position),0) p FROM online_exam_question WHERE paper_id=?",
            (paper_id,)).fetchone()["p"]
        added, missing = 0, []
        stamp = now()
        for qid in question_ids:
            bank = db.execute(
                """SELECT * FROM training_bank_question WHERE id=? AND status='approved'""",
                (qid,)).fetchone()
            if not bank:
                missing.append(qid)
                continue
            answers = json.loads(bank["answer_keys_json"])
            item = _normalize_question({
                "kind": "multiple" if len(answers) > 1 else "single",
                "stem": bank["stem"], "options": json.loads(bank["options_json"]),
                "answer_keys": answers, "points": 2,
                "explanation": bank["explanation"], "source": "bank", "source_question_id": bank["id"]})
            position += 1
            db.execute(
                """INSERT INTO online_exam_question (id, paper_id, position, kind, stem, options_json,
                       answer_keys_json, points, explanation, source, source_question_id,
                       fingerprint, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), paper_id, position, item["kind"], item["stem"],
                 json.dumps(item["options"], ensure_ascii=False),
                 json.dumps(item["answer_keys"], ensure_ascii=False), item["points"],
                 item["explanation"], "bank", bank["id"],
                 hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                        .hexdigest(), stamp))
            added += 1
        if added:
            points = db.execute(
                "SELECT IFNULL(sum(points),0) p FROM online_exam_question WHERE paper_id=?",
                (paper_id,)).fetchone()["p"]
            db.execute("UPDATE online_exam_paper SET total_points=?, updated_at=? WHERE id=?",
                       (points, stamp, paper_id))
        payload = _payload(db, _paper_row(db, paper_id))
        payload["added"] = added
        payload["missing"] = missing
        return payload


def replace_roster(store, actor: dict[str, Any], paper_id: str, college: str, *,
                   class_ids: list[str] | None = None, student_ids: list[str] | None = None,
                   student_nos: list[str] | None = None) -> dict[str, Any]:
    """整份替换考生名单。三种来源可以混用；查不到的学号列出来，不静默丢。"""
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _require(db, paper_id, college)
        if row["status"] == "closed":
            raise ExamError("考试已结束，不能改名单。", 409)
        resolved: dict[str, dict[str, Any]] = {}

        def take(user_row, source: str) -> None:
            # 这里既有 sqlite3.Row 也有 dict（名单来源不同），统一成 dict 再取值
            user = dict(user_row) if user_row is not None else None
            if not user or resolved.get(user["id"]):
                return
            resolved[user["id"]] = {"student_id": user["id"], "source": source,
                                    "name": user.get("display_name") or user.get("name"),
                                    "student_no": user.get("username") or user.get("student_no")}

        if class_ids:
            marks = ",".join("?" * len(class_ids))
            for cls in db.execute(
                    f"""SELECT c.id, c.name FROM training_classes c
                         WHERE c.id IN ({marks}) AND c.org_id=?""",
                    (*class_ids, row["org_id"])).fetchall():
                for member in db.execute(
                        """SELECT u.id, u.display_name, u.username FROM training_class_members m
                             JOIN users u ON u.id=m.student_id AND u.active=1
                            WHERE m.class_id=?""", (cls["id"],)).fetchall():
                    take(member, "class")
        if student_ids:
            marks = ",".join("?" * len(student_ids))
            for member in db.execute(
                    f"""SELECT id, display_name, username FROM users
                         WHERE id IN ({marks}) AND active=1 AND role='student' AND org_id=?""",
                    (*student_ids, row["org_id"])).fetchall():
                take(member, "manual")
        unresolved: list[dict[str, str]] = []
        nos = [str(n).strip() for n in (student_nos or []) if str(n).strip()]
        if nos:
            marks = ",".join("?" * len(nos))
            for info in db.execute(
                    f"""SELECT s.student_no, s.name, l.user_id
                          FROM student_info s
                          LEFT JOIN university_identity_links l ON l.student_id=s.id
                         WHERE s.college=? AND s.student_no IN ({marks})""",
                    (college, *nos)).fetchall():
                if not info["user_id"]:
                    unresolved.append({"student_no": info["student_no"], "name": info["name"],
                                       "reason": "学籍未关联平台账号，无法进入考试"})
                    continue
                user = db.execute(
                    "SELECT id, display_name, username FROM users WHERE id=? AND active=1",
                    (info["user_id"],)).fetchone()
                take({"id": user["id"], "display_name": user["display_name"],
                      "username": info["student_no"]} if user else None, "manual")
            known = {r["student_no"] for r in db.execute(
                f"SELECT student_no FROM student_info WHERE college=? AND student_no IN ({marks})",
                (college, *nos))}
            for number in nos:
                if number not in known:
                    unresolved.append({"student_no": number, "name": "", "reason": "本院校查无此学号"})

        db.execute("DELETE FROM online_exam_roster WHERE paper_id=?", (paper_id,))
        for entry in resolved.values():
            db.execute(
                """INSERT OR REPLACE INTO online_exam_roster (paper_id, student_id, student_no, name,
                       source, enrolled_by, enrolled_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (paper_id, entry["student_id"], entry["student_no"], entry["name"],
                 entry["source"], actor["id"], stamp))
        db.execute("UPDATE online_exam_paper SET updated_at=? WHERE id=?", (stamp, paper_id))
        return {"paper_id": paper_id, "enrolled": len(resolved), "unresolved": unresolved,
                "roster": [dict(r) for r in db.execute(
                    "SELECT * FROM online_exam_roster WHERE paper_id=? ORDER BY student_no, name",
                    (paper_id,)).fetchall()]}


def publish(store, actor: dict[str, Any], paper_id: str, college: str) -> dict[str, Any]:
    """发布：发布后卷面冻结（题目禁写），只让学生分子 may enter（E2 的起点）。"""
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _require(db, paper_id, college)
        if row["status"] != "draft":
            raise ExamError("只有草稿状态的考试可以发布。", 409)
        questions = db.execute(
            "SELECT * FROM online_exam_question WHERE paper_id=? ORDER BY position", (paper_id,)).fetchall()
        if not questions:
            raise ExamError("这份考试还没有题目，先加题再发布。")
        roster = db.execute("SELECT count(*) n FROM online_exam_roster WHERE paper_id=?", (paper_id,)).fetchone()["n"]
        if not roster:
            raise ExamError("考生名单是空的，先导入名单再发布。")
        total = sum(q["points"] for q in questions)
        db.execute(
            """UPDATE online_exam_paper SET status='published', total_points=?, fingerprint=?, updated_at=?
                WHERE id=?""",
            (total, _fingerprint(questions), stamp, paper_id))
        payload = _payload(db, _paper_row(db, paper_id))
        payload["frozen"] = {"question_count": len(questions), "total_points": total,
                             "fingerprint": payload["fingerprint"]}
        return payload


def close(store, actor: dict[str, Any], paper_id: str, college: str) -> dict[str, Any]:
    """提前结束（到点不 manual 也由惰性收卷处理，见 E2）。"""
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _require(db, paper_id, college)
        if row["status"] != "published":
            raise ExamError("只有已发布的考试可以结束。", 409)
        db.execute("UPDATE online_exam_paper SET status='closed', updated_at=? WHERE id=?", (stamp, paper_id))
        return _payload(db, _paper_row(db, paper_id))


# --------------------------------------------------------------------------- 学生机考（E2）

def _student_user_id(db, who: dict[str, Any]) -> str:
    """名单存的是平台账号 users.id，学生会话给的是 student_info.id，靠身份桥接。"""
    row = db.execute("SELECT user_id FROM university_identity_links WHERE student_id=?",
                     (who["id"],)).fetchone()
    if not row or not row["user_id"]:
        raise ExamError("该学籍还没有关联平台账号，无法进入考试；请联系管理员。", 403)
    return row["user_id"]


def _seconds_left(session: sqlite3.Row, *, moment: str | None = None) -> int:
    stamp = datetime.fromisoformat(moment or now())
    end = datetime.fromisoformat(session["ends_at"])
    return max(0, int((end - stamp).total_seconds()))


def _close_due_sessions(db, paper_id: str, *, moment: str | None = None) -> int:
    """惰性收卷：把到点仍未交、或教师已提前结束的会话按已答内容收掉。

    没有定时任务，所以每次访问都查一遍；"到点"两个口径——考生时间用完（ends_at 到了），
    或整场考试已经不是 published（教师提前结束/归档），否则监考台会一直显示"作答中"。
    """
    stamp = moment or now()
    paper = _paper_row(db, paper_id)
    over = 1 if paper["status"] != "published" else 0
    rows = db.execute(
        """SELECT * FROM online_exam_session
            WHERE paper_id=? AND status='in_progress' AND (? OR ends_at<=?)""",
        (paper_id, over, stamp)).fetchall()
    for session in rows:
        db.execute("UPDATE online_exam_session SET status='forced', submitted_at=? WHERE id=?",
                   (stamp, session["id"]))
        _grade(db, session["id"])
    # 写接口（保存/心跳/事件/交卷）随后要 409 拒绝写入，同一个事务里的改动会被回滚，
    # 所以调用方必须在**独立的读连接**里先跑一次收卷，让结果先提交落库。
    return len(rows)


def _grade(db, session_id: str) -> float:
    """客观题判分：选择集合与标准答案完全一致才得分（与初级会计客观题口径一致）。"""
    rows = db.execute(
        """SELECT a.question_id, a.picked_json, q.answer_keys_json, q.points
             FROM online_exam_answer a
             JOIN online_exam_question q ON q.id=a.question_id
            WHERE a.session_id=?""", (session_id,)).fetchall()
    total = 0.0
    for row in rows:
        try:
            picked = sorted(json.loads(row["picked_json"] or "[]"))
        except json.JSONDecodeError:
            picked = []
        keys = sorted(json.loads(row["answer_keys_json"]))
        correct = int(bool(picked) and picked == keys)
        points = float(row["points"]) if correct else 0.0
        total += points
        db.execute("UPDATE online_exam_answer SET correct=?, points_gained=? WHERE session_id=? AND question_id=?",
                   (correct, points, session_id, row["question_id"]))
    db.execute("UPDATE online_exam_session SET score=? WHERE id=?", (total, session_id))
    return total


def _ensure_open(db, paper: sqlite3.Row, session: sqlite3.Row, *, moment: str | None = None) -> sqlite3.Row:
    """所有写操作前都过一道：卷关了、到点了就拒绝写入。

    已经交卷/被收卷的会话直接放过，由调用方给出更准确的说法（"已交卷" 还是 "自动收卷"），
    避免这里统一报成"时间到"。
    """
    stamp = moment or now()
    if paper["status"] != "published" or (paper["close_at"] and stamp >= paper["close_at"]):
        raise ExamError("考试已结束，不能再作答。", 409)
    if session["status"] != "in_progress":
        return session
    if _seconds_left(session, moment=stamp) <= 0:
        db.execute("UPDATE online_exam_session SET status='forced', submitted_at=? WHERE id=?",
                   (stamp, session["id"]))
        _grade(db, session["id"])
        raise ExamError("考试时间已到，系统已自动收卷。", 409)
    return session


def _live_session(db, paper: sqlite3.Row, session: sqlite3.Row | None,
                  *, moment: str | None = None) -> sqlite3.Row:
    """写操作统一的会话闸门：先判卷面开关，再判会话是不是还在进行中。"""
    if session is None:
        raise ExamError("当前没有进行中的考试。", 409)
    _ensure_open(db, paper, session, moment=moment)
    if session["status"] != "in_progress":
        raise ExamError("考试时间已到，系统已自动收卷。" if session["status"] == "forced"
                        else "当前没有进行中的考试。", 409)
    return session


def _my_session(db, paper_id: str, student_user_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM online_exam_session WHERE paper_id=? AND student_id=?",
                      (paper_id, student_user_id)).fetchone()


def _ordered_questions(db, paper: sqlite3.Row, session_id: str) -> list[sqlite3.Row]:
    """题目乱序：用会话 id 做种子，保证同一场考试里刷新/重连取卷顺序不变。"""
    rows = db.execute(
        "SELECT * FROM online_exam_question WHERE paper_id=? ORDER BY position", (paper["id"],)).fetchall()
    if paper["shuffle_questions"]:
        rng = random.Random(hashlib.sha256(f"{session_id}:questions".encode("utf-8")).hexdigest())
        rows = list(rows)
        rng.shuffle(rows)
    return rows


def _view_options(row: sqlite3.Row, session_id: str, *, shuffle: bool) -> list[dict[str, Any]]:
    """选项乱序：同样以会话 id 为种子，避免每次取卷选项乱跳。"""
    options = list(json.loads(row["options_json"]))
    if shuffle:
        rng = random.Random(hashlib.sha256(f"{session_id}:options:{row['id']}".encode("utf-8")).hexdigest())
        rng.shuffle(options)
    return options


def list_my_exams(store, student_user_id: str, college: str) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute(
            """SELECT p.* FROM online_exam_paper p
                 JOIN online_exam_roster r ON r.paper_id=p.id
                WHERE r.student_id=? ORDER BY p.created_at DESC""", (student_user_id,)).fetchall()
        out = []
        for row in rows:
            _close_due_sessions(db, row["id"])
            session = _my_session(db, row["id"], student_user_id)
            item = {"id": row["id"], "title": row["title"], "description": row["description"],
                    "duration_minutes": row["duration_minutes"], "open_at": row["open_at"],
                    "close_at": row["close_at"], "status": row["status"], "phase": _phase(row),
                    "session_status": session["status"] if session else None,
                    "remaining_seconds": _seconds_left(session) if session and session["status"] == "in_progress" else None,
                    "submitted_at": session["submitted_at"] if session else None,
                    "score_visible": _score_visible(row, session)}
            out.append(item)
        return out


def _score_visible(paper: sqlite3.Row, session: sqlite3.Row | None) -> bool:
    if not session or session["status"] not in ("submitted", "forced"):
        return False
    policy = paper["answer_release"]
    if policy == "after_submit":
        return True
    if policy == "after_close":
        return paper["status"] == "closed"
    return bool(paper["released_at"])


def start_exam(store, student: dict[str, Any], paper_id: str) -> dict[str, Any]:
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        student_id = _student_user_id(db, student)
        paper = _paper_row(db, paper_id)
        if not db.execute("SELECT 1 FROM online_exam_roster WHERE paper_id=? AND student_id=?",
                          (paper_id, student_id)).fetchone():
            raise ExamError("考试不存在。", 404)
        if paper["status"] != "published":
            raise ExamError("考试尚未开始或已结束。", 409)
        if paper["open_at"] and stamp < paper["open_at"]:
            raise ExamError(f"考试尚未开始（{paper['open_at']} 开始）。", 409)
        if paper["open_at"] and paper["late_entry_minutes"] is not None:
            grace = datetime.fromisoformat(paper["open_at"]) + timedelta(minutes=int(paper["late_entry_minutes"]))
            if datetime.fromisoformat(stamp) > grace:
                raise ExamError("已超过迟到宽限时间，不能进入考试。", 409)
        if paper["close_at"] and stamp >= paper["close_at"]:
            raise ExamError("考试已结束。", 409)
        existing = _my_session(db, paper_id, student_id)
        if existing:
            raise ExamError("你已在作答中，请勿重复进入；刷新页面即可继续答题。", 409)
        started = datetime.fromisoformat(stamp)
        ends = started + timedelta(minutes=int(paper["duration_minutes"]))
        if paper["close_at"]:
            ends = min(ends, datetime.fromisoformat(paper["close_at"]))
        sid = str(uuid.uuid4())
        db.execute(
            """INSERT INTO online_exam_session (id, paper_id, student_id, started_at, ends_at,
                   status, violations, last_heartbeat_at)
               VALUES (?,?,?,?,?,'in_progress',0,?)""",
            (sid, paper_id, student_id, stamp, ends.isoformat(timespec="seconds"), stamp))
        session = _my_session(db, paper_id, student_id)
        return {"session": _session_payload(db, paper, session)}


def _session_payload(db, paper: sqlite3.Row, session: sqlite3.Row) -> dict[str, Any]:
    answered = db.execute("SELECT count(*) n FROM online_exam_answer WHERE session_id=?",
                          (session["id"],)).fetchone()["n"]
    return {"id": session["id"], "paper_id": session["paper_id"], "status": session["status"],
            "started_at": session["started_at"], "ends_at": session["ends_at"],
            "remaining_seconds": _seconds_left(session), "answered": int(answered or 0),
            "violations": int(session["violations"] or 0), "submitted_at": session["submitted_at"]}


def take_paper(store, student: dict[str, Any], paper_id: str) -> dict[str, Any]:
    """取卷：题目不含标准答案与解析（按发布策略闸门，默认发布后才给）。"""
    stamp = now()
    with store.connect() as db:
        student_id = _student_user_id(db, student)
        paper = _paper_row(db, paper_id)
        if not db.execute("SELECT 1 FROM online_exam_roster WHERE paper_id=? AND student_id=?",
                          (paper_id, student_id)).fetchone():
            raise ExamError("考试不存在。", 404)
        _close_due_sessions(db, paper_id, moment=stamp)
        session = _my_session(db, paper_id, student_id)
        if not session:
            raise ExamError("请先开始考试。", 409)
        saved = {}
        if session["status"] == "in_progress":
            _ensure_open(db, paper, session, moment=stamp)
        for row in db.execute(
                "SELECT question_id, picked_json FROM online_exam_answer WHERE session_id=?",
                (session["id"],)).fetchall():
            try:
                saved[row["question_id"]] = json.loads(row["picked_json"] or "[]")
            except json.JSONDecodeError:
                saved[row["question_id"]] = []
        questions = []
        for row in _ordered_questions(db, paper, session["id"]):
            questions.append({
                "id": row["id"], "kind": row["kind"], "stem": row["stem"],
                "options": _view_options(row, session["id"], shuffle=bool(paper["shuffle_options"])),
                "points": row["points"], "picked": saved.get(row["id"], [])})
        return {"paper": {"id": paper["id"], "title": paper["title"], "description": paper["description"],
                          "duration_minutes": paper["duration_minutes"],
                          "question_count": len(questions),
                          "total_points": paper["total_points"]},
                "session": _session_payload(db, paper, session), "questions": questions}


def save_answers(store, student: dict[str, Any], paper_id: str,
                 answers: list[dict[str, Any]]) -> dict[str, Any]:
    stamp = now()
    with store.connect() as db:
        student_id = _student_user_id(db, student)
        if not db.execute("SELECT 1 FROM online_exam_roster WHERE paper_id=? AND student_id=?",
                          (paper_id, student_id)).fetchone():
            raise ExamError("考试不存在。", 404)
        _close_due_sessions(db, paper_id, moment=stamp)  # 先落库：写事务可能被 409 回滚
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        paper = _paper_row(db, paper_id)
        session = _live_session(db, paper, _my_session(db, paper_id, student_id), moment=stamp)
        valid = {r["id"] for r in db.execute(
            "SELECT id FROM online_exam_question WHERE paper_id=?", (paper_id,)).fetchall()}
        saved = 0
        for item in answers:
            qid = str(item.get("question_id") or "").strip()
            if qid not in valid:
                continue
            picked = [str(k).strip() for k in (item.get("picked") or []) if str(k).strip()]
            db.execute(
                """INSERT INTO online_exam_answer (session_id, question_id, picked_json, saved_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(session_id, question_id)
                   DO UPDATE SET picked_json=excluded.picked_json, saved_at=excluded.saved_at""",
                (session["id"], qid, json.dumps(picked, ensure_ascii=False), stamp))
            saved += 1
        return {"saved": saved, "session": _session_payload(db, paper, _my_session(db, paper_id, student_id))}


def submit_exam(store, student: dict[str, Any], paper_id: str) -> dict[str, Any]:
    stamp = now()
    with store.connect() as db:
        student_id = _student_user_id(db, student)
        _close_due_sessions(db, paper_id, moment=stamp)  # 先落库：写事务可能被 409 回滚
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        paper = _paper_row(db, paper_id)
        session = _my_session(db, paper_id, student_id)
        if not session:
            raise ExamError("还没有开始这场考试。", 409)
        if session["status"] != "in_progress":
            raise ExamError("考试时间已到，系统已自动收卷。" if session["status"] == "forced"
                            else "这场考试已经交卷了。", 409)
        db.execute("UPDATE online_exam_session SET status='submitted', submitted_at=? WHERE id=?",
                   (stamp, session["id"]))
        score = _grade(db, session["id"])
        session = _my_session(db, paper_id, student_id)
        return {"score_visible": _score_visible(paper, session), "score": score if _score_visible(paper, session) else None,
                "session": _session_payload(db, paper, session)}


def heartbeat(store, student: dict[str, Any], paper_id: str) -> dict[str, Any]:
    stamp = now()
    with store.connect() as db:
        student_id = _student_user_id(db, student)
        if not db.execute("SELECT 1 FROM online_exam_roster WHERE paper_id=? AND student_id=?",
                          (paper_id, student_id)).fetchone():
            raise ExamError("考试不存在。", 404)
        _close_due_sessions(db, paper_id, moment=stamp)  # 先落库：写事务可能被 409 回滚
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        paper = _paper_row(db, paper_id)
        session = _live_session(db, paper, _my_session(db, paper_id, student_id), moment=stamp)
        db.execute("UPDATE online_exam_session SET last_heartbeat_at=? WHERE id=?", (stamp, session["id"]))
        return {"remaining_seconds": _seconds_left(session), "at": stamp}


NOISE_EVENTS = {"heartbeat", "resume"}


def record_events(store, student: dict[str, Any], paper_id: str,
                  events: list[dict[str, Any]]) -> dict[str, Any]:
    """防作弊事件：只记录，不自动判定。切页/复制之类的计入 violations 供教师参考。"""
    stamp = now()
    with store.connect() as db:
        student_id = _student_user_id(db, student)
        if not db.execute("SELECT 1 FROM online_exam_roster WHERE paper_id=? AND student_id=?",
                          (paper_id, student_id)).fetchone():
            raise ExamError("考试不存在。", 404)
        _close_due_sessions(db, paper_id, moment=stamp)  # 先落库：写事务可能被 409 回滚
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        paper = _paper_row(db, paper_id)
        session = _live_session(db, paper, _my_session(db, paper_id, student_id), moment=stamp)
        added = 0
        for item in events:
            kind = str(item.get("kind") or "").strip()[:40]
            if not kind:
                continue
            at = str(item.get("at") or "").strip() or stamp
            db.execute(
                "INSERT INTO online_exam_event (id, session_id, kind, at, detail_json) VALUES (?,?,?,?,?)",
                (str(uuid.uuid4()), session["id"], kind, at,
                 json.dumps(item.get("detail") or {}, ensure_ascii=False)[:2000]))
            added += 1
            if kind not in NOISE_EVENTS:
                db.execute("UPDATE online_exam_session SET violations=violations+1 WHERE id=?",
                           (session["id"],))
        session = _my_session(db, paper_id, student_id)
        return {"recorded": added, "violations": int(session["violations"] or 0)}


def result_for_student(store, student: dict[str, Any], paper_id: str) -> dict[str, Any]:
    stamp = now()
    with store.connect() as db:
        student_id = _student_user_id(db, student)
        paper = _paper_row(db, paper_id)
        if not db.execute("SELECT 1 FROM online_exam_roster WHERE paper_id=? AND student_id=?",
                          (paper_id, student_id)).fetchone():
            raise ExamError("考试不存在。", 404)
        _close_due_sessions(db, paper_id, moment=stamp)
        session = _my_session(db, paper_id, student_id)
        if not session:
            raise ExamError("还没有参加这场考试。", 404)
        visible = _score_visible(paper, session)
        # 复核分优先：学生看到的应当与成绩单、监考台是同一个数。
        final = session["adjusted_score"] if session["adjusted_score"] is not None else session["score"]
        payload = {"released": visible, "status": session["status"],
                   "submitted_at": session["submitted_at"],
                   "score": float(final) if visible and final is not None else None,
                   "auto_score": float(session["score"]) if visible and session["score"] is not None else None,
                   "reviewed": None if not visible else session["adjusted_score"] is not None,
                   "pass_score": paper["pass_score"],
                   "passed": None if not visible or paper["pass_score"] is None or final is None
                             else float(final) >= float(paper["pass_score"]),
                   "session": _session_payload(db, paper, session), "questions": []}
        if not visible:
            payload["reason"] = ("成绩尚未发布，教师发布后可见。" if paper["answer_release"] == "manual"
                                 else "考试结束后才能查看成绩与解析。")
            return payload
        for row in _ordered_questions(db, paper, session["id"]):
            answer = db.execute(
                "SELECT picked_json, correct, points_gained, manual_score FROM online_exam_answer"
                " WHERE session_id=? AND question_id=?", (session["id"], row["id"])).fetchone()
            try:
                picked = json.loads(answer["picked_json"]) if answer else []
            except json.JSONDecodeError:
                picked = []
            payload["questions"].append({
                "id": row["id"], "kind": row["kind"], "stem": row["stem"],
                "options": json.loads(row["options_json"]), "points": row["points"],
                "picked": picked, "answer_keys": json.loads(row["answer_keys_json"]),
                "explanation": row["explanation"],
                "correct": bool(answer["correct"]) if answer and answer["correct"] is not None else None,
                "points_gained": answer["points_gained"] if answer else None,
                "manual_score": answer["manual_score"] if answer else None,
                "gain": (answer["manual_score"] if answer and answer["manual_score"] is not None
                         else (answer["points_gained"] if answer else None))})
        return payload


# --------------------------------------------------------------------------- 监考台（E2）

def proctor_sessions(store, paper_id: str, college: str) -> dict[str, Any]:
    stamp = now()
    with store.connect() as db:
        paper = _require(db, paper_id, college)
        _close_due_sessions(db, paper_id, moment=stamp)
        rows = db.execute(
            """SELECT r.student_id, r.student_no, r.name, s.id AS session_id, s.status,
                      s.started_at, s.ends_at, s.submitted_at, s.violations, s.last_heartbeat_at,
                      s.score, s.adjusted_score
                 FROM online_exam_roster r
                 LEFT JOIN online_exam_session s ON s.paper_id=r.paper_id AND s.student_id=r.student_id
                WHERE r.paper_id=? ORDER BY r.student_no, r.name""", (paper_id,)).fetchall()
        people = []
        for row in rows:
            answered = 0
            if row["session_id"]:
                answered = db.execute("SELECT count(*) n FROM online_exam_answer WHERE session_id=?",
                                      (row["session_id"],)).fetchone()["n"] or 0
            people.append({
                "student_id": row["student_id"], "student_no": row["student_no"], "name": row["name"],
                "session_id": row["session_id"], "status": row["status"] or "not_started",
                "started_at": row["started_at"], "submitted_at": row["submitted_at"],
                "answered": int(answered), "question_count": db.execute(
                    "SELECT count(*) n FROM online_exam_question WHERE paper_id=?", (paper_id,)).fetchone()["n"],
                "violations": int(row["violations"] or 0), "last_heartbeat_at": row["last_heartbeat_at"],
                "remaining_seconds": (int((datetime.fromisoformat(row["ends_at"]) - datetime.fromisoformat(stamp))
                                          .total_seconds()) if row["ends_at"] and row["status"] == "in_progress" else None),
                # 三个分数各司其职：自动分原样保留，复核后无人工分时为 None，
                # score 是最终生效值（复核分优先）——成绩单与学生端都用这一份口径。
                "auto_score": row["score"],
                "adjusted_score": row["adjusted_score"],
                "score": row["adjusted_score"] if row["adjusted_score"] is not None else row["score"]})
        return {"paper": {"id": paper["id"], "title": paper["title"], "status": paper["status"],
                          "phase": _phase(paper), "duration_minutes": paper["duration_minutes"]},
                "people": people,
                "summary": {"roster": len(people),
                            "in_progress": sum(1 for p in people if p["status"] == "in_progress"),
                            "submitted": sum(1 for p in people if p["status"] in ("submitted", "forced")),
                            "not_started": sum(1 for p in people if p["status"] == "not_started"),
                            "violations": sum(p["violations"] for p in people)}}


def force_submit(store, actor: dict[str, Any], paper_id: str, college: str,
                 student_id: str) -> dict[str, Any]:
    stamp = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require(db, paper_id, college)
        session = _my_session(db, paper_id, student_id)
        if not session:
            raise ExamError("该考生还没有进入考试。", 404)
        if session["status"] != "in_progress":
            raise ExamError("该考生已交卷。", 409)
        db.execute("UPDATE online_exam_session SET status='forced', submitted_at=? WHERE id=?",
                   (stamp, session["id"]))
        score = _grade(db, session["id"])
        return {"student_id": student_id, "status": "forced", "score": score, "submitted_at": stamp}


# --------------------------------------------------------------------------- 判分复核与成绩（E3）

def _session_by_student(db, paper_id: str, student_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM online_exam_session WHERE paper_id=? AND student_id=?",
                     (paper_id, student_id)).fetchone()
    if not row:
        raise ExamError("该考生还没有参加考试。", 404)
    return row


def _recalculate(db, session_id: str) -> dict[str, Any]:
    """复核分重算：每题取人工分优先，没人工分才用自动判分。

    一条人工分都没有时把 adjusted_score 置 NULL——「复核后分数」只有真的复核过
    才存在，否则成绩单上会多出一个和自动分一模一样的数字，反倒让人以为复核过。
    """
    rows = db.execute(
        "SELECT points_gained, manual_score FROM online_exam_answer WHERE session_id=?",
        (session_id,)).fetchall()
    auto = sum(float(r["points_gained"] or 0) for r in rows)
    reviewed = [r for r in rows if r["manual_score"] is not None]
    adjusted = sum(float(r["manual_score"]) if r["manual_score"] is not None else float(r["points_gained"] or 0)
                   for r in rows)
    db.execute("UPDATE online_exam_session SET adjusted_score=? WHERE id=?",
               (round(adjusted, 2) if reviewed else None, session_id))
    return {"auto_score": round(auto, 2), "score": round(adjusted, 2), "reviewed": len(reviewed)}


def session_answers(store, paper_id: str, college: str, student_id: str) -> dict[str, Any]:
    """教师侧的答卷明细：按卷面顺序，带标准答案、考生选项、自动分与人工分。"""
    stamp = now()
    with store.connect() as db:
        paper = _require(db, paper_id, college)
        _close_due_sessions(db, paper_id, moment=stamp)
        session = _session_by_student(db, paper_id, student_id)
        roster = db.execute(
            "SELECT student_no, name FROM online_exam_roster WHERE paper_id=? AND student_id=?",
            (paper_id, student_id)).fetchone()
        answers = {a["question_id"]: a for a in db.execute(
            "SELECT * FROM online_exam_answer WHERE session_id=?", (session["id"],)).fetchall()}
        questions = []
        for row in db.execute("SELECT * FROM online_exam_question WHERE paper_id=? ORDER BY position",
                              (paper_id,)).fetchall():
            answer = answers.get(row["id"])
            try:
                picked = json.loads(answer["picked_json"]) if answer else []
            except json.JSONDecodeError:
                picked = []
            questions.append({
                "id": row["id"], "position": row["position"], "kind": row["kind"], "stem": row["stem"],
                "options": json.loads(row["options_json"]), "answer_keys": json.loads(row["answer_keys_json"]),
                "explanation": row["explanation"], "points": row["points"],
                "picked": picked,
                "answered": answer is not None,
                "correct": None if not answer or answer["correct"] is None else bool(answer["correct"]),
                "points_gained": answer["points_gained"] if answer else None,
                "manual_score": answer["manual_score"] if answer else None,
                "manual_note": answer["manual_note"] if answer else None,
                "manual_by": answer["manual_by"] if answer else None})
        return {"paper": {"id": paper["id"], "title": paper["title"], "status": paper["status"],
                          "pass_score": paper["pass_score"], "total_points": paper["total_points"],
                          "answer_release": paper["answer_release"], "released_at": paper["released_at"]},
                "student": {"student_id": student_id,
                            "student_no": roster["student_no"] if roster else None,
                            "name": roster["name"] if roster else None},
                "session": {"id": session["id"], "status": session["status"],
                            "auto_score": session["score"], "adjusted_score": session["adjusted_score"],
                            "score": (session["adjusted_score"] if session["adjusted_score"] is not None
                                      else session["score"]),
                            "submitted_at": session["submitted_at"], "violations": session["violations"]},
                "questions": questions}


def review_answer(store, actor: dict[str, Any], paper_id: str, college: str, student_id: str,
                  question_id: str, *, score: float | None, note: str | None = None) -> dict[str, Any]:
    """单题复核改分。score=None 撤销人工分，回到自动判分。

    只改这一题的得分并重算整卷，不动 `score`（自动分）与任何其他考生的数据。
    """
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require(db, paper_id, college)
        session = _session_by_student(db, paper_id, student_id)
        if session["status"] == "in_progress":
            raise ExamError("该考生还在作答，交卷后才能复核。", 409)
        question = db.execute("SELECT points, position FROM online_exam_question WHERE id=? AND paper_id=?",
                              (question_id, paper_id)).fetchone()
        if not question:
            raise ExamError("题目不存在。", 404)
        if not db.execute("SELECT 1 FROM online_exam_answer WHERE session_id=? AND question_id=?",
                          (session["id"], question_id)).fetchone():
            raise ExamError("这份答卷没有这道题的作答记录，无法复核。", 404)
        if score is not None:
            top = float(question["points"])
            if not 0 <= float(score) <= top:
                raise ExamError(f"复核分须在 0–{top:g} 之间（本题满分 {top:g}）。", 422)
        db.execute(
            """UPDATE online_exam_answer SET manual_score=?, manual_by=?, manual_note=?
                WHERE session_id=? AND question_id=?""",
            (None if score is None else float(score), None if score is None else actor["id"],
             _clean(note, 500), session["id"], question_id))
        totals = _recalculate(db, session["id"])
        db.execute("INSERT INTO online_exam_event VALUES (?,?,?,?,?)",
                   (str(uuid.uuid4()), session["id"], "review", now(),
                    json.dumps({"question_id": question_id, "position": question["position"],
                                "score": score, "by": actor["id"]}, ensure_ascii=False)))
        return {"student_id": student_id, "question_id": question_id, "manual_score": score,
                "manual_note": _clean(note, 500), **totals}


def release_scores(store, actor: dict[str, Any], paper_id: str, college: str, *,
                   release: bool) -> dict[str, Any]:
    """成绩发布/撤回（`answer_release='manual'` 的开关）。

    发布前必须先结束考试：否则考生交卷即见分，同场还在作答的人会从别人嘴里听到
    答案与解析。这里把顺序卡死，不用"提醒教师"的方式兜。
    """
    stamp = now()
    with store.connect() as db:
        # 惰性收卷在独立读连接里先落库（与 E2 同一纪律：写事务里的 409 会连带回滚收卷）
        _close_due_sessions(db, paper_id, moment=stamp)
        db.execute("BEGIN IMMEDIATE")
        paper = _require(db, paper_id, college)
        if release:
            if paper["status"] not in ("closed", "archived"):
                raise ExamError("先结束考试再发布成绩，避免考生边考边看到分数与解析。", 409)
            db.execute("UPDATE online_exam_paper SET released_at=?, released_by=?, updated_at=? WHERE id=?",
                       (stamp, actor["id"], stamp, paper_id))
        else:
            db.execute("UPDATE online_exam_paper SET released_at=NULL, released_by=NULL, updated_at=? WHERE id=?",
                       (stamp, paper_id))
        return _payload(db, _paper_row(db, paper_id), with_questions=False)


def scores_csv(store, paper_id: str, college: str) -> tuple[str, bytes]:
    """成绩单 CSV：UTF-8 BOM，前几行写明场次与口径，Excel 双击即开。"""
    stamp = now()
    with store.connect() as db:
        paper = _require(db, paper_id, college)
        _close_due_sessions(db, paper_id, moment=stamp)
        rows = db.execute(
            """SELECT r.student_id, r.student_no, r.name, s.status, s.score, s.adjusted_score,
                      s.submitted_at, s.violations
                 FROM online_exam_roster r
                 LEFT JOIN online_exam_session s ON s.paper_id=r.paper_id AND s.student_id=r.student_id
                WHERE r.paper_id=? ORDER BY r.student_no, r.name""", (paper_id,)).fetchall()
    status_label = {"submitted": "已交卷", "forced": "自动收卷", "in_progress": "作答中"}
    pass_score = paper["pass_score"]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["# 在线考试成绩单"])
    writer.writerow([f"# 考试：{paper['title']} · 状态 {paper['status']} · 满分 {float(paper['total_points']):g}"
                     + (f" · 及格线 {float(pass_score):g}" if pass_score is not None else " · 未设及格线")])
    writer.writerow([f"# 导出时间 {datetime.now(UTC).astimezone().isoformat(timespec='seconds')}"
                     f" · 名单 {len(rows)} 人 · 成绩发布 "
                     + (f"于 {paper['released_at']}" if paper["released_at"] else "未发布")])
    writer.writerow(["# 口径：最终成绩 = 复核后分数优先，未复核用自动判分；空分表示未参加或未交卷"])
    writer.writerow(["学号", "姓名", "状态", "自动判分", "复核后分数", "最终成绩", "是否及格", "交卷时间", "违规次数"])
    for row in rows:
        final = row["adjusted_score"] if row["adjusted_score"] is not None else row["score"]
        if final is None or pass_score is None:
            passed = ""
        else:
            passed = "是" if float(final) >= float(pass_score) else "否"
        writer.writerow([row["student_no"] or "", row["name"] or "",
                         status_label.get(row["status"], "未参加"),
                         "" if row["score"] is None else row["score"],
                         "" if row["adjusted_score"] is None else row["adjusted_score"],
                         "" if final is None else final, passed,
                         row["submitted_at"] or "", int(row["violations"] or 0)])
    safe = "".join(ch for ch in paper["title"] if ch not in '\\/:*?"<>|').strip() or "exam"
    return f"{safe}-成绩单.csv", ("\ufeff" + buffer.getvalue()).encode("utf-8")


# --------------------------------------------------------------------------- 路由

class PaperCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    college: str = Field(min_length=1)
    description: str | None = None
    course_id: str | None = None
    duration_minutes: int = 60
    open_at: str | None = None
    close_at: str | None = None
    late_entry_minutes: int = 0
    shuffle_questions: bool = False
    shuffle_options: bool = False
    pass_score: float | None = None
    answer_release: str = "manual"


class PaperPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    title: str | None = None
    description: str | None = None
    course_id: str | None = None


class QuestionsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    questions: list[dict[str, Any]] = Field(default_factory=list)


class BankPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    question_ids: list[str] = Field(default_factory=list)


class RosterPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    class_ids: list[str] = Field(default_factory=list)
    student_ids: list[str] = Field(default_factory=list)
    student_nos: list[str] = Field(default_factory=list)


class AnswersPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[dict[str, Any]] = Field(default_factory=list)


class EventsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[dict[str, Any]] = Field(default_factory=list)


class ReviewScore(BaseModel):
    """单题复核：score 为 null 即撤销人工分（回到自动判分）。"""
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=500)


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    college: str = Field(min_length=1)
    release: bool = True


def register(app, store_provider, staff_cookie: str = "taxpearls_staff_session",
             student_cookie: str = "taxpearls_training_session") -> None:
    def response(value: Any) -> JSONResponse:
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    def guarded(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except ExamError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from None
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="操作与现有数据冲突。") from None
        return wrapper

    def actor(session: str | None, college: str | None = None) -> dict[str, Any]:
        from webapp.training_portal import staff_for_token
        who = staff_for_token(store_provider(), session)
        if not who:
            raise HTTPException(status_code=401, detail="请先使用教师或管理员账号登录。")
        if college is not None and college != who["college"]:
            raise HTTPException(status_code=403, detail="只能管理本院校的考试。")
        return who

    def learner(session: str | None) -> dict[str, Any]:
        from webapp.training_portal import student_for_token
        who = student_for_token(store_provider(), session)
        if not who:
            raise HTTPException(status_code=401, detail="请先使用学号登录。")
        return who

    @app.get("/api/training/exams/papers")
    @guarded
    def online_exam_paper_list(college: str, include_archived: bool = False,
                        session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, college)
        return response({"papers": list_papers(
            store_provider(), who["college"], include_archived=include_archived)})

    @app.post("/api/training/exams/papers")
    @guarded
    def online_exam_paper_create(body: PaperCreate,
                          session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, body.college)
        payload = body.model_dump(exclude={"college"})
        return response({"paper": create_paper(store_provider(), who, body.college, **payload)})

    @app.get("/api/training/exams/papers/bank-questions")
    @guarded
    def exam_bank_questions(college: str, certificate_id: str | None = None, limit: int = 50,
                            session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        actor(session, college)
        return response({"questions": bank_questions(
            store_provider(), certificate_id=certificate_id, limit=limit)})

    @app.get("/api/training/exams/papers/{paper_id}")
    @guarded
    def online_exam_paper_detail(paper_id: str, college: str,
                          session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        actor(session, college)
        return response({"paper": paper_detail(store_provider(), paper_id, college)})

    @app.patch("/api/training/exams/papers/{paper_id}")
    @guarded
    def online_exam_paper_patch(paper_id: str, body: PaperPatch,
                         session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, body.college)
        fields = {k: v for k, v in body.model_dump(exclude={"college"}).items() if v is not None}
        if not fields:
            raise ExamError("没有要修改的字段。")
        return response({"paper": update_paper(store_provider(), who, paper_id, body.college, fields)})

    @app.put("/api/training/exams/papers/{paper_id}/questions")
    @guarded
    def exam_questions_put(paper_id: str, body: QuestionsPut,
                           session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, body.college)
        return response({"paper": replace_questions(
            store_provider(), who, paper_id, body.college, body.questions)})

    @app.post("/api/training/exams/papers/{paper_id}/questions/from-bank")
    @guarded
    def exam_questions_from_bank(paper_id: str, body: BankPick,
                                 session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, body.college)
        return response({"paper": add_bank_questions(
            store_provider(), who, paper_id, body.college, body.question_ids)})

    @app.put("/api/training/exams/papers/{paper_id}/roster")
    @guarded
    def online_exam_roster_put(paper_id: str, body: RosterPut,
                        session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, body.college)
        return response({"roster": replace_roster(
            store_provider(), who, paper_id, body.college, class_ids=body.class_ids,
            student_ids=body.student_ids, student_nos=body.student_nos)})

    @app.post("/api/training/exams/papers/{paper_id}/publish")
    @guarded
    def exam_publish(paper_id: str, college: str,
                     session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, college)
        return response({"paper": publish(store_provider(), who, paper_id, college)})

    @app.post("/api/training/exams/papers/{paper_id}/close")
    @guarded
    def exam_close(paper_id: str, college: str,
                   session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, college)
        return response({"paper": close(store_provider(), who, paper_id, college)})

    # ---- 监考台（教师） ----

    @app.get("/api/training/exams/papers/{paper_id}/sessions")
    @guarded
    def exam_proctor(paper_id: str, college: str,
                     session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        actor(session, college)
        return response(proctor_sessions(store_provider(), paper_id, college))

    @app.post("/api/training/exams/papers/{paper_id}/sessions/{student_id}/force-submit")
    @guarded
    def exam_force_submit(paper_id: str, student_id: str, college: str,
                          session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, college)
        return response(force_submit(store_provider(), who, paper_id, college, student_id))

    # ---- 判分复核与成绩（E3，教师） ----

    @app.get("/api/training/exams/papers/{paper_id}/sessions/{student_id}/answers")
    @guarded
    def exam_session_answers(paper_id: str, student_id: str, college: str,
                             session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        actor(session, college)
        return response(session_answers(store_provider(), paper_id, college, student_id))

    @app.post("/api/training/exams/papers/{paper_id}/sessions/{student_id}/answers/{question_id}/review")
    @guarded
    def exam_review_answer(paper_id: str, student_id: str, question_id: str, body: ReviewScore,
                           session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, body.college)
        return response(review_answer(store_provider(), who, paper_id, body.college, student_id, question_id,
                                      score=body.score, note=body.note))

    @app.post("/api/training/exams/papers/{paper_id}/release")
    @guarded
    def exam_release(paper_id: str, body: ReleaseBody,
                     session: str | None = Cookie(default=None, alias=staff_cookie)) -> JSONResponse:
        who = actor(session, body.college)
        return response(release_scores(store_provider(), who, paper_id, body.college, release=body.release))

    @app.get("/api/training/exams/papers/{paper_id}/scores.csv")
    @guarded
    def exam_scores_csv(paper_id: str, college: str,
                        session: str | None = Cookie(default=None, alias=staff_cookie)):
        actor(session, college)
        filename, payload = scores_csv(store_provider(), paper_id, college)
        return Response(payload, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="exam-scores.csv"; '
                                                        f"filename*=UTF-8''{quote(filename)}",
                                 "Cache-Control": "private, no-store"})

    # ---- 学生机考 ----

    @app.get("/api/training/exams/my")
    @guarded
    def exam_my(session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        with store_provider().connect() as db:
            student_id = _student_user_id(db, who)
        return response({"exams": list_my_exams(store_provider(), student_id, who.get("college") or "")})

    @app.post("/api/training/exams/{paper_id}/start")
    @guarded
    def exam_start(paper_id: str,
                   session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        return response(start_exam(store_provider(), who, paper_id))

    @app.get("/api/training/exams/{paper_id}/paper")
    @guarded
    def exam_take(paper_id: str,
                  session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        return response(take_paper(store_provider(), who, paper_id))

    @app.post("/api/training/exams/{paper_id}/answers")
    @guarded
    def exam_answers(paper_id: str, body: AnswersPut,
                     session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        return response(save_answers(store_provider(), who, paper_id, body.answers))

    @app.post("/api/training/exams/{paper_id}/submit")
    @guarded
    def exam_submit(paper_id: str,
                    session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        return response(submit_exam(store_provider(), who, paper_id))

    @app.post("/api/training/exams/{paper_id}/heartbeat")
    @guarded
    def exam_heartbeat(paper_id: str,
                       session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        return response(heartbeat(store_provider(), who, paper_id))

    @app.post("/api/training/exams/{paper_id}/events")
    @guarded
    def exam_events(paper_id: str, body: EventsPut,
                    session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        return response(record_events(store_provider(), who, paper_id, body.events))

    @app.get("/api/training/exams/{paper_id}/result")
    @guarded
    def exam_result(paper_id: str,
                    session: str | None = Cookie(default=None, alias=student_cookie)) -> JSONResponse:
        who = learner(session)
        return response(result_for_student(store_provider(), who, paper_id))
