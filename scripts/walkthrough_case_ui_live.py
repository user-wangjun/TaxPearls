"""实操案例任务前端 · 真库浏览器走查（验收清单第 34 项）。

与 scripts/smoke_teaching_case_ui.py 的区别：那边自建临时库跑全量口径；这里连真实
主库上已启动的服务，走一遍「教师发布 → 学生五步填写 → 教师量规评分」的真实点击路径，
用来坐实验收清单第 34 项（此前只做过接口层，前端标注为「未做浏览器走查」）。

用法（先起服务，默认连 8001 以免打扰常驻的 8000）：
    python -B -X utf8 scripts/walkthrough_case_ui_live.py [--base http://127.0.0.1:8001]

会往真库写入：发布 1 条案例任务 + 1 份学生五步交付物 + 1 份量规评分。重复运行是幂等的
（同一标题的作业会再发一条，走查后可用教师端「归档」收掉）。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" · " + detail) if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("WALK_BASE", "http://127.0.0.1:8001"))
    parser.add_argument("--teacher", default="staff01")
    parser.add_argument("--teacher-password", default=os.environ.get("WALK_TEACHER_PW", "Teacher#2026x"))
    parser.add_argument("--student", default="2026001")
    parser.add_argument("--student-password", default=os.environ.get("WALK_STUDENT_PW", "Tp#20260012026"))
    parser.add_argument("--shots", default=os.environ.get("WALK_SHOT_DIR", ""))
    args = parser.parse_args()
    base = args.base.rstrip("/")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path: str, payload=None, method: str | None = None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(base + path, data=data,
                                         method=method or ("POST" if data else "GET"),
                                         headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    # 教师走 staff 会话（真库 staff01 是 college_user，经 identity_links 拿平台会话）；
    # 学生走平台 /api/login。
    who = call("/api/training/staff/auth/login",
              {"username": args.teacher, "password": args.teacher_password})
    check("教师可登录", bool(who.get("staff", {}).get("id")),
          f"{args.teacher} / {who.get('staff', {}).get('role')}")

    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    http_errors: list[str] = []

    def watch(page):
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        # 401（登录前探测）与 404 /case、/rubric（非案例作业）是设计预期，不算缺陷
        page.on("response", lambda resp: http_errors.append(f"{resp.status} {resp.url}")
                if resp.status >= 400 and resp.status != 401
                and not (resp.status == 404
                         and resp.url.split("?")[0].endswith(("/case", "/rubric")))
                else None)
        page.on("dialog", lambda dialog: dialog.accept())

    def login(page, username, password, path="/api/login"):
        page.goto(base + "/", wait_until="domcontentloaded")
        page.evaluate("""async ({u, p, path}) => {
            const response = await fetch(path, {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!response.ok) throw new Error('login failed');
        }""", {"u": username, "p": password, "path": path})
        page.goto(base + "/", wait_until="networkidle")

    def open_training(page):
        """切到「课程与作业」视图。注意：案例任务卡自称 case-tasks 视图，
        而 staffViews 里根本没有它，所以切到别的视图它始终是 hidden。"""
        page.wait_for_timeout(800)
        nav = page.locator('#universityNav [data-university-view="tasks"]')
        if nav.count():
            nav.first.click()
            page.wait_for_timeout(600)
            return "tasks"
        panel = page.locator('#nav [data-panel="trainingPanel"]')
        if panel.count():
            panel.first.click()
            page.wait_for_timeout(500)
        return "workbench"

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        watch(page)
        login(page, args.teacher, args.teacher_password, "/api/training/staff/auth/login")
        mode = open_training(page)

        # ---------------------------------------------------------- 教师：发布
        card = page.locator("#case-task-card")
        card.wait_for(state="attached", timeout=20000)
        visible = card.is_visible()
        check("教师端「实操案例任务」卡可见", visible,
              f"视图={mode} · class={card.get_attribute('class') or ''}"
              f" · classroom-view={card.get_attribute('data-classroom-view')}")
        if not visible:
            if shots:
                page.screenshot(path=str(shots / "case-task-hidden.png"), full_page=True)
            print("\n卡在高校工作台里不可见：staffViews 没有 case-tasks 视图，"
                  "而卡片 data-classroom-view=\"case-tasks\" 永远匹配不上 selected。\n")
            browser.close()
            failed = [name for name, ok, _ in CHECKS if not ok]
            print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
            print("失败项：" + "；".join(failed))
            return 1

        selects = card.locator(".ct-form select")
        counts = [selects.nth(i).locator("option").count() for i in range(selects.count())]
        check("发布表单四个下拉都取到真库数据",
              selects.count() >= 4 and all(counts[:4]), f"下拉 {selects.count()} 个 / 选项 {counts}")

        title = "走查·案例任务前端"
        existing = [item for item in call("/api/training/teaching/case-tasks")["items"]
                    if (item.get("assignment_title") or "") == title and not item.get("archived")]
        if existing:
            check("复用已发布的走查任务（不重复造数据）", True, f"{len(existing)} 条")
        else:
            for box in card.locator(".ct-step input[type=checkbox]").all():
                if not box.is_checked():
                    box.check()
            for box in card.locator(".ct-dim input[type=checkbox]").all():
                if not box.is_checked():
                    box.check()
            card.locator(".ct-form input[type=text]").first.fill(title)
            card.get_by_role("button", name="发布成学生任务").first.click()
            page.wait_for_function("""name => {
                const text = document.querySelector('#case-task-card')?.textContent || '';
                return text.includes(name);
            }""", arg=title, timeout=30000)
            check("教师端发布成学生任务", title in card.inner_text(), "列表出现新任务")
        if shots:
            page.screenshot(path=str(shots / "case-task-teacher.png"), full_page=True)

        tasks = call("/api/training/teaching/case-tasks")["items"]
        task = next(item for item in tasks if (item.get("assignment_title") or "") == title)
        check("发布沿用主链任务（带班级）",
              bool(task["assignment_id"]) and bool(task.get("class_id")),
              f"assignment={task['assignment_id'][:8]} class={str(task.get('class_id'))[:8]}")

        # ---------------------------------------------------------- 学生：五步
        # 必须用独立 context：同一个 context 共享 cookie，学生一登录就把教师会话顶掉，
        # 教师页再去取案例任务列表就没有教师权限了。
        context2 = browser.new_context(viewport={"width": 1440, "height": 900})
        page2 = context2.new_page()
        watch(page2)
        login(page2, args.student, args.student_password)
        # 学生同样落在高校工作台：不切到「课程与作业」视图，#exerciseBox 是 hidden，
        # 五步面板插进去也就跟着不可见。
        open_training(page2)
        page2.evaluate("id => window.openExercise(id)", task["assignment_id"])
        panel = page2.locator("#tp-case-panel")
        panel.wait_for(timeout=20000)
        text = panel.first.inner_text()
        check("学生作业页内嵌五步表单", "实操案例" in text and "接收材料" in text,
              text.split("\n")[0][:28])

        panel.locator("textarea").first.fill("收到增值税专用发票 12 张，缺运输发票 1 张。")
        panel.get_by_role("button", name="保存这一步").first.click()
        page2.wait_for_function("""() => (document.querySelector('#tp-case-panel')?.textContent || '')
                                          .includes('已保存')""", timeout=20000)
        check("五步可在页面逐步保存", "已保存" in panel.inner_text(), "receive 步")
        if shots:
            page2.screenshot(path=str(shots / "case-task-student.png"), full_page=True)

        deliveries = call(f"/api/training/teaching/case-tasks/{task['id']}/deliveries")
        rows = deliveries.get("items") if isinstance(deliveries, dict) else deliveries
        check("教师端能读到学生交付物明文",
              any("运输发票" in json.dumps(row, ensure_ascii=False) for row in (rows or [])),
              f"{len(rows or [])} 步")

        # ---------------------------------------------------------- 教师：量规
        page.bring_to_front()
        # 学生那一轮操作后，教师卡的列表可能正在重建；直接点可能拿到还没带 class_id 的
        # task 对象（表现为「这个班级还没有学生」）。先等行可见再点，失败则刷新后重试一次。
        def open_scoring():
            card.locator(".ct-row").first.wait_for(state="visible", timeout=20000)
            page.wait_for_timeout(400)
            card.get_by_role("button", name="量规评分").first.click()
            page.wait_for_selector("#case-task-card .ct-table", timeout=20000)

        try:
            open_scoring()
        except Exception as exc:
            print(f"  [重试] 评分面板首次未渲染（{type(exc).__name__}），刷新后重试")
            card.get_by_role("button", name="刷新").first.click()
            page.wait_for_timeout(1200)
            open_scoring()
        names = card.locator(".ct-detail select").first.inner_text()
        check("评分面板按班级名册列出学生", "陈雨欣" in names or "李子航" in names,
              names.replace("\n", "/")[:28])

        selects2 = card.locator(".ct-table select")
        rows_count = card.locator(".ct-table tr").count()
        check("量规表按本次维度渲染", rows_count >= 1 and selects2.count() >= 1,
              f"{rows_count} 行")

        for index in range(selects2.count()):
            selects2.nth(index).select_option("2")
        card.get_by_role("button", name="保存量规评分").first.click()
        page.wait_for_function("""() => {
            const text = document.querySelector('#case-task-card')?.textContent || '';
            return text.includes('量规总分');
        }""", timeout=20000)
        detail = card.locator(".ct-detail").first.inner_text()
        check("保存后回显量规总分与分母", "量规总分" in detail, detail.replace("\n", " ")[:60])
        if shots:
            page.screenshot(path=str(shots / "case-task-rubric.png"), full_page=True)

        # 归档收尾，避免走查数据长期留在演示库的任务清单里
        card.get_by_role("button", name="归档").first.click()
        page.wait_for_function("""() => (document.querySelector('#case-task-card')?.textContent || '')
                                        .includes('已归档')""", timeout=20000)
        check("可归档（走查数据可收掉）", "已归档" in card.inner_text())
        browser.close()

    check("浏览器控制台无 JS 报错", not js_errors, "; ".join(js_errors[:3]))
    check("页面没有 4xx/5xx 资源请求（401 与预期 404 除外）", not http_errors,
          "; ".join(http_errors[:3]))

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    if failed:
        print("失败项：" + "；".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
