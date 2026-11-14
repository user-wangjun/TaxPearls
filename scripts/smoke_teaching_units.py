"""B1 教学单元/实操案例端到端冒烟：真实 app + 临时库。

用法（临时库，不碰真库）：
    python -B -X utf8 scripts/smoke_teaching_units.py

检查项：路由在真实 app 上注册可用、教师登录后单元/案例全链路（建→查→发布→
归档守卫）、校验拒绝（未知规则/跨证书知识点/未绑定结论/非法三态）、
依据链（快照+指标+待专业复核）、未挂靠规则含 R-024、未登录 401。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" · " + detail) if detail else ""))


def main() -> int:
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    os.environ["TAXPEARLS_DB"] = str(Path(tmp.name) / "smoke.db")
    os.environ["TAXPEARLS_AI_ENABLED"] = "0"
    os.environ["TAXPEARLS_NOTIFICATION_EMAIL_ENABLED"] = "0"

    from fastapi.testclient import TestClient
    from argon2 import PasswordHasher

    from src import settings  # noqa: F401 - FIELD_KEY
    from webapp import app as module

    now = "2026-10-22T10:00:00"
    staff_id, cert, kp = (uuid.uuid4().hex for _ in range(3))
    password = "Smoke-units-9007"
    with TestClient(module.app) as client:  # 进入 lifespan（建库/建表）
        store = module.store
        with store.connect() as db:
            db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                       (staff_id, "smoke-admin", PasswordHasher().hash(password),
                        "冒烟管理员", "示例大学", None, None, None, now))
            db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                       (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", now, now))
            db.execute(
                "INSERT INTO knowledge_point (id,certificate_id,code,name,subject,"
                "active,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
                (kp, cert, "KP-01", "增值税申报", "初级会计实务", now, now))
            db.execute(
                "INSERT INTO knowledge_point_link (id,knowledge_point_id,target_type,"
                "target_id,active,created_by,created_at) VALUES (?,?,?,?,1,NULL,?)",
                (uuid.uuid4().hex, kp, "rule", "R-002", now))

        # 未登录 401
        res = client.get(f"/api/training/teaching/units?certificate_id={cert}")
        check("未登录访问单元清单返回 401", res.status_code == 401, str(res.status_code))

        res = client.post("/api/training/staff/auth/login",
                          json={"username": "smoke-admin", "password": password})
        check("教师登录", res.status_code == 200, res.text[:120])

        # 建单元
        res = client.post("/api/training/teaching/units", json={
            "certificate_id": cert, "name": "数据层·账申勾稽", "stage": "data",
            "standard": "账表票申报四方口径一致", "knowledge_point_ids": [kp],
            "rule_codes": ["R-002", "R-005"], "deliverables": "勾稽底稿"})
        check("创建单元", res.status_code == 200, res.text[:200])
        unit = res.json()["unit"]
        check("单元草稿态+数据层标签", unit["status"] == "draft"
              and unit["stage_label"] == "数据层")

        # 校验拒绝
        res = client.post("/api/training/teaching/units", json={
            "certificate_id": cert, "name": "坏规则", "stage": "data",
            "rule_codes": ["R-999"]})
        check("未知规则拒绝 422", res.status_code == 422, res.text[:120])
        res = client.post("/api/training/teaching/units", json={
            "certificate_id": cert, "name": "跨证书知识点", "stage": "data",
            "knowledge_point_ids": ["kp-other"], "rule_codes": ["R-002"]})
        check("跨证书知识点拒绝 422", res.status_code == 422, res.text[:120])

        # 发布→退草稿拒绝→归档→改拒绝
        res = client.put(f"/api/training/teaching/units/{unit['id']}",
                         json={"status": "published"})
        check("发布单元", res.status_code == 200 and res.json()["unit"]["status"] == "published")
        res = client.put(f"/api/training/teaching/units/{unit['id']}",
                         json={"status": "draft"})
        check("已发布不能退回草稿 422", res.status_code == 422)
        client.put(f"/api/training/teaching/units/{unit['id']}", json={"status": "archived"})
        res = client.put(f"/api/training/teaching/units/{unit['id']}",
                         json={"standard": "改"})
        check("已归档不可改 422", res.status_code == 422)

        # 案例
        res = client.post("/api/training/teaching/cases", json={
            "certificate_id": cert, "name": "样板·账申勾稽", "scenario": "仿真",
            "rule_codes": ["R-002"],
            "materials": [{"name": "科目余额表", "note": "仿真"}],
            "standard_conclusion": [
                {"rule_code": "R-002", "expected": "hit", "note": "差异需调节表"}]})
        check("创建案例", res.status_code == 200, res.text[:200])
        case = res.json()["case"]
        check("案例草稿+材料1项", case["status"] == "draft"
              and len(case["materials"]) == 1)

        res = client.post("/api/training/teaching/cases", json={
            "certificate_id": cert, "name": "坏结论", "rule_codes": ["R-002"],
            "standard_conclusion": [{"rule_code": "R-011", "expected": "pass"}]})
        check("结论引用未绑定规则拒绝 422", res.status_code == 422)
        res = client.post("/api/training/teaching/cases", json={
            "certificate_id": cert, "name": "坏三态", "rule_codes": ["R-002"],
            "standard_conclusion": [{"rule_code": "R-002", "expected": "maybe"}]})
        check("非法三态拒绝 422", res.status_code == 422)

        # 清单
        res = client.get(f"/api/training/teaching/units?certificate_id={cert}")
        check("单元清单 1 条", res.status_code == 200
              and len(res.json()["units"]) == 1)
        res = client.get(f"/api/training/teaching/cases?certificate_id={cert}")
        check("案例清单 1 条", res.status_code == 200
              and len(res.json()["cases"]) == 1)

        # 依据链
        res = client.get("/api/training/teaching/rule-evidence/R-002")
        ok = res.status_code == 200
        if ok:
            ev = res.json()["evidence"]
            ok = (ev["snapshot_id"].startswith("R-002@")
                  and len(ev["metrics"]) > 0
                  and ev["clauses"] == []
                  and ev["legal_status"] == "pending_review"
                  and "待专业复核" in ev["legal_status_label"]
                  and ev["legal_basis"])
        check("依据链：快照+指标+待专业复核+法条要点", ok, res.text[:200])
        res = client.get("/api/training/teaching/rule-evidence/R-999")
        check("未知规则 404", res.status_code == 404)

        # 未挂靠规则
        res = client.get("/api/training/teaching/unlinked-rules")
        codes = [r["rule_code"] for r in res.json().get("rules", [])]
        check("未挂靠规则含 R-024 且不含 R-002",
              "R-024" in codes and "R-002" not in codes, ",".join(codes[:6]))

        # 汇总
        with store.connect() as db:
            units_n = db.execute("SELECT count(*) c FROM teaching_unit").fetchone()["c"]
            cases_n = db.execute("SELECT count(*) c FROM teaching_case").fetchone()["c"]
        check("临时库落库 units=1 cases=1", units_n == 1 and cases_n == 1,
              f"units={units_n} cases={cases_n}")

    # 退出 lifespan 后 Store 仍可能持有 keepalive 句柄；显式关闭再删临时目录，
    # 否则 Windows 上 TemporaryDirectory 清理会报 WinError 32（清理失败不影响结论，
    # 故用 ignore_cleanup_errors 兜底，避免在 __exit__ 阶段抛异常盖掉测试结果）。
    try:
        module.store.close()
    except Exception:
        pass
    tmp.cleanup()
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n共 {len(CHECKS)} 项：{len(CHECKS) - len(failed)} 过 / {len(failed)} 败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
