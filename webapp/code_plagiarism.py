"""代码查重 / 抄袭检测（FR-J06 v1）：同题跨学生两两比对。

方法（诚实标注边界）：
- 归一化：tokenize 去注释与空白，标识符统一为 <ID>（纯改名规避无效），
  字符串/数字字面量归一为 <STR>/<NUM>。代价：结构同构的不同解法可能
  误报，教师端核查后忽略（语义级查重待专项）。
- 相似度：k-gram（默认 6）token shingle 集合的 Jaccard 系数，0~1。
- 比对范围：同一道题下每个学生**最新一次**提交两两比对（含跨班），
  同班/跨班落 same_class 标记；阈值默认 0.85（教师可调）。
- 配对方向按学号升序固定（student_no_a ≤ student_no_b），
  与提交 UUID 无关，重扫方向稳定。
- 结果幂等：重扫覆盖同题旧标记（只删本problem 的标记后重插），
  历史不累积噪声。
"""
from __future__ import annotations

import io
import json
import keyword
import tokenize
import uuid
from typing import Any

from webapp.storage import _now

DEFAULT_THRESHOLD = 0.85
SHINGLE_K = 6
FLAG_LIMIT = 500  # 单题一次扫描最多落标记数（防 O(n²) 超大班级刷爆库）


def normalize_tokens(code: str) -> list[str]:
    """结构归一化 token 序列；语法错误时抛 tokenize.TokenError。

    - 注释与空白（COMMENT/NL/NEWLINE/INDENT/DEDENT）剔除；
    - 所有标识符归一为 <ID>：纯改名规避无效（v1 查重口径）；代价是
      结构同构的不同解法可能误报，教师端核查后可忽略（v1 边界如实标注）；
    - 字符串/数字字面量归一为 <STR>/<NUM>，降低噪声。"""
    toks: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(code).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                        tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
                        tokenize.ENDMARKER):
            continue
        if tok.type == tokenize.NAME and not keyword.iskeyword(tok.string):
            toks.append("<ID>")
        elif tok.type == tokenize.STRING:
            toks.append("<STR>")
        elif tok.type == tokenize.NUMBER:
            toks.append("<NUM>")
        else:
            toks.append(tok.string)
    return toks


def shingles(code: str, k: int = SHINGLE_K) -> frozenset[tuple[str, ...]]:
    tokens = normalize_tokens(code)
    if len(tokens) < k:
        return frozenset({tuple(tokens)}) if tokens else frozenset()
    return frozenset(tuple(tokens[i:i + k]) for i in range(len(tokens) - k + 1))


def similarity(code_a: str, code_b: str) -> float:
    """两份代码的 token shingle Jaccard 相似度（0~1）。"""
    a, b = shingles(code_a), shingles(code_b)
    if not a and not b:
        return 1.0  # 两份都解析不出 token（如空/纯注释）视为完全相同
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def scan_problem(store, problem_id: str, *, threshold: float = DEFAULT_THRESHOLD,
                 k: int = SHINGLE_K, flag_limit: int = FLAG_LIMIT) -> dict[str, Any]:
    """对一道题做全量查重扫描：每个学生最新提交两两比对，超阈值落标记。

    返回 {pairs_checked, flagged, threshold}；标记表按题覆盖重建（幂等）。
    """
    threshold = min(1.0, max(0.0, float(threshold)))
    with store.connect() as db:
        rows = db.execute(
            """SELECT s.id, s.code_text, st.class_name
               FROM code_submission s
               JOIN student_info st ON st.id=s.student_id
               WHERE s.problem_id=? AND s.status='judged'
                 AND s.submitted_at=(SELECT MAX(x.submitted_at) FROM code_submission x
                                     WHERE x.problem_id=s.problem_id AND x.student_id=s.student_id)
               ORDER BY st.student_no, s.submitted_at, s.id""", (problem_id,)).fetchall()
        subs = [(r["id"], r["code_text"], r["class_name"]) for r in rows]
    shingle_map = {sid: shingles(code, k) for sid, code, _ in subs}
    class_map = {sid: cls for sid, _, cls in subs}
    flags: list[dict[str, Any]] = []
    for i in range(len(subs)):
        for j in range(i + 1, len(subs)):
            id_a, id_b = subs[i][0], subs[j][0]
            a, b = shingle_map[id_a], shingle_map[id_b]
            if not a and not b:
                sim = 1.0
            elif not a or not b:
                sim = 0.0
            else:
                sim = len(a & b) / len(a | b)
            if sim >= threshold:
                # i<j 且 subs 按学号升序 → 方向固定为学号小的学生在前
                flags.append({
                    "submission_a": id_a,
                    "submission_b": id_b,
                    "similarity": round(sim, 4),
                    "same_class": 1 if class_map[id_a] == class_map[id_b] else 0,
                    "shared": len(a & b) if a and b else 0,
                })
    flags.sort(key=lambda f: -f["similarity"])
    flags = flags[:max(1, int(flag_limit))]
    now = _now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM code_plagiarism_flag WHERE problem_id=?", (problem_id,))
        for f in flags:
            db.execute(
                """INSERT INTO code_plagiarism_flag (id, problem_id, submission_a, submission_b,
                                                     similarity, same_class, detail_json, flagged_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), problem_id, f["submission_a"], f["submission_b"],
                 f["similarity"], f["same_class"],
                 json.dumps({"shared_shingles": f["shared"], "k": k, "threshold": threshold},
                            ensure_ascii=False), now))
    return {"pairs_checked": len(subs) * (len(subs) - 1) // 2,
            "flagged": len(flags), "threshold": threshold}


def list_flags(store, problem_id: str) -> list[dict[str, Any]]:
    """按相似度降序列出某题的查重标记（含双方学生与班级，便于教师核查）。"""
    with store.connect() as db:
        rows = db.execute(
            """SELECT f.id, f.similarity, f.same_class, f.flagged_at, f.detail_json,
                      sa.id AS sub_a_id, sa.student_id AS student_a,
                      sta.student_no AS student_no_a, sta.name AS name_a, sta.class_name AS class_a,
                      sb.id AS sub_b_id, sb.student_id AS student_b,
                      stb.student_no AS student_no_b, stb.name AS name_b, stb.class_name AS class_b
               FROM code_plagiarism_flag f
               JOIN code_submission sa ON sa.id=f.submission_a
               JOIN student_info sta ON sta.id=sa.student_id
               JOIN code_submission sb ON sb.id=f.submission_b
               JOIN student_info stb ON stb.id=sb.student_id
               WHERE f.problem_id=? ORDER BY f.similarity DESC""", (problem_id,)).fetchall()
    return [dict(r) for r in rows]
