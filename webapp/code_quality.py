"""代码质量维度评分（FR-J07 v1）：AST 静态检查，辅助分不计入判题通过判定。

检查项与固定扣分（确定性、无外部依赖、如实标注边界）：
- syntax_error：代码无法解析 → 直接 0 分（其余项不再评）。
- long_line：单行超 100 字符，每行扣 2（最多扣 20）。
- func_naming：函数名不符合 snake_case，每处扣 3（最多扣 12）。
- func_length：单函数超 40 行（按 AST 行号跨度），每个扣 5（最多扣 15）。
- complexity：单函数分支点（if/for/while/and/or/except/with 判断支数）
  超 10，每个函数扣 5（最多扣 15）。
- bare_except：`except:` 裸捕获，每处扣 3（最多扣 9）。

分数下限 0，上限 100；只有 python 提交参与评估。质量分是辅助维度：
教师调分（J08）时可参考，但引擎不因质量分改判对错。
"""
from __future__ import annotations

import ast
import json
import uuid
from typing import Any

from webapp.storage import _now

MAX_LINE = 100
MAX_FUNC_LINES = 40
MAX_BRANCHES = 10


def _deduct(checks: list[dict[str, Any]], name: str, each: int, cap: int,
            items: list[Any], message: str) -> None:
    if not items:
        return
    penalty = min(cap, each * len(items))
    checks.append({"name": name, "penalty": penalty,
                   "count": len(items),
                   "detail": "; ".join(items[:5]) + ("…" if len(items) > 5 else ""),
                   "message": message})


def _branches(node: ast.AST) -> int:
    count = 0
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.With, ast.ExceptHandler)):
            count += 1
        elif isinstance(child, ast.BoolOp):
            count += len(child.values) - 1
    return count


def assess(code: str) -> dict[str, Any]:
    """对一份 python 代码做静态质量评估，返回 {score, checks}。"""
    checks: list[dict[str, Any]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"score": 0.0, "checks": [
            {"name": "syntax_error", "penalty": 100, "count": 1,
             "detail": f"第 {exc.lineno} 行: {exc.msg}", "message": "代码无法解析，不评质量。"}]}

    lines = code.replace("\r\n", "\n").split("\n")
    long_lines = [str(i + 1) for i, line in enumerate(lines) if len(line) > MAX_LINE]
    _deduct(checks, "long_line", 2, 20, long_lines, f"单行超 {MAX_LINE} 字符，每行扣 2。")

    bad_names: list[str] = []
    long_funcs: list[str] = []
    complex_funcs: list[str] = []
    bare_excepts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name != node.name.lower() or node.name[0].isdigit():
                bad_names.append(node.name)
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            if span > MAX_FUNC_LINES:
                long_funcs.append(f"{node.name}({span} 行)")
            branches = _branches(node)
            if branches > MAX_BRANCHES:
                complex_funcs.append(f"{node.name}(分支 {branches})")
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            bare_excepts.append(f"第 {node.lineno} 行")
    _deduct(checks, "func_naming", 3, 12, bad_names, "函数名应为 snake_case，每处扣 3。")
    _deduct(checks, "func_length", 5, 15, long_funcs, f"单函数超 {MAX_FUNC_LINES} 行，每个扣 5。")
    _deduct(checks, "complexity", 5, 15, complex_funcs, f"单函数分支点超 {MAX_BRANCHES}，每个扣 5。")
    _deduct(checks, "bare_except", 3, 9, bare_excepts, "裸 except: 掩盖异常，每处扣 3。")

    score = 100.0 - sum(c["penalty"] for c in checks)
    return {"score": round(max(0.0, score), 2), "checks": checks}


def record_quality(store, submission_id: str, code: str, *,
                   language: str = "python") -> dict[str, Any] | None:
    """评估并落库一份提交的质量分（幂等：同提交覆盖更新）。非 python 返回 None。"""
    if language != "python":
        return None
    result = assess(code)
    now = _now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM code_quality_result WHERE submission_id=?", (submission_id,))
        db.execute(
            """INSERT INTO code_quality_result (id, submission_id, score, detail_json, checked_at)
               VALUES (?,?,?,?,?)""",
            (str(uuid.uuid4()), submission_id, result["score"],
             json.dumps(result["checks"], ensure_ascii=False), now))
        row = db.execute(
            "SELECT score, detail_json, checked_at FROM code_quality_result WHERE submission_id=?",
            (submission_id,)).fetchone()
    return {"score": row["score"], "checks": json.loads(row["detail_json"]),
            "checked_at": row["checked_at"]}


def get_quality(store, submission_id: str) -> dict[str, Any] | None:
    with store.connect() as db:
        row = db.execute(
            "SELECT score, detail_json, checked_at FROM code_quality_result WHERE submission_id=?",
            (submission_id,)).fetchone()
    if not row:
        return None
    return {"score": row["score"], "checks": json.loads(row["detail_json"]),
            "checked_at": row["checked_at"]}
