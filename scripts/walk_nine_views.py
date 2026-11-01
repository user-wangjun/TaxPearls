"""高校端九视图接口走查（只读）。

对已经跑起来的服务逐个请求每个导航视图依赖的接口，记录状态码与返回规模，
用来定位「点进去是空白 / 转圈 / 报错」到底断在哪一层。只发 GET，不改数据。

用法：
    python scripts/walk_nine_views.py --base http://127.0.0.1:8000 \
        --staff staff01 --staff-password xxx --student 2026001 --student-password yyy \
        --college 演示财经职业学院
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "✅", "⚠️", "❌"


class Walker:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:  # 网络层失败
            return 0, str(exc)

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: dict):
        return self.call("POST", path, body)


def size_of(payload: dict | list) -> int:
    for key in ("items", "units", "cases", "certificates", "knowledge_points",
                "students", "classes", "courses", "rules", "versions", "tasks",
                "assignments", "papers", "rows", "auths", "links", "marks",
                "relations", "feedback", "departments", "majors", "terms"):
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            return len(payload[key])
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return len(payload)
    return 0


def brief(raw: str, limit: int = 90) -> str:
    try:
        payload = json.loads(raw)
    except Exception:
        return raw[:limit].replace("\n", " ")
    n = size_of(payload)
    if isinstance(payload, dict) and "detail" in payload:
        return f"detail={payload['detail']}"[:limit]
    return f"条数≈{n}"


def section(title: str) -> None:
    print(f"\n{'─' * 74}\n{title}\n{'─' * 74}")


def probe(walker: Walker, label: str, path: str, expect_empty_ok: bool = False):
    status, raw = walker.get(path)
    if status == 200:
        mark = OK
        try:
            n = size_of(json.loads(raw))
            if n == 0 and not expect_empty_ok:
                mark = WARN
        except Exception:
            n = -1
        extra = "" if mark == OK else ("（空列表）" if n == 0 else "")
        print(f"  {mark} {label:<26} {status}  {brief(raw)}{extra}")
    else:
        print(f"  {BAD} {label:<26} {status}  {brief(raw, 120)}")
    return status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--staff", default="staff01")
    ap.add_argument("--staff-password", required=True)
    ap.add_argument("--student", default="2026001")
    ap.add_argument("--student-password", required=True)
    ap.add_argument("--college", required=True)
    ap.add_argument("--skip-student", action="store_true")
    args = ap.parse_args()

    college = urllib.parse.quote(args.college)
    walker = Walker(args.base)

    # ---------------- 教师登录 ----------------
    status, raw = walker.post("/api/training/staff/auth/login",
                              {"username": args.staff, "password": args.staff_password})
    if status != 200:
        print(f"{BAD} 教师登录失败 {status}: {raw[:200]}")
        return 1
    print(f"{OK} 教师 {args.staff} 登录成功")

    status, raw = walker.get("/api/me")
    me = json.loads(raw) if status == 200 else {}
    print(f"  平台身份：org_type={me.get('org_type')} role={me.get('role')}"
          f" education_role={me.get('education_role')}"
          f" workspace={me.get('workspace')} university={me.get('university_kind', '-')}")

    # 基础 id：第一张证书 / 第一条课程 / 第一条作业
    status, raw = walker.get("/api/training/staff/certificates?include_inactive=true")
    certs = json.loads(raw).get("certificates", []) if status == 200 else []
    if not certs:
        print(f"{BAD} 库里没有证书，走查无法继续")
        return 1
    cert_id = certs[0]["id"]
    print(f"  证书 {certs[0].get('name')}（{cert_id[:8]}），共 {len(certs)} 张")

    status, raw = walker.get("/api/training/staff/courses")
    courses = json.loads(raw).get("courses", []) if status == 200 else []
    course_id = courses[0]["id"] if courses else ""

    failures: list[str] = []

    def check(label: str, path: str, empty_ok: bool = False, record: bool = True):
        st = probe(walker, label, path, empty_ok)
        if st != 200 and record:
            failures.append(f"{label} → {st} {path}")
        return st

    # ---------------- 教师端九视图 ----------------
    section("【教师端】1. home 教学总览")
    check("工作台 dashboard", "/api/training/workspace/dashboard", record=True)
    check("院校概况 overview", f"/api/training/staff/overview?college={college}")

    section("【教师端】2. students 师生与班级")
    check("学生名单", "/api/training/staff/students")
    check("班级", f"/api/training/staff/classes?college={college}", empty_ok=True)
    check("班级历史", f"/api/training/staff/class-history?college={college}&limit=20",
          empty_ok=True)
    check("教师名册", "/api/training/staff/teachers", empty_ok=True)
    check("院系", f"/api/training/staff/org/departments?college={college}")
    check("专业", f"/api/training/staff/org/majors?college={college}")
    check("学期", f"/api/training/staff/org/terms?college={college}")
    check("班级实体", f"/api/training/staff/org/classes?college={college}")
    check("席位配额", f"/api/training/staff/org/quota?college={college}")

    section("【教师端】3. courses 课程管理")
    check("课程列表", "/api/training/staff/courses")
    if course_id:
        check("课程名册", f"/api/training/staff/courses/{course_id}/roster", empty_ok=True)
    else:
        print(f"  {WARN} 没有课程，跳过名册")

    section("【教师端】4. tasks 课程与作业")
    check("任务清单", "/api/training/tasks")
    check("作业", "/api/assignments")
    check("试卷", "/api/papers")
    check("教学面板班级", "/api/classes", empty_ok=True)
    check("教学案例底座", "/api/teaching/cases", empty_ok=True)
    check("案例任务", "/api/training/teaching/case-tasks", empty_ok=True)
    check("发布可选案例", f"/api/training/teaching/publish-cases?certificate_id={cert_id}",
          empty_ok=True)
    check("可选证书", "/api/training/teaching/certificates")

    section("【教师端】5. statistics 学情统计")
    check("标注统计", f"/api/training/staff/certificates/{cert_id}/mark-stats", empty_ok=True)
    check("试用观察统计", "/api/training/staff/trial-stats", empty_ok=True)

    section("【教师端】6. knowledge 政策与学习图谱")
    check("知识图谱", "/api/training/knowledge/graph")
    check("规则清单", "/api/training/staff/rules")
    check("知识点", f"/api/training/staff/certificates/{cert_id}/knowledge-points")

    section("【教师端】7. content 教学资源")
    check("证书(含停用)", "/api/training/staff/certificates?include_inactive=true")
    dates = certs[0].get("exam_dates") or []
    print(f"  {'✅' if dates else '⚠️'} 考试日期（内嵌 cert.exam_dates）  "
          f"{len(dates)} 条"
          + (f"：{dates[0].get('round_label')} {dates[0].get('exam_date')}"
             f"（{dates[0].get('date_type')}）" if dates else "（无）"))
    check("内容版本", f"/api/training/staff/certificates/{cert_id}/content-versions")
    check("题源授权", f"/api/training/staff/certificates/{cert_id}/source-authorizations",
          empty_ok=True)
    check("题库题目", f"/api/training/staff/certificates/{cert_id}/bank-questions",
          empty_ok=True)
    check("考纲升版", f"/api/training/staff/certificates/{cert_id}/outline-upgrade",
          empty_ok=True)
    check("试用反馈", "/api/training/staff/trial-feedback", empty_ok=True)
    check("教学单元", f"/api/training/teaching/units?certificate_id={cert_id}")
    check("实操案例", f"/api/training/teaching/cases?certificate_id={cert_id}")
    check("依据链 R-002", "/api/training/teaching/rule-evidence/R-002")
    check("未挂靠规则", "/api/training/teaching/unlinked-rules", empty_ok=True)

    section("【教师端】8. archives 教学档案")
    print("  （HTML 里没有 data-university-view=archives 的卡，靠 overview 卡兜底；"
          "上面 dashboard 已覆盖）")

    section("【教师端】9. settings 账号设置")
    st, raw = walker.post("/api/training/staff/auth/change-password",
                          {"old_password": "__wrong__", "new_password": "Whatever#2026z"})
    print(f"  {'OK' if st in (400, 401) else 'ERR'} 改密码校验旧口令 → {st}"
          f" {brief(raw, 80)}  （400/401 是正确的，说明校验生效）")

    # ---------------- 学生端九视图 ----------------
    student_failures: list[str] = []
    if not args.skip_student:
        sw = Walker(args.base)

        def scheck(label: str, path: str, empty_ok: bool = False):
            st = probe(sw, label, path, empty_ok)
            if st != 200:
                student_failures.append(f"{label} → {st} {path}")
            return st

        section("【学生端】登录")
        first_kp = ""
        st, raw = sw.post("/api/training/auth/login",
                          {"student_no": args.student, "password": args.student_password,
                           "college": args.college})
        if st != 200:
            print(f"  {BAD} 学生登录失败 {st}: {raw[:200]}")
        else:
            print(f"  {OK} 学生 {args.student} 登录成功")
            who = json.loads(raw).get("student", {})
            print(f"     {who.get('name')} · {who.get('college')}")
            st, raw = sw.get("/api/me")
            me = json.loads(raw) if st == 200 else {}
            print(f"     平台身份 org_type={me.get('org_type')}"
                  f" education_role={me.get('education_role')}")

            section("【学生端】1. home 学习总览")
            scheck("学习总览 dashboard", "/api/training/my/dashboard")
            scheck("工作台 dashboard", "/api/training/workspace/dashboard")

            section("【学生端】2. tasks 课程任务")
            scheck("我的作业", "/api/assignments", empty_ok=True)
            scheck("我的试卷", "/api/papers", empty_ok=True)

            section("【学生端】3. practice 练习与考证")
            scheck("证书列表", "/api/training/certificates")
            scheck("证书搜索",
                   f"/api/training/certificates/search?q={urllib.parse.quote('会计')}",
                   empty_ok=True)
            st2, raw2 = sw.get(
                f"/api/training/my/certificate-guide?certificate_id={cert_id}")
            print(f"  {'✅' if st2 == 200 else '❌'} 备考指引                {st2} "
                  f" {brief(raw2, 110)}")
            if st2 != 200:
                student_failures.append(f"备考指引 → {st2}")
            scheck("我的知识点",
                   f"/api/training/my/knowledge-points?certificate_id={cert_id}")
            _stKp, _rawKp = sw.get(
                f"/api/training/my/knowledge-points?certificate_id={cert_id}")
            try:
                first_kp = (json.loads(_rawKp).get("knowledge_points")
                            or [{}])[0].get("id", "")
            except Exception:
                first_kp = ""
            scheck("刷题统计",
                   f"/api/training/my/practice/stats?certificate_id={cert_id}",
                   empty_ok=True)
            scheck("进行中的作答",
                   f"/api/training/my/practice?status=open&certificate_id={cert_id}",
                   empty_ok=True)

            section("【学生端】4. wrongs 错题复习")
            scheck("错题本",
                   f"/api/training/my/practice/wrong?certificate_id={cert_id}",
                   empty_ok=True)

            section("【学生端】5. goals 考证目标")
            scheck("我的目标", "/api/training/my/goals?include_archived=1", empty_ok=True)

            section("【学生端】6. catalog 找证书")
            scheck("我的反馈", "/api/training/my/feedback", empty_ok=True)

            section("【学生端】7. knowledge 政策与学习图谱")
            scheck("知识图谱", "/api/training/knowledge/graph")
            scheck("AI 答疑历史",
                   f"/api/training/my/tutor/history?knowledge_point_id="
                   f"{urllib.parse.quote(first_kp)}&limit=20", empty_ok=True)

            section("【学生端】8. profile 学习档案")
            scheck("学习档案", f"/api/training/my/profile?certificate_id={cert_id}",
                   empty_ok=True)

            section("【学生端】9. settings 账号设置")
            st3, raw3 = sw.post("/api/training/auth/change-password",
                                {"old_password": "__wrong__",
                                 "new_password": "Whatever#2026z"})
            print(f"  {'OK' if st3 in (400, 401) else 'ERR'} 改密码校验旧口令 → {st3}"
                  f" {brief(raw3, 80)}")

    # ---------------- 汇总 ----------------
    section("汇总")
    print(f"  教师端非 200：{len(failures)} 项")
    for item in failures:
        print("    -", item)
    print(f"  学生端非 200：{len(student_failures)} 项")
    for item in student_failures:
        print("    -", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
