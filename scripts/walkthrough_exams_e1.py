"""独立在线考试 E1（出卷与发布冻结）· 真库浏览器走查。

验证四件事：考试**完全旁路作业主链**也能建得起来；题目发布即冻结（题库改了卷面不动）；
名单按学号导入且"查无此人/未关联"要报出来；发布有硬门槛（无题、空名单都拦）。

用法（先起服务，默认连 8003 以免打扰常驻的 8000/8001/8002）：
    python -B -X utf8 scripts/walkthrough_exams_e1.py [--base http://127.0.0.1:8003]

会写库：新建一门「走查临时考试-<时间>」；脚本结束前自动清理（只删名称前缀匹配的卷，
连带它的题目、名单、会话、答案、事件一起删；不涉及其它考试与任何作业/提交数据）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []
TEMP_PREFIX = "走查临时考试-"


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" · " + detail) if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("WALK_BASE", "http://127.0.0.1:8003"))
    parser.add_argument("--teacher", default="staff01")
    parser.add_argument("--teacher-password", default=os.environ.get("WALK_TEACHER_PW", "Teacher#2026x"))
    parser.add_argument("--db", default=os.environ.get("TAXPEARLS_DB", ""),
                        help="真库路径；给了就清理走查临时考试，不留脏数据")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    from playwright.sync_api import sync_playwright

    js_errors, http_errors = [], []
    stamp = time.strftime("%H%M%S")
    title = f"{TEMP_PREFIX}{stamp}"

    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1500, "height": 950})
        page = context.new_page()
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on("response", lambda resp: http_errors.append(f"{resp.status} {resp.url}")
                if resp.status >= 400 and resp.status not in (401, 422, 409, 403, 404) else None)

        page.goto(base + "/", wait_until="domcontentloaded")
        page.evaluate("""async ({u, p}) => {
            const r = await fetch('/api/training/staff/auth/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!r.ok) throw new Error('staff login failed: ' + r.status);
        }""", {"u": args.teacher, "p": args.teacher_password})
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(800)

        def api(method: str, path: str, body=None, *, raw=False):
            return page.evaluate("""async ({method, path, body, raw}) => {
                const options = {method, headers: {'Content-Type': 'application/json'}};
                if (body !== null) options.body = JSON.stringify(body);
                const r = await fetch(path, options);
                const text = await r.text();
                return raw ? {status: r.status, text} : {status: r.status, json: text ? JSON.parse(text) : null};
            }""", {"method": method, "path": path, "body": body, "raw": raw})

        college = page.evaluate("""async () => {
            const r = await fetch('/api/training/staff/students');
            const data = await r.json();
            const s = (data.students || []).find(x => x.active);
            return s ? s.college : null;
        }""")
        check("拿得到本院校信息", bool(college), str(college))

        # ---- 1. 建卷（草稿）
        created = api("POST", "/api/training/exams/papers", {
            "title": title, "college": college, "duration_minutes": 45,
            "description": "E1 走查用，跑完自动清理", "pass_score": 60, "answer_release": "manual"})
        check("建卷成功（不经 assignments）", created["status"] == 200, created.get("text", "")[:160])
        paper = (created.get("json") or {}).get("paper") or {}
        paper_id = paper.get("id")
        check("新卷是草稿且可编辑", paper.get("status") == "draft" and paper.get("can_edit"),
              str({k: paper.get(k) for k in ("status", "phase", "can_edit", "duration_minutes")}))
        check("时长与及格线落库", paper.get("duration_minutes") == 45 and paper.get("pass_score") == 60)

        # ---- 2. 发布门槛：无题目、名单为空都要拦
        empty_publish = api("POST", f"/api/training/exams/papers/{paper_id}/publish?college={college}")
        check("没题目不能发布", empty_publish["status"] == 422,
              (empty_publish.get("json") or {}).get("detail", ""))
        put = api("PUT", f"/api/training/exams/papers/{paper_id}/questions", {
            "college": college,
            "questions": [
                {"kind": "single", "stem": "增值税一般纳税人销售货物适用税率是？",
                 "options": [{"key": "A", "text": "13%"}, {"key": "B", "text": "6%"}],
                 "answer_keys": ["A"], "points": 2},
                {"kind": "multiple", "stem": "下列行为属于视同销售的有？",
                 "options": [{"key": "A", "text": "自产货物用于集体福利"},
                             {"key": "B", "text": "购进货物用于免税项目"},
                             {"key": "C", "text": "自产货物无偿赠送"}],
                 "answer_keys": ["A", "C"], "points": 4}]})
        check("题目入卷（两种题型）", put["status"] == 200 and
              len(((put.get("json") or {}).get("paper") or {}).get("questions", [])) == 2,
              str(put.get("status")))
        bad_question = api("PUT", f"/api/training/exams/papers/{paper_id}/questions", {
            "college": college,
            "questions": [{"kind": "single", "stem": "答案不在选项里",
                           "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}],
                           "answer_keys": ["Z"], "points": 1}]})
        check("非法题目被拦（答案不在选项）", bad_question["status"] == 422,
              (bad_question.get("json") or {}).get("detail", ""))
        no_roster = api("POST", f"/api/training/exams/papers/{paper_id}/publish?college={college}")
        check("名单为空不能发布", no_roster["status"] == 422, (no_roster.get("json") or {}).get("detail", ""))

        # ---- 3. 名单：真学号 + 查无此学号
        nos = page.evaluate("""async () => {
            const r = await fetch('/api/training/staff/students?include_inactive=false');
            const data = await r.json();
            return (data.students || []).slice(0, 3).map(s => s.student_no);
        }""")
        roster = api("PUT", f"/api/training/exams/papers/{paper_id}/roster", {
            "college": college, "student_nos": [*nos, "00000000"]})
        body = (roster.get("json") or {}).get("roster") or {}
        check("名单导入成功", roster["status"] == 200 and body.get("enrolled", 0) >= 1,
              f"enrolled={body.get('enrolled')} unresolved={len(body.get('unresolved') or [])}")
        unresolved = {item.get("student_no"): item.get("reason") for item in (body.get("unresolved") or [])}
        check("查无此学号被报出来", "00000000" in unresolved, str(unresolved.get("00000000")))
        reconfirm = api("GET", f"/api/training/exams/papers/{paper_id}?college={college}")
        detail = (reconfirm.get("json") or {}).get("paper") or {}
        check("详情里名单与题目都在", detail.get("roster_count", 0) >= 1 and detail.get("question_count") == 2,
              str({k: detail.get(k) for k in ("roster_count", "question_count", "total_points")}))

        # ---- 4. 发布并冻结
        published = api("POST", f"/api/training/exams/papers/{paper_id}/publish?college={college}")
        check("发布成功", published["status"] == 200, published.get("text", "")[:160])
        pub = (published.get("json") or {}).get("paper") or {}
        fingerprint, snapshot = pub.get("fingerprint"), pub.get("questions")
        check("发布后状态与阶段正确", pub.get("status") == "published" and pub.get("phase") in ("open", "scheduled"),
              f"{pub.get('status')}/{pub.get('phase')}")
        check("卷面已冻结（不可改）", pub.get("can_edit") is False)
        check("总分=题目分值合计", abs((pub.get("total_points") or 0) - 6) < 1e-6, str(pub.get("total_points")))

        frozen = api("PUT", f"/api/training/exams/papers/{paper_id}/questions", {
            "college": college,
            "questions": [{"kind": "single", "stem": "想偷偷改题", "options":
                [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}], "answer_keys": ["A"], "points": 1}]})
        check("已发布卷不能改题目", frozen["status"] == 409, (frozen.get("json") or {}).get("detail", ""))
        again = api("GET", f"/api/training/exams/papers/{paper_id}?college={college}")
        after = (again.get("json") or {}).get("paper") or {}
        check("发布的快照没被改动（题目与指纹一致）",
              after.get("fingerprint") == fingerprint and after.get("questions") == snapshot)

        # ---- 5. 结束与列表
        closed = api("POST", f"/api/training/exams/papers/{paper_id}/close?college={college}")
        check("可提前结束考试", closed["status"] == 200, str(closed.get("status")))
        listing = api("GET", f"/api/training/exams/papers?college={college}")
        papers = (listing.get("json") or {}).get("papers") or []
        mine = next((p for p in papers if p.get("id") == paper_id), None)
        check("列表里能看到这门考试", mine is not None and mine.get("phase") == "closed",
              str(mine and mine.get("phase")))

        check("页面无 JS 报错", not js_errors, "; ".join(js_errors[:2]))
        check("无未预期服务端报错", not http_errors, "; ".join(http_errors[:3]))
        browser.close()

    if args.db and Path(args.db).exists():
        import sqlite3
        conn = sqlite3.connect(args.db)
        try:
            with conn:
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM online_exam_paper WHERE title LIKE ?", (TEMP_PREFIX + "%",))]
                for pid in ids:
                    conn.execute(
                        """DELETE FROM online_exam_answer WHERE session_id IN
                           (SELECT id FROM online_exam_session WHERE paper_id=?)""", (pid,))
                    conn.execute(
                        """DELETE FROM online_exam_event WHERE session_id IN
                           (SELECT id FROM online_exam_session WHERE paper_id=?)""", (pid,))
                    for table in ("online_exam_session", "online_exam_roster", "online_exam_question"):
                        conn.execute(f"DELETE FROM {table} WHERE paper_id=?", (pid,))
                    conn.execute("DELETE FROM online_exam_paper WHERE id=?", (pid,))
                print(f"清理走查临时考试 {len(ids)} 门（仅限标题前缀 {TEMP_PREFIX}）")
        finally:
            conn.close()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
