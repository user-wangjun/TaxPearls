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

E1 只到这里：**出卷与发布**。E2 做学生机考（会话/限时/收卷/监考台），E3 做判分与成绩发布。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from fastapi import Cookie, HTTPException
from fastapi.responses import JSONResponse
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


def register(app, store_provider, staff_cookie: str = "taxpearls_staff_session") -> None:
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
