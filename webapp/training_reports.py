"""高校管理报表（FR-U05）：院系 / 专业 / 班级 / 课程维度的教学成效聚合。

三条纪律：
1. **不另起一套算法。** 每个班级的数字直接来自 `classroom.gradebook`（与「班级学情统计」
   和「成绩单导出」同一个函数），这里只做按层级求和，所以跨层级数字可以互相对账。
2. **不猜归属。** 教学班（`training_classes`）与组织架构班级实体（`college_class`）之间
   靠 `training_classes.college_class_id` 显式绑定。历史上实体叫「会计2601」、教学班叫
   「会计2601 演示班」，按名字模糊匹配会把成绩算到错误院系，因此未绑定的教学班一律进
   「未归属」，并给出候选建议供管理员人工确认。
3. **不补造。** 没有任务的班级照样列出来，数字为 0 并写明"暂无已发布任务"，不因为 0 就隐藏。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from webapp import classroom

EMPTY = {"classes": 0, "students": 0, "tasks": 0, "expected": 0, "submitted": 0,
         "scored": 0, "score_sum": 0.0, "unreviewed": 0}


def _ratio(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return float((Decimal(numerator) * 100 / Decimal(denominator)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP))


def _mean(score_sum: float, scored: int) -> float | None:
    if not scored:
        return None
    return float((Decimal(str(score_sum)) / Decimal(scored)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))


def _finish(totals: dict[str, Any]) -> dict[str, Any]:
    totals = dict(totals)
    totals["submit_rate"] = _ratio(totals["submitted"], totals["expected"])
    totals["mean"] = _mean(totals["score_sum"], totals["scored"])
    return totals


def _merge(target: dict[str, Any], source: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    for stats in (source if isinstance(source, list) else [source]):
        for key in EMPTY:
            target[key] = target.get(key, 0) + stats[key]
    return target


def class_stats(db: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """单个教学班的成效数字，口径完全来自 gradebook。"""
    book = classroom.gradebook(db, dict(row))
    cells = book["detail"]
    stats = dict(EMPTY)
    stats["classes"] = 1
    stats["students"] = len(book["students"])
    stats["tasks"] = len(book["tasks"])
    stats["expected"] = sum(person["expected"] for person in book["students"])
    stats["submitted"] = sum(person["submitted"] for person in book["students"])
    scored = [cell["final"] for cell in cells if cell["final"] is not None]
    stats["scored"] = len(scored)
    stats["score_sum"] = float(sum(scored))
    stats["unreviewed"] = sum(1 for cell in cells
                              if cell["state"] in ("已交", "材料异常") and cell["adjusted"] is None)
    stats["class_name"] = row["name"]
    stats["teaching_class_id"] = row["id"]
    stats["college_class_id"] = row["college_class_id"]
    stats["empty_reason"] = None if book["tasks"] else "暂无已发布任务"
    return stats


def college_teaching_classes(db: sqlite3.Connection, college: str) -> list[sqlite3.Row]:
    """本院校的教学班：教学班的 owner 经身份关联落到 college_user 才认（不靠名字）。"""
    return db.execute(
        """SELECT tc.*, u.display_name AS owner_name
             FROM training_classes tc
             JOIN university_identity_links l ON l.user_id = tc.owner_id
             JOIN college_user u ON u.id = l.staff_id AND u.college = ?
            ORDER BY tc.name, tc.id""", (college,)).fetchall()


def college_classes(db: sqlite3.Connection, college: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT c.*, d.name AS department_name, m.name AS major_name, t.name AS term_name
             FROM college_class c
             LEFT JOIN college_department d ON d.id = c.department_id
             LEFT JOIN college_major m ON m.id = c.major_id
             LEFT JOIN college_term t ON t.id = c.term_id
            WHERE c.college = ? AND c.archived = 0
            ORDER BY d.name, m.name, c.name""", (college,)).fetchall()


def course_class_names(db: sqlite3.Connection, course_id: str) -> list[str]:
    return [r["class_name"] for r in db.execute(
        "SELECT class_name FROM course_class WHERE course_id=?", (course_id,)).fetchall()]


def build_report(db: sqlite3.Connection, college: str, *, term_id: str | None = None,
                 department_id: str | None = None) -> dict[str, Any]:
    entities = college_classes(db, college)
    if term_id:
        entities = [e for e in entities if e["term_id"] == term_id]
    if department_id:
        entities = [e for e in entities if e["department_id"] == department_id]
    entity_ids = {e["id"] for e in entities}
    teaching = college_teaching_classes(db, college)
    bound = [t for t in teaching if t["college_class_id"] in entity_ids]
    # 每个教学班只算一次（gradebook 是逐班查询，重复调用会成倍放大开销）
    cache: dict[str, dict[str, Any]] = {}

    def stats_of(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        key = row["id"]
        if key not in cache:
            cache[key] = class_stats(db, row)
        return cache[key]

    per_entity: dict[str, list[dict[str, Any]]] = {}
    for row in bound:
        per_entity.setdefault(row["college_class_id"], []).append(stats_of(row))
    departments: dict[str, dict[str, Any]] = {}
    for entity in entities:
        stats_list = per_entity.get(entity["id"], [])
        entry = {"id": entity["id"], "name": entity["name"], "term_id": entity["term_id"],
                 "term_name": entity["term_name"], "major_id": entity["major_id"],
                 "teaching_classes": [{"id": s["teaching_class_id"], "name": s["class_name"],
                                       "students": s["students"], "tasks": s["tasks"],
                                       "submitted": s["submitted"], "mean": _finish(s)["mean"],
                                       "empty_reason": s["empty_reason"]} for s in stats_list],
                 "totals": _finish(_merge_stats(stats_list))}
        dept = departments.setdefault(entity["department_id"] or "__none__", {
            "id": entity["department_id"], "name": entity["department_name"] or "未归入院系",
            "majors": {}, "totals": dict(EMPTY)})
        major = dept["majors"].setdefault(entity["major_id"] or "__none__", {
            "id": entity["major_id"], "name": entity["major_name"] or "未归入专业",
            "classes": [], "totals": dict(EMPTY)})
        major["classes"].append(entry)
        _merge(major["totals"], stats_list)
        _merge(dept["totals"], stats_list)

    unbound = [t for t in teaching if t["college_class_id"] not in entity_ids]
    unbound_stats = []
    for row in unbound:
        stats = stats_of(row)
        unbound_stats.append({
            "id": row["id"], "name": row["name"], "owner": row["owner_name"],
            "college_class_id": row["college_class_id"],
            "suggestions": _suggest(db, college, row),
            **{k: stats[k] for k in ("students", "tasks", "expected", "submitted", "unreviewed")},
            "submit_rate": _finish(stats)["submit_rate"], "mean": _finish(stats)["mean"],
            "empty_reason": stats["empty_reason"]})

    courses = []
    for course in db.execute(
            """SELECT c.*, u.display_name AS teacher_name FROM course c
                 LEFT JOIN college_user u ON u.id = c.teacher_id
                WHERE c.college=? AND c.active=1 ORDER BY c.created_at DESC""", (college,)).fetchall():
        names = set(course_class_names(db, course["id"]))
        # 课程绑定的是班级名：先落到实体，再经实体找绑定的教学班；没有实体时退回同名教学班。
        hit_ids = {e["id"] for e in entities if e["name"] in names}
        stats_list = [stats_of(t) for t in bound if t["college_class_id"] in hit_ids]
        if not stats_list:
            stats_list = [stats_of(t) for t in teaching if t["name"] in names]
        totals = _finish(_merge_stats(stats_list))
        courses.append({"id": course["id"], "name": course["name"], "semester": course["semester"],
                        "teacher": course["teacher_name"], "class_names": sorted(names),
                        "classes": totals["classes"], "tasks": totals["tasks"],
                        "expected": totals["expected"], "submitted": totals["submitted"],
                        "submit_rate": totals["submit_rate"], "mean": totals["mean"],
                        "unreviewed": totals["unreviewed"],
                        "note": None if stats_list else "未关联到已绑定的教学班，暂无可统计任务"})

    overall = dict(EMPTY)
    for dept in departments.values():
        _merge(overall, dept["totals"])
    scope = ("统计口径与「班级学情统计」「成绩单导出」同源：有效名册∩作业指定学生、默认只含已发布作业、"
             "未交不计 0 分、材料异常单列、当前分取复核分优先。院系/专业归属来自组织架构实体，"
             "教学班须先绑定到班级实体才计入；未绑定的教学班单列在「未归属」，不按名字猜。"
             "课程维度按课程绑定的班级名归集，同一学生修多门课不重复计人（课程区只算提交份数）。")
    return {"college": college, "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
            "filters": {"term_id": term_id, "department_id": department_id},
            "options": {
                "terms": [dict(r) for r in db.execute(
                    """SELECT id, name, is_current FROM college_term
                        WHERE college=? AND archived=0 ORDER BY start_date DESC, name""",
                    (college,)).fetchall()],
                "departments": [dict(r) for r in db.execute(
                    """SELECT id, name FROM college_department
                        WHERE college=? AND archived=0 ORDER BY name""", (college,)).fetchall()],
            },
            "departments": [
                {"id": d["id"], "name": d["name"],
                 "majors": [{"id": m["id"], "name": m["name"], "classes": m["classes"],
                             "totals": _finish(m["totals"])} for m in d["majors"].values()],
                 "totals": _finish(d["totals"])} for d in departments.values()],
            "unbound": unbound_stats, "courses": courses,
            "totals": _finish(overall), "scope": scope,
            "notes": [
                "教学班与班级实体未绑定时不计入院系/专业汇总，避免把成绩算到错的院系。",
                "「未复核」= 已提交但还没人工调分/写评语的份数，用于提醒老师补复核。",
                "考证内容层（知识点、标注、关联）为全平台共享数据，不在院校报表内按校切分。",
            ]}


def _merge_stats(stats_list: list[dict[str, Any]]) -> dict[str, Any]:
    totals = dict(EMPTY)
    for stats in stats_list:
        _merge(totals, stats)
    return totals


def _suggest(db: sqlite3.Connection, college: str, teaching: sqlite3.Row) -> list[dict[str, str]]:
    """候选班级实体（只作提示，不自动绑定）：先同名，再互相包含。"""
    name = teaching["name"]
    rows = college_classes(db, college)
    exact = [r for r in rows if r["name"] == name]
    loose = [r for r in rows if r["name"] != name and (r["name"] in name or name in r["name"])]
    return [{"id": r["id"], "name": r["name"], "match": "同名"} for r in exact] + \
           [{"id": r["id"], "name": r["name"], "match": "名称包含"} for r in loose][:3]


def bind_teaching_class(db: sqlite3.Connection, college: str, teaching_class_id: str,
                        college_class_id: str | None) -> dict[str, Any]:
    """把教学班绑定到班级实体（或解绑）。只做归属登记，不动任何成绩数据。"""
    teaching = db.execute(
        """SELECT tc.* FROM training_classes tc
             JOIN university_identity_links l ON l.user_id = tc.owner_id
             JOIN college_user u ON u.id = l.staff_id AND u.college = ?
            WHERE tc.id = ?""", (college, teaching_class_id)).fetchone()
    if not teaching:
        raise classroom.ClassroomError("教学班不存在或不属于本院校。", 404)
    if college_class_id is not None:
        entity = db.execute("SELECT id FROM college_class WHERE id=? AND college=? AND archived=0",
                            (college_class_id, college)).fetchone()
        if not entity:
            raise classroom.ClassroomError("班级实体不存在或已归档。", 404)
    db.execute("UPDATE training_classes SET college_class_id=? WHERE id=?",
               (college_class_id, teaching_class_id))
    return {"teaching_class_id": teaching_class_id, "name": teaching["name"],
            "college_class_id": college_class_id}
