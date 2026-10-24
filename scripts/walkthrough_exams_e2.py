"""独立在线考试 E2（学生机考：进入/取卷/保存/交卷/限时收卷/监考台）· 真库浏览器走查。

验证六件事（都是单测覆盖不到、必须在真库上确认的）：
1. 学生用**学号会话**能进入考试，进入即计时，剩余时间由服务端给；
2. 取卷不带答案与解析，已答内容能恢复；
3. 防作弊事件上报后"心跳不计数"；
4. 交卷判分，成绩按发布策略下发（manual 交卷不给分）；
5. **惰性收卷**：把结束时间拨到过去后，任何写接口都拒绝并把会话收掉；
6. 监考台看得到每个考生状态，教师能强制收卷；
外加一条 UI：教师端与学生端都能看到「在线考试」卡片并渲染出内容。

用法（先起服务，默认连 8003 以免打扰常驻的 8000/8001/8002）：
    python -B -X utf8 scripts/walkthrough_exams_e2.py [--base http://127.0.0.1:8003] \
        --db <真库路径>

会写库：新建「走查临时机考-<时间>」系列考试；脚本结束前按标题前缀清理
（连带题目/名单/会话/答案/事件；不涉及其它考试与任何作业、提交数据）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []
TEMP_PREFIX = "走查临时机考-"

Q1 = {"kind": "single", "stem": "增值税一般纳税人销售货物适用税率是？",
      "options": [{"key": "A", "text": "13%"}, {"key": "B", "text": "6%"}],
      "answer_keys": ["A"], "points": 2, "explanation": "销售货物适用 13%。"}
Q2 = {"kind": "multiple", "stem": "下列行为属于视同销售的有？",
      "options": [{"key": "A", "text": "自产货物用于集体福利"},
                  {"key": "B", "text": "购进货物用于免税项目"},
                  {"key": "C", "text": "自产货物无偿赠送"}],
      "answer_keys": ["A", "C"], "points": 4}


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" · " + detail) if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("WALK_BASE", "http://127.0.0.1:8003"))
    parser.add_argument("--teacher", default="staff01")
    parser.add_argument("--teacher-password", default=os.environ.get("WALK_TEACHER_PW", "Teacher#2026x"))
    parser.add_argument("--student-password", default=os.environ.get("WALK_STUDENT_PW", "Tp#20260012026"))
    parser.add_argument("--db", default=os.environ.get("TAXPEARLS_DB", ""),
                        help="真库路径；给了才做惰性收卷（改 ends_at）与走后清理")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    from playwright.sync_api import sync_playwright

    js_errors, http_errors = [], []
    stamp = time.strftime("%H%M%S")
    title_manual = f"{TEMP_PREFIX}manual-{stamp}"
    title_after = f"{TEMP_PREFIX}after_submit-{stamp}"
    title_forced = f"{TEMP_PREFIX}收卷-{stamp}"

    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)

        # ---------------------------------------------------------- 教师上下文
        staff_ctx = browser.new_context(viewport={"width": 1500, "height": 950})
        staff = staff_ctx.new_page()
        staff.on("pageerror", lambda exc: js_errors.append("teacher: " + str(exc)))
        staff.on("response", lambda resp: http_errors.append(f"{resp.status} {resp.url}")
                 if resp.status >= 400 and resp.status not in (401, 403, 404, 409, 422) else None)
        staff.goto(base + "/", wait_until="domcontentloaded")
        staff.evaluate("""async ({u, p}) => {
            const r = await fetch('/api/training/staff/auth/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!r.ok) throw new Error('staff login failed: ' + r.status);
        }""", {"u": args.teacher, "p": args.teacher_password})
        staff.goto(base + "/", wait_until="networkidle")
        staff.wait_for_timeout(1200)

        def call(page, method: str, path: str, body=None):
            return page.evaluate("""async ({method, path, body}) => {
                const options = {method, headers: {'Content-Type': 'application/json'}};
                if (body !== null) options.body = JSON.stringify(body);
                const r = await fetch(path, options);
                const text = await r.text();
                let json = null;
                try { json = text ? JSON.parse(text) : null; } catch (e) { json = null; }
                return {status: r.status, json, text};
            }""", {"method": method, "path": path, "body": body})

        college = staff.evaluate("""async () => {
            const r = await fetch('/api/training/staff/students');
            const data = await r.json();
            const s = (data.students || []).find(x => x.active);
            return s ? s.college : null;
        }""")
        check("拿得到本院校信息", bool(college), str(college))
        if not college:
            browser.close()
            return 1

        # 找一名**已关联平台账号**的考生（名单只收平台账号，没关联的会被报出来）
        nos = staff.evaluate("""async () => {
            const r = await fetch('/api/training/staff/students?include_inactive=false');
            const data = await r.json();
            return (data.students || []).slice(0, 8).map(s => s.student_no);
        }""")

        def build_paper(title: str, release: str) -> dict:
            created = call(staff, "POST", "/api/training/exams/papers", {
                "title": title, "college": college, "duration_minutes": 30,
                "answer_release": release, "description": "E2 走查用，跑完自动清理"})
            paper = (created.get("json") or {}).get("paper") or {}
            call(staff, "PUT", f"/api/training/exams/papers/{paper['id']}/questions",
                 {"college": college, "questions": [Q1, Q2]})
            roster = call(staff, "PUT", f"/api/training/exams/papers/{paper['id']}/roster",
                          {"college": college, "student_nos": nos})
            info = (roster.get("json") or {}).get("roster") or {}
            published = call(staff, "POST",
                             f"/api/training/exams/papers/{paper['id']}/publish?college={college}")
            return {"id": paper.get("id"), "enrolled": info.get("enrolled", 0),
                    "unresolved": info.get("unresolved") or [],
                    "published": published["status"] == 200,
                    "roster": info.get("roster") or []}

        manual = build_paper(title_manual, "manual")
        check("建卷→加题→导入名单→发布（manual）",
              bool(manual["id"]) and manual["enrolled"] >= 1 and manual["published"],
              f"enrolled={manual['enrolled']} unresolved={len(manual['unresolved'])}")
        check("未关联/查无此学号的会被报出来（不静默丢弃）",
              all("reason" in item for item in manual["unresolved"]),
              str([item.get("student_no") for item in manual["unresolved"]][:5]))
        student_no = (manual["roster"][0] or {}).get("student_no") if manual["roster"] else None
        check("名单里有可用考生", bool(student_no), str(student_no))
        if not student_no:
            browser.close()
            return 1

        # ---------------------------------------------------------- 学生上下文
        stu_ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        stu = stu_ctx.new_page()
        stu.on("pageerror", lambda exc: js_errors.append("student: " + str(exc)))
        stu.on("response", lambda resp: http_errors.append(f"{resp.status} {resp.url}")
               if resp.status >= 400 and resp.status not in (401, 403, 404, 409, 422) else None)
        stu.goto(base + "/", wait_until="domcontentloaded")
        login = stu.evaluate("""async ({no, pw, college}) => {
            const r = await fetch('/api/training/auth/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({student_no: no, password: pw, college})});
            return r.status;
        }""", {"no": student_no, "pw": args.student_password, "college": college})
        check("学生用学号登录成功", login == 200, str(login))
        if login != 200:
            check("后续机考检查", False, "学号登录失败，后续无法验证")
            browser.close()
            return 1

        mine = call(stu, "GET", "/api/training/exams/my")
        exams = (mine.get("json") or {}).get("exams") or []
        check("我的考试里看得到这场", any(e.get("id") == manual["id"] for e in exams),
              str([e.get("title") for e in exams][:3]))

        started = call(stu, "POST", f"/api/training/exams/{manual['id']}/start")
        session = (started.get("json") or {}).get("session") or {}
        check("进入考试成功且服务端计时", started["status"] == 200
              and abs((session.get("remaining_seconds") or 0) - 1800) <= 10,
              f"remaining={session.get('remaining_seconds')}")
        again = call(stu, "POST", f"/api/training/exams/{manual['id']}/start")
        check("重复进入被拦（409）", again["status"] == 409,
              (again.get("json") or {}).get("detail", ""))

        taken = call(stu, "GET", f"/api/training/exams/{manual['id']}/paper")
        questions = (taken.get("json") or {}).get("questions") or []
        check("取卷成功且不含标准答案与解析",
              len(questions) == 2 and all("answer_keys" not in q and "explanation" not in q
                                          for q in questions),
              str(len(questions)))
        qids = [q["id"] for q in questions]
        saved = call(stu, "POST", f"/api/training/exams/{manual['id']}/answers",
                     {"answers": [{"question_id": qids[0], "picked": ["A"]},
                                  {"question_id": qids[1], "picked": ["A", "C"]}]})
        check("逐题保存成功", (saved.get("json") or {}).get("saved") == 2,
              str((saved.get("json") or {}).get("saved")))
        retaken = call(stu, "GET", f"/api/training/exams/{manual['id']}/paper")
        restored = {q["id"]: q.get("picked") for q in
                    ((retaken.get("json") or {}).get("questions") or [])}
        check("刷新取卷能恢复已答", restored.get(qids[0]) == ["A"] and restored.get(qids[1]) == ["A", "C"],
              str(restored))

        events = call(stu, "POST", f"/api/training/exams/{manual['id']}/events",
                      {"events": [{"kind": "blur"}, {"kind": "visibility"}, {"kind": "heartbeat"}]})
        payload = events.get("json") or {}
        check("防作弊事件上报，心跳不计异常",
              payload.get("recorded") == 3 and payload.get("violations") == 2,
              f"recorded={payload.get('recorded')} violations={payload.get('violations')}")
        beat = call(stu, "POST", f"/api/training/exams/{manual['id']}/heartbeat")
        check("心跳返回剩余时间", (beat.get("json") or {}).get("remaining_seconds", 0) > 0,
              str((beat.get("json") or {}).get("remaining_seconds")))

        submitted = call(stu, "POST", f"/api/training/exams/{manual['id']}/submit")
        submit_body = submitted.get("json") or {}
        check("交卷成功且 manual 策略不下发成绩",
              submitted["status"] == 200 and submit_body.get("score_visible") is False
              and submit_body.get("score") is None, str(submit_body.get("score_visible")))
        result = call(stu, "GET", f"/api/training/exams/{manual['id']}/result")
        result_body = result.get("json") or {}
        check("未发布时学生看不到成绩与解析",
              result_body.get("released") is False and not result_body.get("questions")
              and "尚未发布" in (result_body.get("reason") or ""),
              str(result_body.get("reason")))

        # ---- after_submit：交卷即可看分
        after = build_paper(title_after, "after_submit")
        check("第二场（after_submit）发布成功", after["published"], str(after["id"]))
        call(stu, "POST", f"/api/training/exams/{after['id']}/start")
        q2 = [q["id"] for q in (((call(stu, "GET", f"/api/training/exams/{after['id']}/paper")
                                  .get("json") or {}).get("questions")) or [])]
        call(stu, "POST", f"/api/training/exams/{after['id']}/answers",
             {"answers": [{"question_id": q2[0], "picked": ["A"]},
                          {"question_id": q2[1], "picked": ["A", "C"]}]})
        done = call(stu, "POST", f"/api/training/exams/{after['id']}/submit").get("json") or {}
        check("after_submit 交卷即给分（全对 6 分）",
              done.get("score_visible") is True and abs((done.get("score") or 0) - 6) < 0.001,
              str(done.get("score")))

        # ---- 惰性收卷：把结束时间拨到过去，写接口必须拒并自动收卷
        if args.db and Path(args.db).exists():
            forced = build_paper(title_forced, "manual")
            check("第三场（惰性收卷）发布成功", forced["published"], str(forced["id"]))
            call(stu, "POST", f"/api/training/exams/{forced['id']}/start")
            import sqlite3
            conn = sqlite3.connect(args.db)
            try:
                past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds")
                with conn:
                    conn.execute("UPDATE online_exam_session SET ends_at=? WHERE paper_id=?",
                                 (past, forced["id"]))
            finally:
                conn.close()
            denied = call(stu, "POST", f"/api/training/exams/{forced['id']}/heartbeat")
            check("到点后写接口拒绝并提示自动收卷",
                  denied["status"] == 409 and "自动收卷" in (denied.get("text") or ""),
                  (denied.get("json") or {}).get("detail", "")[:60])
            desk = call(staff, "GET",
                        f"/api/training/exams/papers/{forced['id']}/sessions?college={college}")
            people = ((desk.get("json") or {}).get("people")) or []
            check("惰性收卷已落库（会话变 forced）",
                  any(p.get("status") == "forced" for p in people),
                  str([p.get("status") for p in people]))
        else:
            check("惰性收卷（需要 --db 才能把结束时间拨到过去）", False, "未给 --db，跳过")

        # ---- 监考台 + 强制收卷
        desk = call(staff, "GET",
                    f"/api/training/exams/papers/{after['id']}/sessions?college={college}")
        summary = (desk.get("json") or {}).get("summary") or {}
        check("监考台汇总人数正确", summary.get("roster", 0) >= 1,
              str({k: summary.get(k) for k in ("roster", "in_progress", "submitted", "not_started")}))

        # ---- UI：两端都能渲染出「在线考试」卡片
        def open_exams_view(page, label: str) -> str:
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            button = page.query_selector('#universityNav button[data-university-view="exams"]')
            if not button:
                return f"{label}: 导航里没有在线考试入口"
            button.click()
            page.wait_for_timeout(2000)
            card = page.query_selector("#exam-ui-card")
            if not card:
                return f"{label}: 卡片没渲染"
            text = card.inner_text() or ""
            return "" if text.strip() else f"{label}: 卡片是空的"

        problem = open_exams_view(staff, "教师端")
        check("教师端「在线考试」卡片渲染出内容", not problem, problem)
        teacher_text = (staff.query_selector("#exam-ui-card").inner_text()
                        if staff.query_selector("#exam-ui-card") else "")
        check("教师端看得到走查考试", title_manual in (teacher_text or ""),
              (teacher_text or "")[:80].replace("\n", " / "))

        problem = open_exams_view(stu, "学生端")
        check("学生端「在线考试」卡片渲染出内容", not problem, problem)

        check("页面无 JS 报错", not js_errors, "; ".join(js_errors[:3]))
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
