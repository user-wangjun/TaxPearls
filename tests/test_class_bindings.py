"""教学班 → 班级实体回填（FR-U01 尾巴）的边界：同名唯一才认领，判过不再翻案。

覆盖：
1. 同院校内同名实体恰好一个 → 认领，`binding_source='auto'`；
2. 同名实体两个 → 留 NULL 并记 `unbound`（宁可未归属，也不猜）；
3. 无同名实体 → 留 NULL 并记 `unbound`；
4. 管理员解绑过的行（`unbound`）不会被回填重新挂回去；
5. 管理员绑定过的行（`manual`）不会被回填改到别的实体；
6. dry_run 只报数不落库；重复执行幂等（第二次 bound=0）。
"""

import tempfile
import unittest
from pathlib import Path

from webapp.storage import Store
from webapp.training_reports import backfill_bindings, bind_teaching_class

COLLEGE = "示例财经大学"
NOW = "2026-10-26T00:00:00+00:00"


class ClassBindingBackfillTests(unittest.TestCase):
    password = "SecureForest!2026"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "bindings.db", keepalive=True)
        self.addCleanup(self.store.close)
        self.root = self.store.create_initial_admin("rootadmin", self.password, "平台", "root")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # 院校管理员身份：college_user + 平台用户 + 映射（回填靠这条链取 college）
            db.execute("""INSERT INTO users(id,username,password_hash,display_name,role,org_id,active,email,created_at)
                          VALUES ('u1','staff01','x','演示教师','teacher','org-t','1','t@example.com',?)""", (NOW,))
            db.execute("""INSERT INTO college_user(id,username,password_hash,display_name,college,email,role,active,created_at)
                          VALUES ('s1','staff01','x','演示教师',?,'t@example.com','admin',1,?)""", (COLLEGE, NOW))
            db.execute("INSERT INTO university_identity_links VALUES ('u1','s1',NULL)")
            db.execute("INSERT INTO college_department(id,college,name,archived,created_at) VALUES ('d1',?,?,0,?)",
                       (COLLEGE, "会计学院", NOW))

    # ---------------------------------------------------------------- 造数
    def entity(self, class_id, name, college=COLLEGE):
        with self.store.connect() as db:
            db.execute("""INSERT INTO college_class(id,college,name,department_id,term_id,archived,created_at,major_id)
                          VALUES (?,?,?,'d1',NULL,0,?,NULL)""", (class_id, college, name, NOW))

    def teaching(self, class_id, name, source="__null__"):
        with self.store.connect() as db:
            db.execute("""INSERT INTO training_classes(id,org_id,owner_id,name,revision,created_at,college_class_id,binding_source)
                          VALUES (?,'org-t','u1',?,1,?,NULL,?)""",
                       (class_id, name, NOW, None if source == "__null__" else source))

    def row(self, class_id):
        with self.store.connect() as db:
            return dict(db.execute("SELECT college_class_id,binding_source FROM training_classes WHERE id=?",
                                   (class_id,)).fetchone())

    def run_backfill(self, **kwargs):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = backfill_bindings(db, **kwargs)
            db.execute("COMMIT")
        return result

    # ---------------------------------------------------------------- 用例
    def test_unique_same_name_is_bound_once(self):
        self.entity("c1", "会计2601")
        self.teaching("t1", "会计2601")
        self.teaching("t2", "会计2602")
        result = self.run_backfill()
        self.assertEqual(result["bound"], 1)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(self.row("t1"), {"college_class_id": "c1", "binding_source": "auto"})
        self.assertEqual(self.row("t2"), {"college_class_id": None, "binding_source": "unbound"})
        again = self.run_backfill()
        self.assertEqual(again["bound"], 0, "判定即定案，重复执行不再改数")

    def test_duplicate_same_name_stays_unbound(self):
        self.entity("c1", "会计2601")
        self.entity("c2", "会计2601")
        self.teaching("t1", "会计2601")
        result = self.run_backfill()
        self.assertEqual((result["bound"], result["ambiguous"]), (0, 1))
        self.assertEqual(self.row("t1"), {"college_class_id": None, "binding_source": "unbound"})

    def test_naive_name_variant_is_not_bound(self):
        """「会计2601 演示班」≠「会计2601」：正是不能模糊匹配的那类历史数据。"""
        self.entity("c1", "会计2601")
        self.teaching("t1", "会计2601 演示班")
        self.assertEqual(self.run_backfill()["bound"], 0)
        self.assertEqual(self.row("t1")["college_class_id"], None)

    def test_manual_unbind_is_never_rebound(self):
        self.entity("c1", "会计2601")
        self.teaching("t1", "会计2601", source="unbound")
        self.assertEqual(self.run_backfill()["bound"], 0)
        self.assertEqual(self.row("t1")["binding_source"], "unbound")

    def test_manual_binding_is_never_overwritten(self):
        self.entity("c1", "会计2601")
        self.entity("c9", "另一个班")
        self.teaching("t1", "会计2601", source="manual")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE training_classes SET college_class_id='c9' WHERE id='t1'")
            db.execute("COMMIT")
        self.assertEqual(self.run_backfill()["bound"], 0)
        self.assertEqual(self.row("t1"), {"college_class_id": "c9", "binding_source": "manual"})

    def test_dry_run_reports_without_writing(self):
        self.entity("c1", "会计2601")
        self.teaching("t1", "会计2601")
        result = self.run_backfill(dry_run=True)
        self.assertEqual(result["bound"], 1)
        self.assertEqual(result["bindings"][0]["college_class_id"], "c1")
        self.assertEqual(self.row("t1"), {"college_class_id": None, "binding_source": None},
                         "预演不能落库")

    def test_other_college_is_out_of_scope(self):
        self.entity("cx", "会计2601", college="别的大学")
        self.teaching("t1", "会计2601")
        self.assertEqual(self.run_backfill()["bound"], 0, "只认同院校内的实体")
        self.assertEqual(self.run_backfill(college="别的大学")["bound"], 0,
                         "按院校过滤时别的院校的教学班不该被处理")

    def test_bind_teaching_class_marks_source(self):
        self.entity("c1", "会计2601")
        self.teaching("t1", "会计2601", source="unbound")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            bind_teaching_class(db, COLLEGE, "t1", "c1")
            db.execute("COMMIT")
        self.assertEqual(self.row("t1"), {"college_class_id": "c1", "binding_source": "manual"})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            bind_teaching_class(db, COLLEGE, "t1", None)
            db.execute("COMMIT")
        self.assertEqual(self.row("t1"), {"college_class_id": None, "binding_source": "unbound"})
        self.assertEqual(self.run_backfill()["bound"], 0, "人工解绑后不能被回填翻案")


if __name__ == "__main__":
    unittest.main()
