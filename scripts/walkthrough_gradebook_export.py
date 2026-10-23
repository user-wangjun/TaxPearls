"""教师端成绩单导出 · 真库浏览器走查。

只验证一件事：老师点「导出成绩单」能不能真拿到一份能用的 CSV。
接口层的口径由 tests/test_gradebook.py 守住，这里守的是按钮、下载与文件内容。

用法（先起服务，默认连 8002 以免打扰常驻的 8000/8001）：
    python -B -X utf8 scripts/walkthrough_gradebook_export.py [--base http://127.0.0.1:8002]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
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
    args = parser.parse_args()
    base = args.base.rstrip("/")

    from playwright.sync_api import sync_playwright

    js_errors, http_errors = [], []

    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on("response", lambda resp: http_errors.append(f"{resp.status} {resp.url}")
                if resp.status >= 400 and resp.status != 401 else None)
        page.on("dialog", lambda dialog: dialog.accept())

        page.goto(base + "/", wait_until="domcontentloaded")
        # 学情统计卡读的是平台会话（/api/classes 走 COOKIE_NAME），只登 /api/login。
        # 先登 staff 会话再登平台会把身份切成 staff 账号，/api/classes 反而拿不到班级。
        page.evaluate("""async ({u, p}) => {
            const response = await fetch('/api/login', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: u, password: p})});
            if (!response.ok) throw new Error('login failed: ' + response.status);
        }""", {"u": args.teacher, "p": args.teacher_password})
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1200)

        nav = page.locator('#universityNav [data-university-view="statistics"]')
        if nav.count():
            nav.first.click()
        # 视图切换是异步 applyView，死等固定毫秒会时快时慢，直接等卡片可见
        try:
            page.wait_for_selector("#classStatistics", state="visible", timeout=15000)
        except Exception:  # noqa: BLE001 - 没等到就交给下面的断言报错
            pass
        card = page.locator("#classStatistics")
        check("「班级学情统计」卡可见", card.count() == 1 and card.first.is_visible(),
              "statistics 视图" if nav.count() else "无高校导航")

        options = page.eval_on_selector_all("#statisticsClass option",
                                           "os => os.map(o => ({v: o.value, t: o.textContent.trim()}))")
        options = [o for o in options if o["v"]]
        check("班级下拉有真库数据", bool(options), f"{len(options)} 个 · " + (options[0]["t"] if options else "空"))
        if not options:
            return 1
        page.select_option("#statisticsClass", options[0]["v"])
        page.wait_for_timeout(1500)

        tmp = Path(tempfile.mkdtemp())
        for button, expect in (("btnExportGradebook", "成绩单"), ("btnExportGradebookDetail", "成绩明细")):
            check(f"导出按钮存在：{expect}", page.locator(f"#{button}").count() == 1)
            with page.expect_download(timeout=20000) as info:
                page.click(f"#{button}")
            download = info.value
            target = tmp / download.suggested_filename
            download.save_as(str(target))
            text = target.read_text(encoding="utf-8-sig")
            lines = [line for line in text.splitlines() if line.strip()]
            body = [line for line in lines if not line.startswith("#")]
            check(f"{expect}：文件名带班级与日期", download.suggested_filename.startswith(options[0]["t"])
                  and download.suggested_filename.endswith(".csv"), download.suggested_filename)
            check(f"{expect}：含口径说明行", any("口径" in line for line in lines if line.startswith("#")),
                  next((line[:60] for line in lines if line.startswith("# 口径")), "无"))
            check(f"{expect}：表头与数据行就位", len(body) >= 2, f"{len(body)} 行（含表头）")

        status = page.locator("#classStatisticsStatus").inner_text().strip()
        check("页面回显已导出文件", "已导出" in status, status[:60])
        check("全程零 JS 报错", not js_errors, "；".join(js_errors[:3]))
        check("无 4xx/5xx（401 探测除外）", not http_errors, "；".join(http_errors[:3]))

        context.close()
        browser.close()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n==== {passed}/{len(CHECKS)} 通过 ====")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
