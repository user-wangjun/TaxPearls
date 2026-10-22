"""课程任务主链浏览器冒烟：教师清单→预览→设置，学生清单→任务说明。

用法（临时库，不碰真库）：
    python -B -X utf8 scripts/smoke_training_tasks.py

检查项：教师看到作业与试卷各一项、预览可用、学生只看到已发布任务、
学生端答案开放状态文案正确、控制台无 JS 错误。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" · " + detail) if detail else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    tmp = TemporaryDirectory()
    os.environ["TAXPEARLS_DB"] = str(Path(tmp.name) / "smoke.db")
    os.environ["TAXPEARLS_AI_ENABLED"] = "0"
    os.environ["TAXPEARLS_NOTIFICATION_EMAIL_ENABLED"] = "0"

    from webapp import app as module

    import uvicorn

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.15)
    # store 在应用 lifespan 里创建，服务起来之后才有实例
    for _ in range(80):
        if module.store is not None:
            break
        time.sleep(0.1)
    store = module.store
    password = "Smoke-tk84bdqe-2210!"
    teacher = store.create_user("smoke-task-teacher", password, "冒烟教师", "teacher", "smoke-college")
    student = store.create_user("smoke-task-student", password, "冒烟学生", "student", "smoke-college")

    import http.cookiejar
    import urllib.error
    import urllib.request

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path: str, payload=None, method: str | None = None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(base + path, data=data,
                                         method=method or ("POST" if data else "GET"),
                                         headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")

    def call_expect(path: str, payload=None) -> int:
        """只取状态码，用于预期失败的调用（被拒也是结果，不该让脚本崩）。"""
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(base + path, data=data,
                                         method="POST" if data else "GET",
                                         headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=20) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    errors: list[str] = []
    call("/api/login", {"username": teacher["username"], "password": password})
    case = call("/api/exercises", {"rule_id": "R-001", "seed": 23, "expected_version": "2.0"})
    cls = call("/api/classes", {"name": "冒烟会计一班", "student_ids": [student["id"]]})
    due = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    single = call("/api/assignments", {"title": "冒烟单案例作业", "audit_id": case["audit_id"],
                                       "published": True, "class_id": cls["id"], "deadline_at": due})
    paper = call("/api/papers", {"title": "冒烟综合卷", "class_id": cls["id"], "deadline_at": due,
                                 "published": True,
                                 "items": [{"audit_id": case["audit_id"], "points": 100}]})
    draft_task = call("/api/assignments", {"title": "冒烟草稿作业", "audit_id": case["audit_id"],
                                           "published": True, "class_id": cls["id"], "deadline_at": due})
    check("教师创建单案例作业与试卷", bool(single.get("id") and paper.get("id")),
          json.dumps({"assignment": single.get("id"), "paper": paper.get("id")}, ensure_ascii=False))
    for kind, subject in (("assignment", single["id"]), ("paper", paper["id"])):
        task = call(f"/api/training/tasks/{kind}/{subject}")
        check(f"任务详情 {kind}", task.get("state") == "published" and task.get("expected") == 1,
              f"state={task.get('state')} expected={task.get('expected')}")
    listed = call("/api/training/tasks")["items"]
    check("教师清单含两类任务且不重复列题", {(i["kind"], i["id"]) for i in listed} ==
          {("assignment", single["id"]), ("paper", paper["id"]), ("assignment", draft_task["id"])},
          f"{len(listed)} 项")
    preview = call(f"/api/training/tasks/assignment/{single['id']}/preview")
    check("预览返回学生视角与题目顺序", preview["task"]["preview"] and len(preview["task"]["questions"]) == 1,
          preview["note"][:24])

    call("/api/logout", {})
    call("/api/login", {"username": student["username"], "password": password})
    mine = call("/api/training/tasks")["items"]
    check("学生看到在班任务", {i["id"] for i in mine} == {single["id"], paper["id"], draft_task["id"]},
          f"{len(mine)} 项")
    detail = call(f"/api/training/tasks/assignment/{single['id']}")
    check("学生详情默认答案不开放", detail["answers_visible"] is False and detail["can_submit"] is True,
          f"release={detail['answer_release']}")
    check("学生任务卡带本人进度", all("my_status" in item for item in mine),
          ",".join(sorted({item["my_status"] for item in mine})))

    # 订正闭环：重复提交拦截 → 待复核 → 复核 → 退回 → 再交 → 旧分留在版本里
    first = call(f"/api/assignments/{single['id']}/submit", {"selected_rule_ids": []})
    # 默认 after_due 且截止在 7 天后：回执只确认已提交，不带分数与解析
    check("学生首次提交成功且回执按策略不给分", first.get("result_released") is False
          and first.get("score") is None and not first.get("missed"),
          f"released={first.get('result_released')} score={first.get('score')}")
    again = call_expect(f"/api/assignments/{single['id']}/submit", {"selected_rule_ids": []})
    check("重复提交被拦截", again == 409, f"status={again}")
    pending = call(f"/api/assignments/{single['id']}/progress")
    check("提交后为待复核", pending["status"] == "pending_review" and pending["attempts"] == 1,
          f"status={pending['status']} attempts={pending['attempts']}")

    call("/api/logout", {})
    call("/api/login", {"username": teacher["username"], "password": password})
    subs = call(f"/api/submissions?assignment_id={single['id']}")
    sub_id = next((row["id"] for row in subs), None)
    check("教师能看到学生提交", bool(sub_id), f"submission={sub_id}")
    call(f"/api/submissions/{sub_id}/review", {"adjusted_score": 88, "feedback": "第一次口径写错"}, method="PUT")
    returned = call(f"/api/assignments/{single['id']}/return",
                    {"student_id": student["id"], "reason": "口径写错了，按三步重新算"})
    check("教师退回成功", returned.get("status") == "returned", f"status={returned.get('status')}")

    call("/api/logout", {})
    call("/api/login", {"username": student["username"], "password": password})
    mine_progress = call(f"/api/assignments/{single['id']}/progress")
    check("学生看到退回原因", mine_progress["status"] == "returned"
          and "口径写错" in (mine_progress.get("return_reason") or ""),
          f"status={mine_progress['status']}")
    again_ok = call_expect(f"/api/assignments/{single['id']}/submit", {"selected_rule_ids": []})
    check("退回后可再次提交", again_ok == 200, f"status={again_ok}")
    history = call(f"/api/assignments/{single['id']}/versions")["items"]
    check("旧分数留在版本里", len(history) == 1 and history[0]["adjusted_score"] == 88.0
          and history[0]["feedback"] == "第一次口径写错",
          f"versions={len(history)} adjusted={history[0]['adjusted_score'] if history else None}")

    # 结果页按策略下发：after_due 未到截止，作业详情只回执「已提交」
    gated = call(f"/api/assignments/{single['id']}").get("submission") or {}
    check("未开放时结果页只回执已提交", gated.get("result_released") is False
          and gated.get("score") is None and gated.get("details") == {},
          f"released={gated.get('result_released')} score={gated.get('score')}")
    check("未开放时给出原因文案", "截止后开放" in (gated.get("result_note") or ""),
          (gated.get("result_note") or "")[:24])
    # 学习档案按版本展开：旧版本的复核分与评语跟着记录走
    profile = call("/api/training/profile")
    mine_record = next((r for r in profile["records"] if r["id"] == single["id"]), None)
    versions_in_profile = (mine_record or {}).get("versions") or []
    check("学习档案按版本展开", len(versions_in_profile) == 1
          and versions_in_profile[0]["adjusted_score"] == 88.0
          and versions_in_profile[0]["feedback"] == "第一次口径写错",
          f"versions={len(versions_in_profile)}")

    # 教师改成「交后即可看」，学生再打开作业页应能看到分数与解析
    call("/api/logout", {})
    call("/api/login", {"username": teacher["username"], "password": password})
    call(f"/api/training/tasks/assignment/{single['id']}/settings",
         {"answer_release": "after_submit"}, method="PUT")
    call("/api/logout", {})
    call("/api/login", {"username": student["username"], "password": password})
    released = call(f"/api/assignments/{single['id']}").get("submission") or {}
    check("改成交后即可看后结果页给分", released.get("result_released") is True
          and released.get("score") is not None and bool(released.get("details")),
          f"score={released.get('score')}")

    # 再退一次，留给浏览器验证「已退回 + 订正次数用尽」的页面态
    call("/api/logout", {})
    call("/api/login", {"username": teacher["username"], "password": password})
    call(f"/api/assignments/{single['id']}/return",
         {"student_id": student["id"], "reason": "第二步口径仍不对，先看复核意见"})
    call("/api/logout", {})
    call("/api/login", {"username": student["username"], "password": password})
    check("二次退回可记录", call(f"/api/assignments/{single['id']}/progress")["status"] == "returned",
          "status=returned")

    from playwright.sync_api import sync_playwright

    def open_tasks(page):
        """高校工作台点「课程与作业」；通用工作台点「实训中心」。"""
        page.wait_for_timeout(600)
        tasks_nav = page.locator('#universityNav [data-university-view="tasks"]')
        if tasks_nav.count():
            tasks_nav.first.click()
            page.wait_for_timeout(400)
            return "university"
        panel = page.locator('#nav [data-panel="trainingPanel"]')
        if panel.count():
            panel.first.click()
            page.wait_for_timeout(400)
        return "workbench"

    def login(page, who):
        page.goto(base + "/", wait_until="domcontentloaded")
        page.evaluate("""async ({u, p}) => {
            const response = await fetch('/api/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!response.ok) throw new Error('login failed');
        }""", {"u": who["username"], "p": password})
        page.goto(base + "/", wait_until="networkidle")

    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("console", lambda msg: errors.append(msg.text)
                if msg.type == "error" and "401" not in msg.text else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        login(page, teacher)
        mode = open_tasks(page)
        card = page.locator("#task-board-card")
        card.wait_for(timeout=15000)
        text = card.inner_text()
        check("教师端任务卡渲染两类任务", "冒烟单案例作业" in text and "冒烟综合卷" in text,
              f"{mode} · {text.split(chr(10))[0]}")
        page.get_by_role("button", name="预览学生视角").first.click()
        page.wait_for_function("""() => {
            const box = document.querySelector('#task-board-card .tb-detail');
            return box && box.textContent.includes('学生看到的样子');
        }""", timeout=10000)
        shots = os.environ.get("SMOKE_SHOT_DIR")
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / "task-board-teacher.png"), full_page=True)
        detail_text = page.locator("#task-board-card .tb-detail").first.inner_text()
        check("预览面板可展开", "学生看到的样子" in detail_text, detail_text.replace(chr(10), " / ")[:40])
        page.get_by_role("button", name="任务设置").first.click()
        page.wait_for_selector("#task-board-card .tb-form", timeout=10000)
        check("任务设置表单可展开", page.locator("#task-board-card .tb-form select").first.input_value() in
              {"after_due", "after_submit", "manual"})

        context2 = browser.new_context(viewport={"width": 1440, "height": 900})
        page2 = context2.new_page()
        page2.on("console", lambda msg: errors.append(msg.text)
                 if msg.type == "error" and "401" not in msg.text else None)
        page2.on("pageerror", lambda exc: errors.append(str(exc)))
        login(page2, student)
        mode2 = open_tasks(page2)
        card2 = page2.locator("#task-board-card")
        card2.wait_for(timeout=15000)
        text2 = card2.inner_text()
        check("学生端任务卡渲染任务与答案口径", "冒烟单案例作业" in text2 and "截止后开放" in text2,
              f"{mode2} · {text2.split(chr(10))[0]}")
        page2.get_by_role("button", name="任务说明").first.click()
        page2.wait_for_selector("#task-board-card .tb-detail", timeout=10000)
        detail_text = page2.locator("#task-board-card .tb-detail").first.inner_text()
        check("学生任务说明可展开", "提交情况" in detail_text and "订正" in detail_text,
              detail_text.split(chr(10))[4][:36] if len(detail_text.split(chr(10))) > 4 else "")
        if shots:
            page2.screenshot(path=str(Path(shots) / "task-board-student.png"), full_page=True)

        # 草稿面板：直接打开作业页（不依赖列表顺序）
        page2.evaluate("id => window.openExercise(id)", draft_task["id"])
        page2.wait_for_selector("#tp-submission-panel", timeout=15000)
        panel_text = page2.locator("#tp-submission-panel").first.inner_text()
        check("作业页显示提交状态面板", "未开始" in panel_text and "限交" not in panel_text,
              panel_text.split(chr(10))[0][:40])
        page2.get_by_role("button", name="保存草稿").first.click()
        page2.wait_for_function("""() => (document.querySelector('#tp-submission-panel')?.textContent || '')
                                          .includes('草稿已保存')""", timeout=10000)
        check("草稿可在页面保存", True, "面板出现「草稿已保存」")
        check("提交按钮仍可用", page2.locator("#btnSubmitExercise").is_enabled(), "未提交时不禁用")

        # 已提交并被退回的作业：面板给出退回原因，按钮按订正次数封住
        page2.evaluate("id => window.openExercise(id)", single["id"])
        page2.wait_for_function("""() => (document.querySelector('#tp-submission-panel')?.textContent || '')
                                          .includes('退回')""", timeout=15000)
        returned_text = page2.locator("#tp-submission-panel").first.inner_text()
        check("退回原因在作业页可见", "第二步" in returned_text, returned_text.split(chr(10))[0][:40])
        check("订正次数用尽后按钮禁用", page2.locator("#btnSubmitExercise").is_disabled(),
              page2.locator("#btnSubmitExercise").inner_text())
        if shots:
            page2.screenshot(path=str(Path(shots) / "submission-panel-student.png"), full_page=True)
            page2.locator("#tp-submission-panel").first.screenshot(
                path=str(Path(shots) / "submission-panel-only.png"))

        # 教师端复核列表里的退回入口
        page.evaluate("() => window.loadAssignments()")
        page.wait_for_timeout(2000)
        check("复核列表有退回入口",
              page.locator("#submissionList").get_by_role("button", name="退回要求订正").count() > 0,
              f"rows={page.locator('#submissionList .data-item').count()}")
        browser.close()

    check("浏览器控制台无 JS 错误", not errors, "; ".join(errors[:3]))
    server.should_exit = True
    time.sleep(0.5)
    try:
        tmp.cleanup()
    except OSError:
        pass  # Windows 上服务线程可能还占着 db 文件，临时目录交给系统清理

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    if failed:
        print("未通过：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
