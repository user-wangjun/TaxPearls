"""真题授权 + 题库 全链路冒烟（真实 app、全新库）。

覆盖：教师登记授权 → 题库逐行导入（含坏行）→ 提交审核 → 审核上线 →
发布真题内容版本（文号留痕）→ 学生按真题作答判分 → 解析判分后开放 →
下架后不再抽到但历史作答可回看 → 教师侧列表/撤销。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="tp-e2e-")
DB = Path(TMP) / "taxpearls.db"
os.environ["TAXPEARLS_DB"] = str(DB)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argon2 import PasswordHasher  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import settings  # noqa: E402,F401
from webapp.app import app  # noqa: E402

ph = PasswordHasher()
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


with TestClient(app) as client:
    # ---- 播种：管理员 / 学生 / 证书 / 知识点 ---------------------------------
    from webapp.app import store as _s  # lifespan 已建 store

    staff_id, student_id, cert, kp = (uuid.uuid4().hex for _ in range(4))
    now = "2026-10-20T10:00:00"
    with _s.connect() as db:
        db.execute("INSERT INTO college_user VALUES (?,?,?,?,?,?,?,?,'admin',1,?)",
                   (staff_id, "staff01", ph.hash("Teacher#2026x"), "演示教师",
                    "演示财经职业学院", None, None, None, now))
        db.execute("INSERT INTO student_info VALUES (?,?,?,?,?,?,?,1,?)",
                   (student_id, "演示财经职业学院", "2026001", "林小测", "会计2601",
                    ph.hash("Tp#20260012026"), None, now))
        db.execute("INSERT INTO certificate VALUES (?,?,?,?,?,?,1,?,?)",
                   (cert, "cjkj", "初级会计职称", "", '["初级会计实务"]', "", now, now))
        db.execute(
            "INSERT INTO knowledge_point (id,certificate_id,code,name,subject,active,"
            "outline_version,created_at,updated_at) VALUES (?,?,?,?,?,1,'2026',?,?)",
            (kp, cert, "KP-01-01", "库存现金", "初级会计实务", now, now))

    print("\n[1] 教师登录")
    r = client.post("/api/training/staff/auth/login",
                   json={"username": "staff01", "password": "Teacher#2026x"})
    check("staff01 登录 200", r.status_code == 200, r.text[:120])

    print("\n[2] 无授权时拒绝导入真题 / 发布真题版本")
    q_real = {"source_type": "real", "year": 2026, "question_no": "一、单选第1题",
              "stem": "下列各项中，属于流动资产的是（　）。",
              "options": [{"key": "A", "text": "库存现金"}, {"key": "B", "text": "应付账款"}],
              "answer_keys": ["A"], "explanation": "库存现金属于流动资产。",
              "knowledge_point_id": kp}
    r = client.post(f"/api/training/staff/certificates/{cert}/bank-questions",
                    json={"questions": [q_real]})
    payload = r.json()
    check("导入返回 200 且 0 成功", r.status_code == 200 and payload.get("imported") == 0, r.text[:200])
    check("失败原因提书面授权", "书面授权" in (payload.get("results") or [{}])[0].get("error", ""), str(payload)[:200])
    r = client.post(f"/api/training/staff/certificates/{cert}/content-versions",
                    json={"label": "2026 真题卷 A", "source_type": "real", "source_ref": "2026 年真题"})
    check("无授权发布真题版本 422", r.status_code == 422, r.text[:150])

    print("\n[3] 登记授权（含未来生效的一条）")
    r = client.post(f"/api/training/staff/certificates/{cert}/source-authorizations",
                    json={"licensor": "全国会计考办授权出版方", "auth_no": "AUTH-2026-001",
                          "question_types": ["real", "recall", "mock"],
                          "valid_from": "2026-01-01", "valid_until": "2027-12-31",
                          "scope_note": "仅限校内教学使用"})
    check("登记授权 200", r.status_code == 200, r.text[:200])
    auth = (r.json() or {}).get("authorization") or {}
    check("状态 active", auth.get("status") == "active")
    r = client.post(f"/api/training/staff/certificates/{cert}/source-authorizations",
                    json={"licensor": "授权方", "auth_no": "AUTH-2028", "question_types": ["recall"],
                          "valid_from": "2028-01-01", "valid_until": None})
    check("未来生效授权可登记（长期）", r.status_code == 200, r.text[:150])
    r = client.post(f"/api/training/staff/certificates/{cert}/source-authorizations",
                    json={"licensor": "授权方", "auth_no": "AUTH-2026-001", "question_types": ["real"],
                          "valid_from": "2026-01-01"})
    check("重复文号 409", r.status_code == 409, r.text[:120])

    print("\n[4] 授权列表（历史 NameError 崩溃点）")
    r = client.get(f"/api/training/staff/certificates/{cert}/source-authorizations")
    check("列表 200", r.status_code == 200, r.text[:200])
    rows = {a["auth_no"]: a for a in (r.json() or {}).get("authorizations", [])}
    check("当日生效覆盖=True", rows.get("AUTH-2026-001", {}).get("covering_today") is True)
    check("未来授权 coverage=False", rows.get("AUTH-2028", {}).get("covering_today") is False)

    print("\n[5] 题库逐行导入（含坏行，不中断整批）")
    bad_no_opt = dict(q_real, answer_keys=["E"])
    not_dict = "not-a-dict"
    r = client.post(f"/api/training/staff/certificates/{cert}/bank-questions",
                    json={"questions": [q_real, bad_no_opt, not_dict,
                                        dict(q_real, stem="多选题：下列属于流动资产的有（　）。",
                                             answer_keys=["A", "B"])]})
    payload = r.json()
    check("整批 200（非 422）", r.status_code == 200, r.text[:200])
    check("成功 2 / 失败 2", payload.get("imported") == 2 and payload.get("failed") == 2, str(payload)[:200])
    check("逐行 ok 标记", [x["ok"] for x in payload["results"]] == [True, False, False, True], str(payload)[:200])
    qid = payload["results"][0]["id"]

    r = client.get(f"/api/training/staff/certificates/{cert}/bank-questions")
    listing = (r.json() or {}).get("questions", [])
    check("教师列表可见 2 题且带答案", len(listing) == 2 and all("answer_keys" in q for q in listing))
    check("自动写入授权文号", all("AUTH-2026-001" in q["source_ref"] for q in listing))
    check("导入即为草稿", all(q["status"] == "draft" for q in listing))

    print("\n[6] 审核流转")
    r = client.post(f"/api/training/staff/bank-questions/{qid}/review", json={"decision": "approve"})
    check("草稿直接审核 409", r.status_code == 409, r.text[:120])
    check("提交审核 200", client.post(f"/api/training/staff/bank-questions/{qid}/submit").status_code == 200)
    r = client.post(f"/api/training/staff/bank-questions/{qid}/review",
                    json={"decision": "approve", "note": "核对 2026 真题卷"})
    check("审核上线 200", r.status_code == 200 and r.json()["question"]["status"] == "approved", r.text[:150])

    print("\n[7] 有授权后可发布真题内容版本（文号留痕）")
    r = client.post(f"/api/training/staff/certificates/{cert}/content-versions",
                    json={"label": "2026 真题卷 A", "source_type": "real",
                          "source_ref": "2026 年真题"})
    check("发布真题版本 200", r.status_code == 200, r.text[:200])
    ver = (r.json() or {}).get("content_version") or {}
    check("source_ref 含授权文号", "AUTH-2026-001" in str(ver.get("source_ref", "")), str(ver)[:200])

    print("\n[8] 学生按真题作答")
    client.post("/api/training/staff/auth/logout")
    r = client.post("/api/training/auth/login",
                    json={"student_no": "2026001", "password": "Tp#20260012026",
                          "college": "演示财经职业学院"})
    check("学生登录 200", r.status_code == 200, r.text[:150])
    r = client.post("/api/training/my/practice/start",
                    json={"certificate_id": cert, "knowledge_point_id": kp, "source_type": "real"})
    check("抽到真题 200", r.status_code == 200, r.text[:200])
    attempt = (r.json() or {}).get("attempt") or {}
    check("标记为题库题", attempt.get("bank") is True and attempt.get("bank_question_id") == qid)
    check("作答中不下发答案", "answer_keys" not in attempt)
    check("作答中不下发解析", "explanation" not in attempt)

    r = client.post(f"/api/training/my/practice/{attempt['id']}/submit", json={"selected_keys": ["B"]})
    result = (r.json() or {}).get("attempt", {}).get("result") or {}
    check("判分 200 且 0 分", r.status_code == 200 and result.get("score") == 0, r.text[:200])
    check("解析判分后开放", "库存现金" in str(result.get("explanation", "")))
    check("重复提交 409",
          client.post(f"/api/training/my/practice/{attempt['id']}/submit",
                      json={"selected_keys": ["A"]}).status_code == 409)

    print("\n[9] 下架后：抽不到，但历史作答可回看")
    client.post("/api/training/auth/logout")
    client.post("/api/training/staff/auth/login",
                json={"username": "staff01", "password": "Teacher#2026x"})
    r = client.post(f"/api/training/staff/bank-questions/{qid}/offline")
    check("下架 200", r.status_code == 200, r.text[:150])
    client.post("/api/training/staff/auth/logout")
    client.post("/api/training/auth/login",
                json={"student_no": "2026001", "password": "Tp#20260012026",
                      "college": "演示财经职业学院"})
    r = client.post("/api/training/my/practice/start",
                    json={"certificate_id": cert, "knowledge_point_id": kp, "source_type": "real"})
    check("下架后抽不到 404", r.status_code == 404, r.text[:150])
    r = client.get(f"/api/training/my/practice/{attempt['id']}")
    detail = (r.json() or {}).get("attempt") or {}
    check("历史作答仍可读题干", r.status_code == 200 and "流动资产" in str(detail.get("stem", "")), r.text[:200])
    check("历史作答解析仍在", "库存现金" in str((detail.get("result") or {}).get("explanation", "")))

    print("\n[10] 撤销授权后不得再导入 / 再提交审核")
    client.post("/api/training/auth/logout")
    client.post("/api/training/staff/auth/login",
                json={"username": "staff01", "password": "Teacher#2026x"})
    r = client.post(f"/api/training/staff/source-authorizations/{auth['id']}/revoke")
    check("撤销 200 且状态 revoked",
          r.status_code == 200 and r.json()["authorization"]["status"] == "revoked", r.text[:150])
    r = client.post(f"/api/training/staff/certificates/{cert}/bank-questions",
                    json={"questions": [q_real]})
    check("撤销后导入 0 成功", r.json().get("imported") == 0)
    r = client.post(f"/api/training/staff/bank-questions/{qid}/submit")
    check("撤销后提交审核被拒（422 且提授权）",
          r.status_code == 422 and "书面授权" in r.text, f"{r.status_code} {r.text[:120]}")

print("\n" + "=" * 60)
if FAILED:
    print(f"FAILED {len(FAILED)} 项：")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过：真题授权 + 题库 全链路")
