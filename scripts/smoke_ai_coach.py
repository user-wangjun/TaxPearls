"""AI 备课助手全链路冒烟：真实 app + 临时库 + 真实模型生成一次。

覆盖：教师生成草稿（真实 AI）→ 审核编辑 → 发布 → 学生列表/详情（未提交不下发答案）
→ 作答判分 → 重复提交拦截 → 教师统计。
运行：python -B -X utf8 scripts/smoke_ai_coach.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from webapp import app as module  # noqa: E402
from webapp.storage import Store  # noqa: E402

BRIEF = (
    "本节课讲增值税一般计税方法。重点一：销项税额 = 不含税销售额 × 适用税率，"
    "价外费用要并入销售额；重点二：进项税额抵扣必须取得合法扣税凭证，"
    "用于集体福利、个人消费的购进货物不得抵扣；重点三：当期应纳税额 = 当期销项税额 - 当期进项税额。"
    "学生常把不得抵扣的进项税记成可以抵扣，也常忘记价外费用要并入销售额。"
)
PASSWORD = "Smoke-coach-8241!"


def check(label, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + label + (f" · {detail}" if detail else ""))
    if not condition:
        raise SystemExit(f"冒烟失败：{label}")


def main() -> None:
    os.environ.setdefault("TAXPEARLS_NOTIFICATION_EMAIL_ENABLED", "0")
    with TemporaryDirectory() as tmp:
        old = module.store
        store = Store(Path(tmp) / "smoke.db", keepalive=True)
        module.store = store
        client = TestClient(module.app)
        client.__enter__()
        try:
            teacher = store.create_user("smoke-teacher", PASSWORD, "演示教师", "teacher", "demo-college")
            student = store.create_user("smoke-student", PASSWORD, "演示学生", "student", "demo-college")
            client.post("/api/login", json={"username": teacher["username"], "password": PASSWORD})
            class_id = client.post("/api/classes", json={
                "name": "会计一班", "student_ids": [student["id"]]}).json()["id"]
            print("[1] 教师登录并建班：", class_id)

            draft = client.post("/api/teaching/ai-tasks/draft",
                                json={"brief": BRIEF, "question_count": 4,
                                      "kinds": ["single", "multiple"]})
            if draft.status_code != 200:
                raise SystemExit(f"AI 生成失败（{draft.status_code}）：{draft.json().get('detail')}")
            data = draft.json()
            print(f"[2] AI 生成：{len(data['knowledge_points'])} 条知识点 / {len(data['questions'])} 道题"
                  f" · 模型 {data.get('model')}")
            for kp in data["knowledge_points"]:
                print(f"      · {kp['name']}（{kp['difficulty']}）：{kp['summary'][:40]}")
            for index, question in enumerate(data["questions"], 1):
                print(f"      Q{index} [{question['kind']}] {question['stem'][:46]} 答案 {''.join(question['answers'])}")
            check("题型都在允许范围", all(q["kind"] in {"single", "multiple"} for q in data["questions"]))
            check("每题答案都落在选项内",
                  all(set(q["answers"]) <= {o["key"] for o in q["options"]} for q in data["questions"]))

            # 教师审核：改标题后保存并发布
            payload = {"title": data["title"] or "增值税一般计税专项练习", "brief": BRIEF, "class_id": class_id,
                       "knowledge_points": data["knowledge_points"], "questions": data["questions"],
                       "publish": True}
            saved = client.post("/api/teaching/ai-tasks", json=payload)
            check("保存并发布成功", saved.status_code == 200, saved.text[:120])
            task = saved.json()
            check("状态为已发布", task["status"] == "published")

            # 学生视角
            client.cookies.clear()
            client.post("/api/login", json={"username": student["username"], "password": PASSWORD})
            items = client.get("/api/teaching/ai-tasks").json()["items"]
            check("学生能看到已发布任务", [item["id"] for item in items] == [task["id"]])
            detail = client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
            check("未提交时不下发答案与解析",
                  all("answers" not in q and "explanation" not in q for q in detail["questions"]))
            answers = {q["id"]: q["answers"] if "answers" in q else [] for q in detail["questions"]}

            # 故意只答对一半：第 1 题照抄正确选项（用后端数据构造），其余留空
            with store.connect() as db:
                rows = db.execute("SELECT id, answer_keys_json FROM teaching_ai_task_question"
                                  " WHERE task_id=? ORDER BY position", (task["id"],)).fetchall()
            answers = {row["id"]: json.loads(row["answer_keys_json"]) for row in rows[:1]}
            submitted = client.post(f"/api/teaching/ai-tasks/{task['id']}/submit", json={"answers": answers})
            check("学生提交成功", submitted.status_code == 200, submitted.text[:120])
            score = submitted.json()["score"]
            print(f"[3] 学生只答对 1/{len(rows)} 题，得分 {score}")
            check("判分按题均分计算", 0 < score < 100, f"{score}")
            again = client.post(f"/api/teaching/ai-tasks/{task['id']}/submit", json={"answers": answers})
            check("重复提交被拦截", again.status_code == 409, again.text[:60])
            after = client.get(f"/api/teaching/ai-tasks/{task['id']}").json()
            check("提交后开放答案与解析", "answers" in after["questions"][0] and "explanation" in after["questions"][0])

            # 教师统计
            client.cookies.clear()
            client.post("/api/login", json={"username": teacher["username"], "password": PASSWORD})
            stats = client.get("/api/teaching/ai-tasks").json()["items"][0]
            print(f"[4] 教师统计：已提交 {stats['submitted']}/{stats['expected']} · 平均 {stats['mean_score']}")
            check("统计已计入提交", stats["submitted"] == 1 and stats["expected"] == 1)
            print("\n全部通过：生成 → 审核 → 发布 → 作答 → 判分 → 统计 闭环可用。")
        finally:
            client.__exit__(None, None, None)
            module.store = old
            store.close()


if __name__ == "__main__":
    main()
