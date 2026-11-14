"""实操案例任务浏览器走查：教师发布 → 学生五步填写 → 教师按量规评分。

用法（临时库，不碰真库）：
    python -B -X utf8 scripts/smoke_teaching_case_ui.py

与 smoke_training_tasks.py 的区别：那边走客观题作业主链，这里走实操案例链路
（案例 → 作业 → 五步交付物 → 四维量规）。HTTP 层先测接口口径，浏览器层再验证
真实点击路径与控制台报错。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import uuid
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
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.15)
    for _ in range(80):
        if module.store is not None:
            break
        time.sleep(0.1)
    store = module.store
    password = "Smoke-case-ui-2026!"
    teacher = store.create_user("smoke-case-teacher", password, "冒烟案例教师", "teacher",
                                "smoke-college")
    student = store.create_user("smoke-case-student", password, "冒烟案例学生", "student",
                                "smoke-college")

    # 证书与案例走 SQL：现有写接口挂在 staff 会话下，教学侧没有对应入口
    stamp = datetime.now(UTC).isoformat()
    cert_id, case_id = uuid.uuid4().hex, uuid.uuid4().hex
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO certificate (id,code,name,description,subjects_json,source_ref,active,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,1,?,?)",
            (cert_id, "smoke-case-cert", "冒烟税务实操证书", "走查用", "[]", "走查造数", stamp, stamp))
        db.execute(
            """INSERT INTO teaching_case
                 (id,certificate_id,unit_id,name,scenario,rule_codes_json,materials_json,
                  standard_conclusion_json,desensitization_note,status,created_by,created_at,updated_at)
               VALUES (?,?,NULL,?,?,?,?,?,?,'published',NULL,?,?)""",
            (case_id, cert_id, "冒烟进项税额勾稽案例", "某企业一个月进项发票与账面不符，需要核。",
             '["R-001"]',
             json.dumps([{"name": "进项发票汇总表", "note": "含 12 张专票"},
                         {"name": "账面应交税费明细", "note": ""}], ensure_ascii=False),
             json.dumps([{"rule_code": "R-001", "expected": "应转出进项税额 1.2 万元",
                          "note": "按不得抵扣情形"}], ensure_ascii=False),
             "企业名称与税号已脱敏。", stamp, stamp))

    import http.cookiejar
    import urllib.error
    import urllib.request

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    retries = 0

    def call(path: str, payload=None, method: str | None = None):
        """串行请求；偶发整服 20s 级卡顿时重试一次（Windows + SQLite WAL 抖动，非功能问题）。"""
        nonlocal retries
        data = None if payload is None else json.dumps(payload).encode()
        for attempt in range(3):
            request = urllib.request.Request(base + path, data=data,
                                             method=method or ("POST" if data else "GET"),
                                             headers={"Content-Type": "application/json"})
            try:
                with opener.open(request, timeout=15) as response:
                    return json.loads(response.read() or b"{}")
            except (TimeoutError, socket.timeout):
                if attempt == 2:
                    raise
                retries += 1
                time.sleep(1.0)

    def call_status(path: str, payload=None, method: str | None = None) -> int:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(base + path, data=data,
                                         method=method or ("POST" if data else "GET"),
                                         headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=20) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    js_errors: list[str] = []
    http_errors: list[str] = []

    js_errors: list[str] = []
    http_errors: list[str] = []

    def watch(page):
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        # 两类回包是设计预期，不算缺陷：
        # 401 = 登录前/跨身份探测；404 /case、/rubric = 「这道作业不是实操案例任务」
        page.on("response", lambda resp: http_errors.append(f"{resp.status} {resp.url}")
                if resp.status >= 400 and resp.status != 401
                and not (resp.status == 404
                         and resp.url.split("?")[0].endswith(("/case", "/rubric")))
                else None)

    call("/api/login", {"username": teacher["username"], "password": password})
    exercise = call("/api/exercises", {"rule_id": "R-001", "seed": 77, "expected_version": "2.0"})
    cls = call("/api/classes", {"name": "冒烟税务一班", "student_ids": [student["id"]]})
    due = (datetime.now(UTC) + timedelta(days=7)).isoformat()

    # ------------------------------------------------------------ 发布（HTTP 层）
    check("证书列表可取", any(item["id"] == cert_id
                          for item in call("/api/training/teaching/certificates")["items"]),
          f"cert={cert_id[:8]}")
    check("已发布案例可选", any(item["id"] == case_id for item in
                            call(f"/api/training/teaching/publish-cases?certificate_id={cert_id}")
                            ["items"]), f"case={case_id[:8]}")
    published = call(f"/api/training/teaching/cases/{case_id}/publish", {
        "class_id": cls["id"], "audit_id": exercise["audit_id"], "title": "冒烟实操案例作业",
        "deadline_at": due, "published": True,
        "steps": ["receive", "identify", "reconcile"], "dimensions": ["material", "reconcile", "basis"],
    })
    task_id = published["id"]
    check("案例发布成作业", bool(published.get("assignment_id")), f"task={task_id[:8]}")
    check("发布沿用主链任务", any(item["id"] == published["assignment_id"]
                             for item in call("/api/training/tasks")["items"]),
          f"assignment={published['assignment_id'][:8]}")
    listed = call("/api/training/teaching/case-tasks")["items"]
    check("案例任务列表带班级（评分按名册取人）",
          listed and listed[0]["class_id"] == cls["id"], f"class_id={listed[0].get('class_id')}")
    check("只列本次勾选的步骤与维度",
          listed[0]["steps"] == ["receive", "identify", "reconcile"]
          and listed[0]["dimensions"] == ["material", "reconcile", "basis"],
          f"{len(listed[0]['steps'])} 步 / {len(listed[0]['dimensions'])} 维")

    # ------------------------------------------------------------ 学生五步（HTTP 层）
    call("/api/logout", {})
    call("/api/login", {"username": student["username"], "password": password})
    view = call(f"/api/assignments/{published['assignment_id']}/case")
    check("学生看到案例与本次步骤", view["case"]["name"] == "冒烟进项税额勾稽案例"
          and len(view["steps_detail"]) == 3, f"{len(view['steps_detail'])} 步")
    check("标准结论未开放时不下发", view["standard_conclusion"] == []
          and bool(view["conclusion_note"]), view["conclusion_note"][:24])
    saved = call(f"/api/assignments/{published['assignment_id']}/case-delivery",
                 {"step": "receive", "content": "收到发票 12 张，缺运输发票 1 张。"}, method="PUT")
    check("五步交付物可保存并回读明文", "运输发票" in (saved.get("content") or ""),
          (saved.get("content") or "")[:24])
    # 落库是密文由单测覆盖（tests/test_teaching_case_tasks.py）：这里不再开裸 sqlite
    # 连接去读原始值——在 Windows 上它会扰动 WAL，让后续请求莫名卡住。
    check("未训练的步骤拒收",
          call_status(f"/api/assignments/{published['assignment_id']}/case-delivery",
                      {"step": "evidence", "content": "本次不训练这一步"}, method="PUT") == 409,
          "step=evidence")
    check("未开放时量规结果不下发",
          call(f"/api/assignments/{published['assignment_id']}/rubric")["released"] is False,
          "released=False")

    # ------------------------------------------------------------ 量规评分（HTTP 层）
    call("/api/logout", {})
    call("/api/login", {"username": teacher["username"], "password": password})
    rubric = call(f"/api/training/teaching/case-tasks/{task_id}/rubric")
    check("量规按发布时的维度建 v1", rubric["version"] == 1 and rubric["max_score"] == 6.0,
          f"v{rubric['version']} 满分 {rubric['max_score']}")
    scored = call(f"/api/training/teaching/case-tasks/{task_id}/rubric-scores", {
        "student_id": student["id"],
        "scores": [{"dimension": "material", "score": 2, "comment": "要素齐全"},
                   {"dimension": "reconcile", "score": 1, "comment": "口径写清了"},
                   {"dimension": "basis", "score": None, "comment": ""}],
    }, method="PUT")
    total = scored["total"]
    check("不适用维度不计入分母", total["raw"] == 3.0 and total["max"] == 4.0
          and total["percent"] == 75.0 and total["not_applicable"] == ["basis"],
          f"{total['raw']}/{total['max']} = {total['percent']}%")
    deliveries = call(f"/api/training/teaching/case-tasks/{task_id}/deliveries"
                      f"?student_id={student['id']}")
    check("教师能看到该学生的五步交付", "运输发票" in
          (deliveries["items"][0]["steps"][0]["content"] or ""),
          f"{len(deliveries['items'][0]['steps'])} 步")
    check("非本班学生评分被拒",
          call_status(f"/api/training/teaching/case-tasks/{task_id}/rubric-scores",
                      {"student_id": teacher["id"],
                       "scores": [{"dimension": "material", "score": 2}]}, method="PUT") == 404,
          "student=teacher")

    # ------------------------------------------------------------ 浏览器层
    from playwright.sync_api import sync_playwright

    def login(page, who):
        page.goto(base + "/", wait_until="domcontentloaded")
        page.evaluate("""async ({u, p}) => {
            const response = await fetch('/api/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!response.ok) throw new Error('login failed');
        }""", {"u": who["username"], "p": password})
        page.goto(base + "/", wait_until="networkidle")

    def open_training(page):
        page.wait_for_timeout(600)
        nav = page.locator('#universityNav [data-university-view="tasks"]')
        if nav.count():
            nav.first.click()
            page.wait_for_timeout(400)
            return "university"
        panel = page.locator('#nav [data-panel="trainingPanel"]')
        if panel.count():
            panel.first.click()
            page.wait_for_timeout(400)
        return "workbench"

    shots = os.environ.get("SMOKE_SHOT_DIR")
    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        watch(page)
        login(page, teacher)
        mode = open_training(page)
        card = page.locator("#case-task-card")
        card.wait_for(timeout=15000)
        text = card.inner_text()
        check("教师端案例任务卡渲染已发布任务", "冒烟实操案例作业" in text and "勾稽分析" in text,
              f"{mode} · {text.split(chr(10))[0]}")

        # 发布表单：证书/案例/班级/数据底座四个下拉都要有选项
        selects = card.locator(".ct-form select")
        check("发布表单四个下拉都取到数据",
              selects.count() >= 4 and all(selects.nth(i).locator("option").count() > 0
                                           for i in range(4)),
              f"下拉 {selects.count()} 个")

        # 量规评分：选学生 → 打分 → 保存
        card.get_by_role("button", name="量规评分").first.click()
        page.wait_for_selector("#case-task-card .ct-table", timeout=15000)
        check("评分面板按班级名册列出学生",
              "冒烟案例学生" in page.locator("#case-task-card .ct-form select").first.inner_text(),
              page.locator("#case-task-card .ct-form select").first.inner_text()[:24])
        rows = page.locator("#case-task-card .ct-table tr")
        check("量规表按维度渲染（含已存分数）", rows.count() == 4
              and page.locator("#case-task-card .ct-table select").first.input_value() == "2",
              f"{rows.count()} 行")
        # 把「勾稽与口径」从 1 改成 0：总分应由 3.0/4.0=75% 变成 2.0/4.0=50%
        page.locator("#case-task-card .ct-table select").nth(1).select_option("0")
        card.get_by_role("button", name="保存量规评分").first.click()
        page.wait_for_function("""() => {
            const text = document.querySelector('#case-task-card')?.textContent || '';
            return text.includes('量规总分') && text.includes('50');
        }""", timeout=10000)
        total_line = [line for line in card.inner_text().split(chr(10)) if "量规总分" in line][0]
        check("页面显示真实分母与不适用维度",
              "量规总分" in total_line and "不适用" in total_line, total_line[:60])
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / "case-task-teacher.png"), full_page=True)

        # 学生端：作业页内嵌五步表单
        context2 = browser.new_context(viewport={"width": 1440, "height": 900})
        page2 = context2.new_page()
        watch(page2)
        login(page2, student)
        page2.evaluate("id => window.openExercise(id)", published["assignment_id"])
        page2.wait_for_selector("#tp-case-panel", timeout=15000)
        panel_text = page2.locator("#tp-case-panel").first.inner_text()
        check("学生作业页内嵌五步表单", "实操案例" in panel_text and "接收材料" in panel_text,
              panel_text.split(chr(10))[0][:30])
        check("已保存的内容回填到表单",
              "运输发票" in page2.locator("#tp-case-panel textarea").first.input_value(),
              "receive 步")
        page2.locator("#tp-case-panel textarea").nth(1).fill("金额 1.2 万，税额 1560 元。")
        page2.locator("#tp-case-panel").get_by_role("button", name="保存这一步").nth(1).click()
        page2.wait_for_function("""() => (document.querySelector('#tp-case-panel')?.textContent || '')
                                          .includes('已保存')""", timeout=10000)
        check("五步可在页面逐步保存", "已保存" in page2.locator("#tp-case-panel").inner_text(),
              "identify 步")
        if shots:
            page2.screenshot(path=str(Path(shots) / "case-task-student.png"), full_page=True)

        # 普通（非案例）作业不该被这套面板打扰
        plain = call("/api/assignments", {"title": "冒烟普通作业", "audit_id": exercise["audit_id"],
                                          "published": True, "class_id": cls["id"],
                                          "deadline_at": due})
        page2.evaluate("id => window.openExercise(id)", plain["id"])
        page2.wait_for_timeout(1500)
        check("非案例作业不显示五步面板",
              page2.locator("#tp-case-panel").count() == 0, "assignment=普通作业")
        browser.close()

    check("浏览器控制台无 JS 报错", not js_errors, "; ".join(js_errors[:3]))
    check("页面没有 4xx/5xx 资源请求（401 除外）", not http_errors, "; ".join(http_errors[:3]))
    check("HTTP 层未出现整服卡顿重试", retries == 0, f"重试 {retries} 次")
    server.should_exit = True
    time.sleep(0.5)
    try:
        tmp.cleanup()
    except OSError:
        pass

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    if failed:
        print("失败项：" + "；".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
