"""在走查副本库里把演示账号密码改成已知值（仅作用于 walk.db，不碰真库）。

真库的口令未记录；与其猜，不如直接在副本上重设，改完即可用同一套口令跑九个视图。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # scripts/ 的上一级 = 仓库根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import settings as _settings  # noqa: F401  先把 .env 载入，Store 才有密钥

from argon2 import PasswordHasher

from webapp.storage import Store  # type: ignore
from webapp import university_members  # type: ignore

_passwords = PasswordHasher()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--staff-password", default="Walk#2026check")
    parser.add_argument("--student-password", default="Walk#2026stu")
    args = parser.parse_args()

    store = Store(args.db, keepalive=True)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT id, email FROM college_user WHERE username='staff01'").fetchone()
        if not row:
            print("FAIL: 副本库里找不到 staff01")
            return 1
        staff_hash = _passwords.hash(args.staff_password)
        db.execute("UPDATE college_user SET password_hash=? WHERE id=?",
                   (staff_hash, row["id"]))
        university_members.sync_password(db, staff_hash, staff_id=row["id"])
        db.execute("DELETE FROM training_staff_sessions WHERE staff_id=?", (row["id"],))

        stu = db.execute(
            "SELECT id, college FROM student_info WHERE student_no='2026001'").fetchone()
        student_info = ""
        if stu:
            student_hash = _passwords.hash(args.student_password)
            db.execute("UPDATE student_info SET password_hash=? WHERE id=?",
                       (student_hash, stu["id"]))
            university_members.sync_password(db, student_hash, student_id=stu["id"])
            db.execute("DELETE FROM training_student_sessions WHERE student_id=?",
                       (stu["id"],))
            student_info = f"学号 2026001（院校 {stu['college']}）"

    counts = {}
    with store.connect() as db:
        for table, label in (("certificate", "证书"), ("knowledge_point", "知识点"),
                             ("teaching_unit", "教学单元"), ("teaching_case", "实操案例"),
                             ("assignments", "作业"), ("training_papers", "试卷"),
                             ("student_info", "学生")):
            try:
                counts[label] = db.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                counts[label] = "（表不存在）"
    store.close()

    print("副本库口令已重设（仅 walk.db）")
    print("  staff01        :", args.staff_password)
    if student_info:
        print(" ", student_info, ":", args.student_password)
    print("  数据规模      :", "，".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
