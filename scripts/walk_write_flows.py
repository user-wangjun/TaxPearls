"""九视图走查的第二半：写操作链路（会把数据写进走查库，别对真库跑）。

读接口全绿不等于「点得动」。这里按人工走查的真实顺序跑三条主角链路：
  1. 教师把实操案例发布成班级作业 → 配量规 → 学生五步提交 → 教师评分 → 学生回看
  2. 学生刷题：抽题 → 作答 → 提交判分 → 解析开放
  3. 教师新建教学单元 → 挂案例

用法：
    python scripts/walk_write_flows.py --staff-password xxx --student-password yyy \
        --college 演示财经职业学院
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, BAD = "✅", "❌"

RESULTS: list[tuple[bool, str]] = []


class Walker:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:
            return 0, str(exc)


def step(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((ok, label))
    print(f"  {OK if ok else BAD} {label}" + (f"  {detail}" if detail else ""))
    return ok


def _list_of(payload: object, *keys: str) -> list:
    """后端这几个列表接口返回形状不统一：有的裸数组、有的 {x:[…]}。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (*keys, "items", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def body(raw: str, limit: int = 140) -> str:
    try:
        payload = json.loads(raw)
    except Exception:
        return raw[:limit].replace("\n", " ")
    if isinstance(payload, dict) and "detail" in payload:
        return f"detail={payload['detail']}"[:limit]
    keys = ",".join(list(payload)[:6]) if isinstance(payload, dict) else f"{len(payload)} 项"
    return f"{{ {keys} }}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--staff", default="staff01")
    ap.add_argument("--staff-password", required=True)
    ap.add_argument("--student", default="2026001")
    ap.add_argument("--student-password", required=True)
    ap.add_argument("--college", required=True)
    args = ap.parse_args()
    run_tag = time.strftime("%H%M%S")  # 让脚本可重复跑：名字不撞 UNIQUE 约束

    t = Walker(args.base)
    st, raw = t.call("POST", "/api/training/staff/auth/login",
                     {"username": args.staff, "password": args.staff_password})
    if st != 200:
        print(f"{BAD} 教师登录失败 {st} {raw[:150]}")
        return 1
    step(True, "教师登录", f"{args.staff}")

    st, raw = t.call("GET", "/api/training/staff/certificates?include_inactive=true")
    certs = json.loads(raw).get("certificates", []) if st == 200 else []
    cert_id = certs[0]["id"]
    step(bool(certs), "取证书", f"{certs[0].get('name')}")

    # ---------------- 链路 1：案例 → 任务 → 交付 → 评分 ----------------
    print("\n── 链路1：教师发布案例任务 → 学生五步提交 → 教师评分 ──")

    st, raw = t.call("GET", f"/api/training/teaching/publish-cases?certificate_id={cert_id}")
    cases = json.loads(raw).get("items", []) if st == 200 else []
    step(bool(cases), "取已发布案例",
         f"{len(cases)} 个：{'、'.join(c['name'] for c in cases[:3])}")
    if not cases:
        return report()
    case_id = cases[0]["id"]

    st, raw = t.call("GET", "/api/classes")
    payload = json.loads(raw) if st == 200 else {}
    classes = _list_of(payload, "classes")
    step(bool(classes), "取班级", f"{len(classes)} 个")
    class_id = (classes[0] or {}).get("id", "") if classes else ""

    st, raw = t.call("GET", "/api/audits")
    payload = json.loads(raw) if st == 200 else {}
    audits = _list_of(payload, "audits")
    step(bool(audits), "取数据底座（仿真案例）", f"{len(audits)} 个")
    audit_id = (audits[0] or {}).get("id", "") if audits else ""

    if not (class_id and audit_id):
        step(False, "缺班级或数据底座，链路 1 无法继续")
        return report()

    st, raw = t.call("POST", f"/api/training/teaching/cases/{case_id}/publish", {
        "class_id": class_id, "audit_id": audit_id,
        "title": "走查：账申勾稽实操 " + run_tag, "published": True,
        "steps": ["receive", "identify", "reconcile", "judge", "evidence"],
        "dimensions": ["material", "reconcile", "basis", "trace"],
    })
    publish_ok = st == 200
    task_id, assignment_id = "", ""
    if publish_ok:
        payload = json.loads(raw)
        task_id = payload.get("id", "")
        assignment_id = payload.get("assignment_id", "")
    step(publish_ok, "发布案例成作业", f"task={task_id[:8]} assignment={assignment_id[:8]}")

    if task_id:
        st, raw = t.call("GET", f"/api/training/teaching/case-tasks/{task_id}/rubric")
        step(st == 200, "读默认量规", body(raw))
        st, raw = t.call("PUT", f"/api/training/teaching/case-tasks/{task_id}/rubric", {
            "note": "走查：勾稽与依据权重加倍",
            "dimensions": [
                {"key": "material", "label": "材料与要素", "weight": 2.0},
                {"key": "reconcile", "label": "勾稽与口径", "weight": 3.0},
                {"key": "basis", "label": "判定与依据", "weight": 3.0},
                {"key": "trace", "label": "留痕与建议", "weight": 2.0},
            ]})
        step(st == 200, "改量规权重", body(raw))

    # 学生侧
    s = Walker(args.base)
    st, raw = s.call("POST", "/api/training/auth/login",
                     {"student_no": args.student, "password": args.student_password,
                      "college": args.college})
    step(st == 200, "学生登录", f"{args.student}")
    if st != 200:
        return report()

    if assignment_id:
        st, raw = s.call("GET", f"/api/assignments/{assignment_id}/case")
        step(st == 200, "学生打开该作业看到案例", body(raw))
        st, raw = s.call("PUT", f"/api/assignments/{assignment_id}/case-delivery", {
            "step": "receive", "content": "收到科目余额表、发票清单、申报表三份材料。"})
        step(st == 200, "学生提交第①步交付物", body(raw))
        ok_step2 = False
        for step_name, text in (("identify", "识别出购销双方、税率与属期"),
                                ("reconcile", "账面收入与申报收入差 3.2 万，已做差异调节"),
                                ("judge", "判断为跨期确认，需调整申报"),
                                ("evidence", "附底稿编号 BG-2026-0912 与调节表")):
            st, raw = s.call("PUT", f"/api/assignments/{assignment_id}/case-delivery",
                             {"step": step_name, "content": text})
            step(st == 200, f"学生提交{step_name}", "" if st == 200 else body(raw))
            ok_step2 = ok_step2 or st == 200
        st, raw = s.call("GET", f"/api/assignments/{assignment_id}/rubric")
        step(st in (200, 403, 404), "学生查看量规得分（未开放策略下 200/403/404 均可）",
             f"{st} {body(raw, 80)}")

    if task_id:
        st, raw = t.call("GET", f"/api/training/teaching/case-tasks/{task_id}/deliveries")
        deliveries = json.loads(raw).get("items", []) if st == 200 else []
        # 交付物在 row["steps"][i]["content"]，不是 row["content"]
        filled = [r for r in deliveries
                  if any((s or {}).get("content") for s in (r.get("steps") or []))]
        step(st == 200 and bool(filled), "教师看到学生交付物",
             f"名册 {len(deliveries)} 人，其中有填写 {len(filled)} 人")
        target = ""
        for item in filled:
            if item.get("student_id"):
                target = item["student_id"]
                break
        if target:
            st, raw = t.call(
                "PUT", f"/api/training/teaching/case-tasks/{task_id}/rubric-scores", {
                    "student_id": target,
                    "scores": [
                        {"dimension": "material", "score": 2, "comment": "材料齐备。"},
                        {"dimension": "reconcile", "score": 1, "comment": "差异调节过程缺一步。"},
                    ]})
            step(st == 200, "教师按量规评分", body(raw, 90))
            st, raw = t.call("GET",
                             f"/api/training/teaching/case-tasks/{task_id}/rubric-scores"
                             f"?student_id={urllib.parse.quote(target)}")
            step(st == 200, "回读评分", body(raw, 90))
        else:
            step(False, "拿不到有交付物的学生 id，跳过评分")

    # ---------------- 链路 2：学生刷题 ----------------
    print("\n── 链路2：学生刷题 → 作答 → 判分 → 解析 ──")
    st, raw = s.call("GET", f"/api/training/my/knowledge-points?certificate_id={cert_id}")
    kps = json.loads(raw).get("knowledge_points", []) if st == 200 else []
    step(bool(kps), "取知识点", f"{len(kps)} 条")

    # 抽题能不能抽到，取决于知识点有没有挂规则。连续试前 20 个，顺带量一下覆盖率。
    probe_kps = kps[:20]
    can_start: list[tuple[str, str, dict]] = []
    for item in probe_kps:
        st_probe, raw_probe = s.call("POST", "/api/training/my/practice/start",
                                     {"certificate_id": cert_id,
                                      "knowledge_point_id": item["id"]})
        if st_probe == 200:
            can_start.append((item["id"], str(item.get("name", "")),
                              (json.loads(raw_probe).get("attempt") or {})))
    print(f"      （抽查前 {len(probe_kps)} 个知识点，能抽到题 {len(can_start)} 个："
          f"{'、'.join(n for _, n, _ in can_start[:5]) or '无'}）")
    note = "" if len(can_start) >= 3 else "  ← 演示前须先在『考纲知识点』给要练的知识点挂规则"
    step(len(can_start) >= 3, "知识点可抽题覆盖率", f"{len(can_start)}/{len(probe_kps)}{note}")

    if can_start:
        kp_id, kp_name, attempt = can_start[0]
        attempt_id = attempt.get("id", "")
        step(bool(attempt_id), "抽题",
             f"知识点「{kp_name}」 可选规则 {len(attempt.get('rule_catalog') or [])} 条"
             f" 材料主体={(attempt.get('materials') or {}).get('company', {}).get('name', '—')}")
        if attempt_id:
            catalog = attempt.get("rule_catalog") or []
            picks = [catalog[0]["id"]] if catalog else []
            st, raw = s.call("POST", f"/api/training/my/practice/{attempt_id}/submit",
                             {"selected_rule_ids": picks})
            step(st == 200, "提交作答（仿真题=选规则）", body(raw))
            if st == 200:
                payload = json.loads(raw).get("attempt") or {}
                print(f"      判分={payload.get('score')} "
                      f"解析长度={len(str(payload.get('explanation', '')))}"
                      f" 状态={payload.get('status')}")

    # ---------------- 链路 3：教师编单元挂案例 ----------------
    print("\n── 链路3：教师新建教学单元 → 挂案例 ──")
    st, raw = t.call("POST", "/api/training/teaching/units", {
        "certificate_id": cert_id, "name": "走查单元·凭证到结论 " + run_tag, "stage": "conclusion",
        "standard": "判断能回溯到规则、规则能落到流水",
        "knowledge_point_ids": [], "rule_codes": ["R-002"],
        "deliverables": "判断链路说明 + 依据快照 id"})
    unit_payload = json.loads(raw) if st == 200 else {}
    unit_id = unit_payload.get("unit", {}).get("id", "")
    step(st == 200, "新建教学单元", f"id={unit_id[:8]}")
    if unit_id:
        st, raw = t.call("PUT", f"/api/training/teaching/units/{unit_id}",
                         {"status": "published"})
        step(st == 200, "发布教学单元", body(raw, 60))
    st, raw = t.call("POST", "/api/training/teaching/cases", {
        "certificate_id": cert_id, "name": "走查案例·留痕检查 " + run_tag, "unit_id": unit_id or None,
        "scenario": "走查用脱敏样例", "rule_codes": ["R-002"],
        "materials": [{"name": "判断底稿", "note": "走查生成"}],
        "desensitization_note": "走查临时数据"})
    new_case_id = json.loads(raw).get("case", {}).get("id", "") if st == 200 else ""
    step(st == 200, "新建实操案例并挂到单元", f"case={new_case_id[:8]}")
    if new_case_id:
        st, raw = t.call("PUT", f"/api/training/teaching/cases/{new_case_id}",
                         {"status": "published"})
        step(st == 200, "发布实操案例", body(raw, 60))
    st, raw = t.call("GET", "/api/training/teaching/rule-evidence/R-002")
    evidence = json.loads(raw).get("evidence", {}) if st == 200 else {}
    step(st == 200, "查依据链",
         f"legal_status={evidence.get('legal_status')} "
         f"快照={str(evidence.get('snapshot_id'))[:28]} "
         f"指标={len(evidence.get('metrics') or [])} 条款={len(evidence.get('clauses') or [])}")

    return report()


def report() -> int:
    bad = [label for ok, label in RESULTS if not ok]
    print(f"\n{'─' * 60}\n合计 {len(RESULTS)} 步，失败 {len(bad)} 步")
    for label in bad:
        print("  ✗", label)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
