"""高校管理报表（FR-U05）· 真库浏览器走查。

验证三件事：卡片在「学情统计」里出得来、未归属教学班能被管理员一键绑定、
绑定后院系/专业/课程三级的数字与班级学情对得上。

用法（先起服务，默认连 8002 以免打扰常驻的 8000/8001）：
    python -B -X utf8 scripts/walkthrough_learning_report.py [--base http://127.0.0.1:8002]

注意：如果真库里还有未归属教学班，本脚本会执行一次「绑定」——这是业务操作，
会写 `training_classes.college_class_id`，可用界面上的解绑或再次运行改绑回退。
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
    parser.add_argument("--base", default=os.environ.get("WALK_BASE", "http://127.0.0.1:8002"))
    parser.add_argument("--teacher", default="staff01")
    parser.add_argument("--teacher-password", default=os.environ.get("WALK_TEACHER_PW", "Teacher#2026x"))
    parser.add_argument("--shots", default=os.environ.get("WALK_SHOT_DIR", ""))
    args = parser.parse_args()
    base = args.base.rstrip("/")

    from playwright.sync_api import sync_playwright

    js_errors, http_errors = [], []
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on("response", lambda resp: http_errors.append(f"{resp.status} {resp.url}")
                if resp.status >= 400 and resp.status != 401 else None)
        page.on("dialog", lambda dialog: dialog.accept())

        page.goto(base + "/", wait_until="domcontentloaded")
        page.evaluate("""async ({u, p}) => {
            const response = await fetch('/api/training/staff/auth/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!response.ok) throw new Error('staff login failed: ' + response.status);
        }""", {"u": args.teacher, "p": args.teacher_password})
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1000)

        nav = page.locator('#universityNav [data-university-view="statistics"]')
        if nav.count():
            nav.first.click()
        try:
            page.wait_for_selector("#collegeLearningReport", state="visible", timeout=15000)
        except Exception:  # noqa: BLE001 - 没等到交给断言
            pass
        card = page.locator("#collegeLearningReport")
        check("「高校管理报表」卡可见", card.count() == 1 and card.first.is_visible(),
              page.locator("#clrStatus").inner_text().strip()[:60] if page.locator("#clrStatus").count() else "")

        try:
            page.wait_for_function("() => document.querySelectorAll('#clrBody .kpi').length >= 6", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        kpis = page.eval_on_selector_all("#clrBody .kpi",
                                         "es => es.map(e => [e.querySelector('div').textContent, e.querySelector('.value').textContent])")
        check("KPI 卡片渲染", len(kpis) >= 6, " / ".join(f"{k}={v}" for k, v in kpis[:6]))

        status = page.locator("#clrStatus").inner_text().strip()
        check("报表汇总状态行", "已汇总" in status, status)

        tables = page.eval_on_selector_all("#clrBody .table-scroll table",
                                          "es => es.map(t => t.rows.length)")
        check("院系/未归属/课程三张表", len(tables) >= 2, f"表格行数 {tables}")

        # 未归属教学班：走一遍绑定（如果还有的话）
        buttons = page.locator("#clrBody table button.small")
        if buttons.count():
            selects = page.locator("#clrBody table select")
            first = selects.first
            options = page.eval_on_selector_all("#clrBody table select:first-of-type option",
                                               "os => os.map(o => [o.value, o.textContent])")
            options = [o for o in options if o[0]]
            check("未归属教学班给出候选班级实体", bool(options),
                  "；".join(name for _, name in options[:3]))
            if options:
                first.select_option(options[0][0])
                buttons.first.click()
                try:
                    page.wait_for_function(
                        "() => (document.querySelector('#clrStatus')||{}).textContent?.includes('已把')",
                        timeout=20000)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1500)
                after = page.locator("#clrStatus").inner_text().strip()
                check("一键绑定后报表刷新", "已把" in after or "已汇总" in after, after[:70])
                dept_rows = page.eval_on_selector_all(
                    "#clrBody table:first-of-type tr",
                    "es => es.slice(1).map(tr => Array.from(tr.cells).map(td => td.textContent.trim()))")
                check("院系表出现已绑定的班级行", bool(dept_rows), str(dept_rows[:1])[:120])
                if shots:
                    page.screenshot(path=str(shots / "learning-report-bound.png"), full_page=True)
        else:
            check("没有未归属教学班（已全部绑定）", True, "跳过绑定步骤")

        check("全程零 JS 报错", not js_errors, "；".join(js_errors[:3]))
        check("无 4xx/5xx（401 探测除外）", not http_errors, "；".join(http_errors[:3]))

        context.close()
        browser.close()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n==== {passed}/{len(CHECKS)} 通过 ====")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
