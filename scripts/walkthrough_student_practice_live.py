"""学生端刷题 · 真库浏览器走查。

回答的问题是「学生到底能不能刷题」——不只看接口返回 200，而是在真实页面上点一遍：
登录 → 打开考证备考页 → 选证书 → 开始训练 → 勾选风险项 → 提交判分 → 看判分与解析。

用法（先起服务，默认连 8001 以免打扰常驻的 8000）：
    python -B -X utf8 scripts/walkthrough_student_practice_live.py [--base http://127.0.0.1:8001]

只读为主：仿真题作答会真切写一条 attempt（这是产品行为，不是造数），
不新建证书/题目，也不改教师侧任何配置。
"""
from __future__ import annotations

import argparse
import os
import sys
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
    parser.add_argument("--student", default="2026001")
    parser.add_argument("--student-password", default=os.environ.get("WALK_STUDENT_PW", "Tp#20260012026"))
    parser.add_argument("--shots", default=os.environ.get("WALK_SHOT_DIR", ""))
    args = parser.parse_args()
    base = args.base.rstrip("/")

    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    http_errors: list[tuple[str, int, str]] = []
    stage = {"name": "boot"}

    def watch(page):
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        # 401 是登录前的会话探测；真题通道无题时后端按设计返 404（文案给到学生），
        # 两者都不算缺陷，其余 4xx/5xx 一律记录下来。
        def record(resp):
            if resp.status < 400 or resp.status == 401:
                return
            if stage["name"] == "real" and resp.status == 404 and resp.url.endswith("/practice/start"):
                return
            http_errors.append((stage["name"], resp.status, resp.url))
        page.on("response", record)
        page.on("dialog", lambda dialog: dialog.accept())

    def notice(page) -> str:
        """提示可能落在卡片内 #pr-error，也可能被工作台外壳接管到 #universityStatus。"""
        for selector in ("#pr-error", "#universityStatus", "#app-error", "#goal-msg"):
            node = page.locator(selector)
            if node.count():
                text = node.first.inner_text().strip()
                if text:
                    return text
        return ""

    def login(page, username, password):
        page.goto(base + "/", wait_until="domcontentloaded")
        page.evaluate("""async ({u, p}) => {
            const response = await fetch('/api/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!response.ok) throw new Error('login failed: ' + response.status);
        }""", {"u": username, "p": password})

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        watch(page)
        login(page, args.student, args.student_password)

        page.goto(base + "/training-certificates", wait_until="networkidle")
        page.wait_for_timeout(1200)

        # 页面 boot() 先探 /api/training/auth/me，401 时用平台会话换训练会话
        app_view = page.locator("#app-view")
        check("学生进入考证备考页", app_view.count() == 1 and app_view.first.is_visible(),
              page.locator("#who").inner_text().strip() if page.locator("#who").count() else "")

        # 刷题卡在 practice 视图，默认在 home；先切视图再看可见性
        nav = page.locator('#universityNav [data-university-view="practice"]')
        if nav.count():
            nav.first.click()
            page.wait_for_timeout(700)
        card = page.locator("#pr-card")
        visible = card.count() == 1 and card.first.is_visible()
        check("「税务案例训练与刷题」卡可见", visible,
              "practice 视图" if nav.count() else "无高校导航（未走 view 显隐）")

        # 证书下拉来自真库
        page.wait_for_timeout(600)
        certs = page.eval_on_selector_all("#pr-cert option",
                                          "os => os.map(o => ({v: o.value, t: o.textContent.trim()}))")
        certs = [c for c in certs if c["v"]]
        check("证书下拉有真库数据", bool(certs), f"{len(certs)} 个 · " + (certs[0]["t"] if certs else "空"))
        if not certs:
            print("没有证书就无法开刷，后续步骤跳过。")
            return 1

        page.select_option("#pr-cert", certs[0]["v"])
        page.wait_for_timeout(800)
        hint = page.locator("#pr-hint").inner_text().strip()
        check("题源说明渲染", bool(hint), hint[:60])

        # ------------------------------------------------ 仿真题：开刷 → 作答 → 判分
        page.select_option("#pr-source", "simulated")
        page.wait_for_timeout(300)
        page.click("#start-practice")
        # 开刷要现算仿真材料，接口实测约 2s 才回，等选择器而不是死等固定毫秒
        try:
            page.wait_for_selector("#pr-attempt .risk-pick", timeout=20000)
        except Exception:  # noqa: BLE001 - 没等到就走下面的诊断分支
            pass

        picks = page.locator("#pr-attempt .risk-pick")
        count = picks.count()
        check("开始训练后出现可勾选的风险项", count > 0, f"{count} 条风险项")
        mats = page.locator("#pr-attempt table tr").count()
        check("仿真材料表（科目余额/申报/指标）渲染", mats > 0, f"{mats} 行")
        if count == 0:
            print("没有可作答的题，后续步骤跳过。")
            print("--- #pr-attempt ---", page.locator("#pr-attempt").inner_text()[:400])
            print("--- #pr-error ---", page.locator("#pr-error").inner_text()[:300]
                  if page.locator("#pr-error").count() else "(无)")
            print("--- #pr-kps ---", page.locator("#pr-kps").inner_text()[:300])
            print("--- #app-error ---", page.locator("#app-error").inner_text()[:300]
                  if page.locator("#app-error").count() else "(无)")
            print("--- #pr-open ---", page.locator("#pr-open").inner_text()[:300]
                  if page.locator("#pr-open").count() else "(无)")
            print("--- js ---", js_errors[:3], "--- http ---", http_errors[:5])
            return 1

        picks.first.check()
        page.click('#pr-attempt button[data-act="submit"]')
        try:
            page.wait_for_selector("#pr-attempt .badge", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(400)

        result = page.locator("#pr-attempt .badge")
        score_text = result.first.inner_text().strip() if result.count() else ""
        check("提交后出现判分结果", bool(score_text), score_text)
        detail = page.locator("#pr-attempt .detail.ok, #pr-attempt .detail.miss, #pr-attempt .detail.false")
        check("判分明细逐项展开", detail.count() > 0, f"{detail.count()} 条明细")
        answer = page.locator("#pr-attempt").inner_text()
        check("标准答案判分后开放", "标准答案" in answer, "标准答案" if "标准答案" in answer else "未出现")
        check("AI 答疑入口在位", page.locator("#pr-attempt #tutor-q").count() == 1)
        stats = page.locator("#pr-stats").inner_text().strip()
        check("刷题统计回显", bool(stats), stats[:60])

        if shots:
            page.screenshot(path=str(shots / "student-practice-graded.png"), full_page=True)

        # ------------------------------------------------ 真题通道：无授权题目时的兜底
        stage["name"] = "real"
        page.select_option("#pr-source", "real")
        page.wait_for_timeout(300)
        page.click("#start-practice")
        page.wait_for_timeout(2000)
        real_text = notice(page)
        check("无真题时给出明确提示而非静默失败", bool(real_text), real_text[:90])

        # 错题重练区块
        page.wait_for_timeout(500)
        wrong = page.locator("#pr-wrong-section")
        check("错题重练区块渲染", wrong.count() == 1, wrong.inner_text().strip()[:40].replace("\n", " / ") if wrong.count() else "")

        check("全程零 JS 报错", not js_errors, "；".join(js_errors[:3]))
        check("无 4xx/5xx（401 探测与真题空题 404 除外）", not http_errors,
              "；".join(f"[{s}] {n} {u.split('8001')[-1]}" for s, n, u in http_errors[:3]))

        context.close()
        browser.close()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n==== {passed}/{len(CHECKS)} 通过 ====")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
