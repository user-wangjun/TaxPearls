"""FTS5 trigram search with bounded, escaped two-character fallback."""
import re
from .common import clause_record, candidate


def initialize(db):
    db.executescript('''
        CREATE VIRTUAL TABLE IF NOT EXISTS kg_clause_fts USING fts5(
            clause_id UNINDEXED, content, tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS kg_clause_fts_insert AFTER INSERT ON Clause BEGIN
            INSERT INTO kg_clause_fts(rowid,clause_id,content) VALUES(new.rowid,new.clause_id,new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS kg_clause_fts_delete AFTER DELETE ON Clause BEGIN
            DELETE FROM kg_clause_fts WHERE rowid=old.rowid;
        END;
        CREATE TRIGGER IF NOT EXISTS kg_clause_fts_update AFTER UPDATE OF content,clause_id ON Clause BEGIN
            DELETE FROM kg_clause_fts WHERE rowid=old.rowid;
            INSERT INTO kg_clause_fts(rowid,clause_id,content) VALUES(new.rowid,new.clause_id,new.content);
        END;
        INSERT INTO kg_clause_fts(rowid,clause_id,content)
            SELECT rowid,clause_id,content FROM Clause
            WHERE rowid NOT IN (SELECT rowid FROM kg_clause_fts);
    ''')


def terms(query, supplied=()):
    # Explicit words plus bounded trigrams recover Chinese phrases inside a natural-language topic.
    words = re.findall(r"[\u3400-\u9fffA-Za-z0-9_]+", query)
    result = [str(v).strip() for v in supplied if isinstance(v, str) and len(v.strip()) >= 2]
    for word in words:
        if len(word) >= 2:
            result.append(word[:80])
        if len(word) > 5 and re.search(r"[\u3400-\u9fff]", word):
            result.extend(word[i:i + 3] for i in range(len(word) - 2))
    return list(dict.fromkeys(result))[:64]


def search(db, query, *, keywords=(), limit=20):
    if not 1 <= limit <= 100 or len(query) > 4000:
        raise ValueError("关键词请求超出范围。")
    search_terms = terms(query, keywords)
    long = [word for word in search_terms if len(word) >= 3]
    short = [word for word in search_terms if len(word) == 2]
    found = {}
    if long:
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in long)
        for row in db.execute("SELECT clause_id,bm25(kg_clause_fts) AS rank FROM kg_clause_fts "
                              "WHERE kg_clause_fts MATCH ? ORDER BY rank,clause_id LIMIT ?", (expression, limit)):
            found[row["clause_id"]] = {"score": -row["rank"], "mode": "fts5_trigram"}
    for word in short:
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        for row in db.execute("SELECT clause_id FROM Clause WHERE content LIKE ? ESCAPE '\\' ORDER BY clause_id LIMIT ?",
                              ("%" + escaped + "%", limit)):
            found.setdefault(row["clause_id"], {"score": 0, "mode": "short_term_like"})
    hits = []
    for cid, details in found.items():
        clause, policy = clause_record(db, cid)
        matched = [word for word in search_terms if word.casefold() in clause["content"].casefold()]
        hits.append(candidate(clause, policy, "keyword", score=details["score"], reason={"mode": details["mode"], "matched_terms": matched}))
    hits.sort(key=lambda hit: (-hit["score"], hit["clause_id"]))
    return {"status": "ready", "hits": hits[:limit], "terms": search_terms}
