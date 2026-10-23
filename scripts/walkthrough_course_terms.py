"""课程挂学期实体（FR-U04 v2）· 真库浏览器走查。

验证四件事：课程卡上的学期来自学期实体（不是自由文本）、新建课程只能从学期下拉里选、
绑定后报表按学期筛选能筛到这门课、旧课程的历史学期文本已被迁移回填成学期实体。

用法（先起服务，默认连 8003 以免打扰常驻的 8000/8001/8002）：
    python -B -X utf8 scripts/walkthrough_course_terms.py [--base http://127.0.0.1:8003]

注意：脚本会在真库新建一门「走查临时课-<时间>」并在结束时停用（停用不删行，
课程卡上仍可见，只是状态变成已停用）。如需彻底清掉，请手工处理。
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


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" · " + detail) if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("WALK_BASE", "http://127.0.0.1:8003"))
    parser.add_argument("--teacher", default="staff01")
    parser.add_argument("--teacher-password", default=os.environ.get("WALK_TEACHER_PW", "Teacher#2026x"))
    parser.add_argument("--shots", default=os.environ.get("WALK_SHOT_DIR", ""))
    parser.add_argument("--db", default=os.environ.get("TAXPEARLS_DB", ""),
                        help="真库路径；给了就把本次的「走查临时课-*」清掉，不留脏数据")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    from playwright.sync_api import sync_playwright

    js_errors, http_errors = [], []
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    temp_name = f"走查临时课-{stamp}"

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

        # ---- 1. 课程管理视图里，既有课程的学期来自学期实体
        nav = page.locator('#universityNav [data-university-view="courses"]')
        if nav.count():
            nav.first.click()
        else:
            page.locator('[data-workspace-view="courses"]').first.click()
        page.wait_for_selector("#courses .item", timeout=15000)
        legacy = page.evaluate("""async () => {
            const r = await fetch('/api/training/staff/courses');
            const data = await r.json();
            const c = data.courses.find(x => x.name.includes('涉税风险识别'));
            return c ? {college: c.college, term_id: c.term_id, semester: c.semester,
                        term_name: c.term_name} : null;
        }""")
        check("既有课程已挂学期实体", bool(legacy and legacy["term_id"]), str(legacy))
        check("既有课程展示文本=学期实体名",
              bool(legacy and legacy["semester"] == legacy["term_name"]), str(legacy and legacy["semester"]))
        card_text = page.locator("#courses .item").first.inner_text()
        check("课程卡渲染学期（非「未挂学期」）",
              bool(legacy) and legacy["semester"] in card_text, card_text.replace("\n", " / ")[:80])
        if shots:
            page.locator("#courses").screenshot(path=str(shots / "course-terms-list.png"))

        # ---- 2. 新建课程表单的学期是下拉，且选项来自学期实体
        page.locator('[data-act="show-course-form"]').first.click()
        page.wait_for_selector("#cf2-term", state="visible", timeout=10000)
        options = page.eval_on_selector_all(
            "#cf2-term option", "nodes => nodes.map(n => ({value: n.value, text: n.textContent}))")
        real_terms = [o for o in options if o["value"]]
        check("学期下拉有选项（来自学期实体）", len(real_terms) >= 1, str([o["text"] for o in options]))
        check("学期下拉不含自由文本输入", page.locator("#cf2-semester").count() == 0)
        if shots:
            page.locator("#course-form").screenshot(path=str(shots / "course-terms-form.png"))

        # ---- 3. 真建一门课，学期取当前学期
        page.fill("#cf2-name", temp_name)
        page.fill("#cf2-classes", "会计2601")
        page.fill("#cf2-desc", "课程挂学期实体的走查看查数据，可停用")
        page.locator('[data-act="create-course"]').first.click()
        page.wait_for_timeout(1500)
        created = page.evaluate("""async (name) => {
            const r = await fetch('/api/training/staff/courses');
            const data = await r.json();
            const c = data.courses.find(x => x.name === name);
            return c ? {id: c.id, term_id: c.term_id, semester: c.semester, term_name: c.term_name} : null;
        }""", temp_name)
        check("新建课程成功且挂上当前学期", bool(created and created["term_id"]), str(created))
        check("新建课程学期文本=学期实体名",
              bool(created and created["semester"] == created["term_name"]), str(created and created["semester"]))

        # ---- 4. 报表按学期筛选能筛到这门课
        filtered = page.evaluate("""async ({college, term}) => {
            const url = '/api/training/staff/reports/learning?college=' + encodeURIComponent(college)
                      + (term ? '&term_id=' + encodeURIComponent(term) : '');
            const r = await fetch(url);
            if (!r.ok) return {status: r.status};
            const data = await r.json();
            return {status: 200, courses: data.courses.map(c => c.name),
                    terms: data.options.terms.length};
        }""", {"college": (legacy or {}).get("college", ""),
                "term": (created or {}).get("term_id", "")})
        check("报表接口按学期筛选返回 200", filtered.get("status") == 200, str(filtered.get("status")))
        check("筛选结果含新建课程", temp_name in (filtered.get("courses") or []),
              str((filtered.get("courses") or [])[:4]))

        # ---- 5. 停用走查数据
        if created:
            page.evaluate("""async (id) => {
                await fetch('/api/training/staff/courses/' + id, {method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({active: false})});
            }""", created["id"])
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(800)
            inactive = page.evaluate("""async (name) => {
                const r = await fetch('/api/training/staff/courses');
                const data = await r.json();
                const c = data.courses.find(x => x.name === name);
                return c ? {active: c.active, term_id: c.term_id} : null;
            }""", temp_name)
            check("走查临时课已停用（停用不删行，学期仍在）",
                  bool(inactive and inactive["active"] == 0 and inactive["term_id"]),
                  str(inactive))

        check("页面无 JS 报错", not js_errors, "; ".join(js_errors[:2]))
        check("无未预期 4xx/5xx", not http_errors, "; ".join(http_errors[:3]))
        browser.close()

    if args.db and Path(args.db).exists():
        import sqlite3
        conn = sqlite3.connect(args.db)
        try:
            with conn:
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM course WHERE name LIKE '走查临时课-%'")]
                for cid in ids:
                    conn.execute("DELETE FROM course_class WHERE course_id=?", (cid,))
                    conn.execute("DELETE FROM course WHERE id=?", (cid,))
                print(f"清理走查临时课 {len(ids)} 门（仅限名称前缀 走查临时课-）")
        finally:
            conn.close()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
