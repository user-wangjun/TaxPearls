"""Public policy graph v1. Eight node/edge tables, immutable content and explicit review.

The public dictionary never stores enterprise facts. Existing YAML rules are frozen
into TaxRule nodes; their executable logic continues to use src.engine.
"""
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from src import tax_conditions as conditions


IDS = {"Policy": "policy_id", "Clause": "clause_id", "TaxRule": "rule_id", "Entity": "entity_id",
       "Metric": "metric_id", "Condition": "condition_id", "Exception": "exception_id",
       "Relationship": "relationship_id"}
KINDS = {"Policy": "policy", "Clause": "clause", "TaxRule": "rule", "Entity": "concept",
         "Metric": "metric", "Condition": "condition", "Exception": "exception"}
RELATIONS = {
    "HAS_CLAUSE": {("Policy", "Clause")},
    "DERIVES_RULE": {("Clause", "TaxRule")},
    "PROVIDES_CONTEXT": {("Clause", "TaxRule")},
    "APPLIES_TO": {("TaxRule", "Entity")},
    "USES_METRIC": {("TaxRule", "Metric"), ("Condition", "Metric")},
    "HAS_CONDITION": {("TaxRule", "Condition")},
    "HAS_EXCEPTION": {("TaxRule", "Exception")},
    "AMENDS": {("Policy", "Policy"), ("Clause", "Clause")},
    "REPEALS": {("Policy", "Policy"), ("Clause", "Clause")},
    "INTERPRETS": {("Policy", "Policy"), ("Clause", "Clause")},
    "SUPPLEMENTS": {("Policy", "Policy"), ("Clause", "Clause")},
}
JSON_FIELDS = {"condition_logic_json", "trigger_logic_json", "source_filter_json", "execution_json"}
RELATION_LABELS = {"HAS_CLAUSE": "包含条款", "DERIVES_RULE": "规定依据", "PROVIDES_CONTEXT": "政策背景",
                   "APPLIES_TO": "适用主体", "USES_METRIC": "使用指标", "HAS_CONDITION": "判断条件",
                   "HAS_EXCEPTION": "例外情形", "SOURCE_CLAUSE": "原文来源", "AMENDS": "修订",
                   "REPEALS": "废止", "INTERPRETS": "解释", "SUPPLEMENTS": "补充"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def migrate(db):
    """Additive/idempotent, also upgrades a database created with the supplied v1 SQL."""
    db.executescript(Path(__file__).with_name("policy_schema.sql").read_text(encoding="utf-8-sig"))
    additions = {
        "Policy": {"version": "TEXT NOT NULL DEFAULT '1'", "supersedes_id": "TEXT REFERENCES Policy(policy_id)"},
        "Clause": {"version": "TEXT NOT NULL DEFAULT '1'", "supersedes_id": "TEXT REFERENCES Clause(clause_id)"},
        "TaxRule": {"rule_code": "TEXT", "version": "TEXT NOT NULL DEFAULT '1'", "fingerprint": "TEXT",
                    "execution_json": "TEXT CHECK(execution_json IS NULL OR json_valid(execution_json))",
                    "conversion_json": "TEXT CHECK(conversion_json IS NULL OR json_valid(conversion_json))",
                    "supersedes_id": "TEXT REFERENCES TaxRule(rule_id)"},
        "Metric": {"logical_code": "TEXT", "version": "TEXT NOT NULL DEFAULT '1'",
                   "supersedes_id": "TEXT REFERENCES Metric(metric_id)"},
    }
    for table, fields in additions.items():
        existing = {r["name"] for r in db.execute(f'PRAGMA table_info("{table}")')}
        for name, declaration in fields.items():
            if name not in existing:
                db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')
    db.execute("CREATE INDEX IF NOT EXISTS idx_tax_rule_version ON TaxRule(rule_code, version, fingerprint)")
    from src.kg_retrieval import keyword_search, semantic_search
    keyword_search.initialize(db)
    semantic_search.initialize(db)


def get(db, table, record_id):
    if table not in IDS:
        raise ValueError("未知的知识类型。")
    row = db.execute(f'SELECT * FROM "{table}" WHERE {IDS[table]}=?', (record_id,)).fetchone()
    if row is None:
        raise ValueError(f"{table} 节点不存在：{record_id}")
    return dict(row)


def _dates(row, start="effective_from", end="effective_to"):
    a, b = row.get(start), row.get(end)
    for value in (a, b):
        if value is not None and (not isinstance(value, str) or date.fromisoformat(value).isoformat() != value):
            raise ValueError("日期须为 YYYY-MM-DD。")
    if a and b and a > b:
        raise ValueError("有效期起止倒置。")


def _check_condition(db, row):
    left = get(db, "Metric", row["left_metric_id"])
    if row["operator"] not in conditions.OPERATORS or row.get("unit") != left.get("unit"):
        raise ValueError("条件运算符或单位与指标不一致。")
    if row["right_kind"] == "metric":
        right = get(db, "Metric", row["right_metric_id"])
        if (row["operator"] in {"IN", "NOT IN"} or left["value_type"] != right["value_type"]
                or left.get("unit") != right.get("unit")):
            raise ValueError("左右指标类型、单位或集合比较不兼容。")
    elif row["operator"] in {"IN", "NOT IN"}:
        values = json.loads(row["right_literal"])
        if not isinstance(values, list) or not 1 <= len(values) <= 256:
            raise ValueError("IN/NOT IN 需要非空 JSON 数组。")
        for value in values:
            conditions.typed(value, left["value_type"])
    else:
        conditions.typed(row["right_literal"], left["value_type"])


def _check_relationship(db, row):
    pair = (row["from_type"], row["to_type"])
    if pair not in RELATIONS.get(row["relation_type"], set()):
        raise ValueError("不支持的关系类型或端点配对。")
    source, target = get(db, pair[0], row["from_id"]), get(db, pair[1], row["to_id"])
    if row["relation_type"] == "HAS_CLAUSE" and target["policy_id"] != row["from_id"]:
        raise ValueError("条款不属于关系中的政策。")
    if row["relation_type"] in {"HAS_CONDITION", "HAS_EXCEPTION"} and target["rule_id"] != row["from_id"]:
        raise ValueError("条件或例外不属于关系中的规则。")
    if row.get("evidence_clause_id"):
        get(db, "Clause", row["evidence_clause_id"])
    if row["relation_type"] in {"AMENDS", "REPEALS", "INTERPRETS", "SUPPLEMENTS"}:
        if row["from_id"] == row["to_id"] or not row.get("evidence_clause_id") or not row.get("effective_from"):
            raise ValueError("修订/废止/解释关系须有不同端点、依据条款和生效日期。")
    if row["relation_type"] == "USES_METRIC" and pair[0] == "Condition":
        if row["to_id"] not in {source["left_metric_id"], source.get("right_metric_id")}:
            raise ValueError("条件没有使用该指标。")


def validate_rule(db, rule_id, *, approval=False):
    rule = get(db, "TaxRule", rule_id)
    items = [dict(r) for r in db.execute('SELECT * FROM "Condition" WHERE rule_id=?', (rule_id,))]
    exceptions = [dict(r) for r in db.execute('SELECT * FROM "Exception" WHERE rule_id=?', (rule_id,))]
    own_ids = {r["condition_id"] for r in items}
    if not rule.get("execution_json"):
        conditions.validate_logic(json.loads(rule["condition_logic_json"]), own_ids)
    elif approval:
        raise ValueError("迁入的 YAML 规则仅保留原执行器，须完成条款与条件建模后另建审核版本。")
    for item in items:
        _check_condition(db, item)
        if approval and item["condition_source"] == "pending":
            raise ValueError("条件的法定/内部筛查来源尚未核实。")
        if item["condition_source"] == "statutory":
            if not item.get("source_clause_id"):
                raise ValueError("法定条件必须引用原文条款。")
        elif approval and rule["rule_type"] == "policy_rule":
            raise ValueError("政策规定不能使用内部筛查阈值。")
    for item in exceptions:
        conditions.validate_logic(json.loads(item["trigger_logic_json"]), own_ids)
    if not approval:
        return
    if not rule.get("valid_from") or not rule.get("applicable_region"):
        raise ValueError("规则适用时间或地区未核实。")
    for metric_id in {m for item in items for m in (item["left_metric_id"], item.get("right_metric_id")) if m}:
        metric = get(db, "Metric", metric_id)
        conditions.validate_mapping(metric)
        if metric["value_type"] == "decimal" and not metric.get("unit"):
            raise ValueError("数值指标须明确单位。")
    sources = {item["source_clause_id"] for item in [*items, *exceptions] if item.get("source_clause_id")}
    links = [dict(r) for r in db.execute(
        "SELECT * FROM Relationship WHERE (from_type='TaxRule' AND from_id=?) "
        "OR (to_type='TaxRule' AND to_id=?)", (rule_id, rule_id))]
    for edge in links:
        _check_relationship(db, edge)
        if edge.get("evidence_clause_id"):
            sources.add(edge["evidence_clause_id"])
        if edge["from_type"] == "Clause":
            sources.add(edge["from_id"])
        if edge["relation_type"] == "DERIVES_RULE" and rule["rule_type"] != "policy_rule":
            raise ValueError("内部筛查只能引用政策背景，不能伪装为条款推导的法定阈值。")
    if rule["rule_type"] == "policy_rule" and not sources:
        raise ValueError("政策规则缺少经核验的原文条款。")
    for clause_id in sources:
        _verified_source(db, clause_id)


def _verified_source(db, clause_id):
    clause = get(db, "Clause", clause_id)
    policy = get(db, "Policy", clause["policy_id"])
    if (clause["extraction_status"] != "verified" or not clause.get("source_location")
            or policy["validity_status"] == "unverified" or not policy.get("effective_from")
            or not re.fullmatch(r"[a-f0-9]{64}", policy.get("file_sha256") or "")
            or not policy.get("raw_file_path")):
        raise ValueError("政策原件、效力、条款位置或提取复核尚未完成。")
    try:
        raw = Path(policy["raw_file_path"]).read_bytes()
    except OSError:
        raise ValueError("政策原件不可读取。") from None
    if hashlib.sha256(raw).hexdigest() != policy["file_sha256"]:
        raise ValueError("政策原件哈希发生变化，请新增版本复核。")


def import_bundle(db, bundle):
    """All-or-nothing candidate import. IDs are immutable version IDs, never upserts."""
    if not isinstance(bundle, dict) or not bundle or not set(bundle) <= IDS.keys():
        raise ValueError("导入须按八类 Schema 分组。")
    if any(not isinstance(rows, list) for rows in bundle.values()) or sum(map(len, bundle.values())) > 2000:
        raise ValueError("一次最多导入两千条对象。")
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    db.execute("SAVEPOINT policy_bundle")
    touched = set()
    try:
        for table in IDS:
            columns = {r["name"] for r in db.execute(f'PRAGMA table_info("{table}")')}
            for supplied in bundle.get(table, []):
                if not isinstance(supplied, dict) or not supplied or not supplied.keys() <= columns:
                    raise ValueError(f"{table} 包含未知字段。")
                row = dict(supplied)
                if not isinstance(row.get(IDS[table]), str) or not row[IDS[table]].strip():
                    raise ValueError("节点须有非空版本 ID。")
                for key in JSON_FIELDS & row.keys():
                    if row[key] is not None:
                        row[key] = canonical(json.loads(row[key]) if isinstance(row[key], str) else row[key])
                if table in {"TaxRule", "Relationship"}:
                    if row.get("review_status", "candidate") != "candidate" or row.get("reviewed_by") or row.get("reviewed_at"):
                        raise ValueError("导入只能创建候选，不能携带审核结果。")
                if table == "TaxRule" and row.get("execution_json"):
                    raise ValueError("执行器快照只能由现有规则适配器产生。")
                if table == 'TaxRule' and row.get('conversion_json'):
                    raise ValueError('规则转换凭据只能由已验证的转换器产生。')
                if table == "Condition" and row.get("right_kind") == "literal" and not isinstance(row.get("right_literal"), str):
                    raise ValueError("条件常量须为字符串，避免金额经过浮点数转换。")
                if row.get("supersedes_id") == row[IDS[table]]:
                    raise ValueError("新版本不能替代自身。")
                _dates(row, "valid_from" if table == "TaxRule" else "effective_from",
                       "valid_to" if table == "TaxRule" else "effective_to")
                existing = db.execute(f'SELECT * FROM "{table}" WHERE {IDS[table]}=?', (row[IDS[table]],)).fetchone()
                if existing:
                    if any(existing[k] != v for k, v in row.items()):
                        raise ValueError("已有节点内容不可覆盖；请使用新版本 ID。")
                    continue
                names = list(row)
                db.execute(f'INSERT INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
                           [row[k] for k in names])
                stored = get(db, table, row[IDS[table]])
                if table == "Clause" and row.get("parent_clause_id"):
                    parent = get(db, table, row["parent_clause_id"])
                    if parent["policy_id"] != row["policy_id"]:
                        raise ValueError("父条款必须来自同一政策版本。")
                if table in {"Condition", "Exception", "Relationship"}:
                    owners = [stored.get("rule_id")]
                    if table == "Relationship":
                        _check_relationship(db, stored)
                        owners += [stored[k + "_id"] for k in ("from", "to") if stored[k + "_type"] == "TaxRule"]
                    for owner in filter(None, owners):
                        if get(db, "TaxRule", owner)["review_status"] != "candidate":
                            raise ValueError("已审核规则的条件、例外和关系不可修改。")
                        touched.add(owner)
                if table == "TaxRule":
                    touched.add(row["rule_id"])
        for rule_id in touched:
            validate_rule(db, rule_id)
        db.execute("RELEASE policy_bundle")
    except (ValueError, sqlite3.Error, TypeError, KeyError) as exc:
        db.execute("ROLLBACK TO policy_bundle")
        db.execute("RELEASE policy_bundle")
        raise ValueError(str(exc)) from None
    return {table: len(rows) for table, rows in bundle.items()}


def approve_rule(db, rule_id, reviewer):
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("审核人不能为空。")
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    rule = get(db, "TaxRule", rule_id)
    if rule["review_status"] != "candidate":
        raise ValueError("仅候选版本可以审核；修改须另建版本。")
    validate_rule(db, rule_id, approval=True)
    now = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE TaxRule SET review_status='approved', reviewed_by=?, reviewed_at=?, updated_at=? WHERE rule_id=?",
               (reviewer, now, now, rule_id))
    db.execute("UPDATE Relationship SET review_status='approved' WHERE "
               "(from_type='TaxRule' AND from_id=?) OR (to_type='TaxRule' AND to_id=?)", (rule_id, rule_id))
    return get(db, "TaxRule", rule_id)


def verify_source(db, policy_id, *, issuer, validity_status, effective_from, effective_to, clause_ids):
    """Explicit human source review, separate from AI extraction and rule approval."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    policy = get(db, "Policy", policy_id)
    if validity_status not in {"in_force", "partially_modified", "repealed", "expired"} or not issuer.strip():
        raise ValueError("须明确发布机关和已核实的效力状态。")
    if not effective_from or (validity_status in {"repealed", "expired"} and not effective_to):
        raise ValueError("须明确生效日期；已废止/失效须有终止日期。")
    _dates({"effective_from": effective_from, "effective_to": effective_to})
    if not clause_ids or len(set(clause_ids)) != len(clause_ids):
        raise ValueError("须明确选择不重复的复核条款。")
    clauses = [get(db, "Clause", cid) for cid in clause_ids]
    if any(c["policy_id"] != policy_id or not c.get("source_location") for c in clauses):
        raise ValueError("复核条款须属于本政策且有原文定位。")
    try:
        actual_hash = hashlib.sha256(Path(policy["raw_file_path"]).read_bytes()).hexdigest()
    except (OSError, TypeError):
        raise ValueError("原件不可读取。") from None
    if actual_hash != policy.get("file_sha256"):
        raise ValueError("原件哈希不一致，须新增版本。")
    changes = dict(issuer=issuer.strip(), validity_status=validity_status,
                   effective_from=effective_from, effective_to=effective_to)
    if policy["validity_status"] != "unverified" and any(policy[k] != v for k, v in changes.items()):
        raise ValueError("已复核来源的效力或时间变更须另建政策版本。")
    db.execute("UPDATE Policy SET issuer=?,validity_status=?,effective_from=?,effective_to=?,updated_at=? WHERE policy_id=?",
               (*changes.values(), datetime.now(timezone.utc).isoformat(), policy_id))
    for cid in clause_ids:
        db.execute("UPDATE Clause SET extraction_status='verified' WHERE clause_id=?", (cid,))
    return {"policy_id": policy_id, "verified_clauses": clause_ids, "validity_status": validity_status}


def import_corpus(db, manifest_path, document_ids, originals_dir):
    """Import selected, already collected originals; collection is never legal approval."""
    manifest_path = Path(manifest_path).resolve()
    root, destination = manifest_path.parent, Path(originals_dir).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not document_ids or len(set(document_ids)) != len(document_ids):
        raise ValueError("须明确选择不重复的政策文档 ID。")
    if manifest.get('graph_preparation',{}).get('format')=='policy-corpus-v1':
        from src.policy_corpus import import_plan, prepare
        return import_plan(db,prepare(manifest_path,document_ids),originals_dir)
    documents = {r["id"]: r for r in manifest["documents"]}
    bundle = {"Policy": [], "Clause": []}
    created = []
    try:
        for document_id in document_ids:
            item = documents[document_id]
            raw_path, text_path = (root / item["raw_path"]).resolve(), (root / item["text_path"]).resolve()
            if not raw_path.is_relative_to(root) or not text_path.is_relative_to(root):
                raise ValueError("来源路径超出原文目录。")
            raw, text_bytes = raw_path.read_bytes(), text_path.read_bytes()
            if (hashlib.sha256(raw).hexdigest() != item["raw_sha256"]
                    or hashlib.sha256(text_bytes).hexdigest() != item["text_sha256"]):
                raise ValueError("原文或提取文本哈希不一致。")
            destination.mkdir(parents=True, exist_ok=True)
            retained = destination / (item["raw_sha256"] + raw_path.suffix.lower())
            if retained.exists():
                if retained.read_bytes() != raw:
                    raise ValueError("留存原件哈希冲突。")
            else:
                with retained.open("xb") as stream:
                    stream.write(raw)
                created.append(retained)
            pid = document_id + ":" + item["raw_sha256"][:16]
            bundle["Policy"].append({
                "policy_id": pid, "title": item["title"], "document_no": item.get("document_no"),
                "issuer": item.get("issuer") or "发布机关待核实", "tax_type": item.get("classification", "待分类"),
                "publish_date": item.get("publish_date"), "effective_from": item.get("version_effective_from"),
                "source_url": item["source_url"], "raw_file_path": str(retained), "file_sha256": item["raw_sha256"],
                "version": item["raw_sha256"], "validity_status": "unverified"})
            matches = list(re.finditer(r"(?m)^((?:第[一二三四五六七八九十百零〇两\d]+条)|(?:[一二三四五六七八九十百]+、))[\s　]*", text_bytes.decode("utf-8-sig")))
            text = text_bytes.decode("utf-8-sig")
            if not matches:
                bundle["Clause"].append({"clause_id": pid + ":body", "policy_id": pid,
                                         "clause_label": "未分条正文", "sort_index": 0, "content": text,
                                         "source_location": item["text_path"] + ":L1", "version": item["text_sha256"],
                                         "extraction_status": "needs_review"})
                continue
            for index, match in enumerate(matches):
                label = match[1]
                content = text[match.start():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()
                bundle["Clause"].append({"clause_id": pid + ":" + label, "policy_id": pid,
                                         "clause_label": label, "sort_index": index + 1, "content": content,
                                         "source_location": f'{item["text_path"]}:L{text[:match.start()].count(chr(10)) + 1}',
                                         "version": item["text_sha256"], "extraction_status": "parsed"})
        return import_bundle(db, bundle)
    except (OSError, ValueError, KeyError) as exc:
        for path in created:
            path.unlink()
        raise ValueError(str(exc)) from None


def sync_rules(db, rules):
    """Bridge existing executable rules by stable ID + version + exact content hash."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    selected = {}
    for rule in rules:
        snapshot = asdict(rule)
        digest = fingerprint(snapshot)
        record_id = f"{rule.id}@{rule.version}:{digest[:16]}"
        selected[record_id] = rule.id
        if db.execute("SELECT 1 FROM TaxRule WHERE rule_id=?", (record_id,)).fetchone():
            from src.rule_conversion import install
            install(db, rule, record_id)
            continue
        db.execute("INSERT INTO TaxRule (rule_id,rule_code,version,fingerprint,name,tax_type,rule_type,"
                   "applicable_region,valid_from,valid_to,outcome_text,execution_json,notes) "
                   "VALUES (?,?,?,?,?,?,'risk_screen',?,?,?,?,?,?)",
                   (record_id, rule.id, rule.version, digest, rule.name, rule.tax_type, "CN",
                    rule.effective_from, rule.effective_to, rule.suggestion, canonical(snapshot),
                    "现有执行规则的版本快照；政策依据和法定适用性尚未完成图谱审核。"))
        for name in dict.fromkeys([*rule.inputs, *rule.evidence]):
            # Different definitions of a similarly named metric remain distinct.
            definition = {"name": name, "requirement": rule.inputs.get(name, "")}
            mid = "legacy:" + fingerprint(definition)[:24]
            db.execute("INSERT OR IGNORE INTO Metric (metric_id,metric_code,logical_code,name,value_type,definition) "
                       "VALUES (?,?,?,?,'decimal',?)", (mid, mid, name, name, definition["requirement"]))
            edge_id = fingerprint([record_id, mid])
            db.execute("INSERT INTO Relationship (relationship_id,from_type,from_id,relation_type,to_type,to_id) "
                       "VALUES (?,'TaxRule',?,'USES_METRIC','Metric',?)", (edge_id, record_id, mid))
        from src.rule_conversion import install
        install(db, rule, record_id)
    return selected


def graph(db, selected=None):
    """Public definitions only. Audit graphs continue using their frozen findings."""
    selected = selected or {}
    from src.models import Rule
    from src.rule_conversion import compile_rule
    converted={}
    for old in selected:
        snapshot=get(db,'TaxRule',old).get('execution_json')
        if snapshot:
            rid=compile_rule(Rule(**json.loads(snapshot)))['rule_id']
            if db.execute('SELECT 1 FROM TaxRule WHERE rule_id=?',(rid,)).fetchone():
                converted[old]=rid
    selected={**selected,**{rid:selected[old] for old,rid in converted.items()}}
    active_rules=set(converted.values())|{r['rule_id'] for r in db.execute('SELECT rule_id FROM TaxRule WHERE execution_json IS NULL AND conversion_json IS NULL')}
    active_metrics={r['to_id'] for r in db.execute("SELECT from_id,to_id FROM Relationship WHERE from_type='TaxRule' AND to_type='Metric' AND relation_type='USES_METRIC'") if r['from_id'] in active_rules}
    nodes, identifiers = [], {}
    for table in KINDS:
        for source in db.execute(f'SELECT * FROM "{table}" ORDER BY {IDS[table]}'):
            row = dict(source)
            record_id = row[IDS[table]]
            if table=='TaxRule' and record_id in converted:
                continue
            if table=='TaxRule' and row.get('conversion_json') and selected and record_id not in selected:
                continue
            if table=='Metric' and converted and row.get('metric_code','').startswith('legacy:'):
                continue
            if converted and table=='Metric' and row.get('source_schema') in {'BusinessMetric','RuleCalculation'} and record_id not in active_metrics:
                continue
            if converted and table in {'Condition','Exception'} and row['rule_id'] not in active_rules:
                continue
            if table == "TaxRule" and row.get("execution_json") and record_id not in selected:
                continue
            node = {**row, "id": selected.get(record_id, f"{table}:{record_id}"), "record_id": record_id,
                    "schema_type": table, "kind": KINDS[table],
                    "label": row.get("name") or row.get("title") or row.get("clause_label") or record_id}
            if table == "Policy":
                node["references"] = [row["source_url"]]
                node["source"] = row["source_url"]
                node["review_status"] = "unverified" if row["validity_status"] == "unverified" else "source_recorded"
            if table == "Clause":
                node["requirement"] = row["content"]
                node["source"] = row.get("source_location") or "待核实"
            if table == "Metric":
                node["requirement"] = row.get("definition") or "指标映射待配置"
            if table == "Condition":
                node["requirement"] = f'{row["left_metric_id"]} {row["operator"]} {row.get("right_literal") or row.get("right_metric_id")}'
                left = get(db, "Metric", row["left_metric_id"])
                node["label"] = f'{left["name"]} {row["operator"]} {row.get("right_literal") or row.get("right_metric_id")} {row.get("unit") or ""}'.strip()
            if table == "TaxRule" and row.get("execution_json"):
                snapshot = json.loads(row.pop("execution_json"))
                node.pop("execution_json", None)
                node.update(category=snapshot["category"], scope=snapshot["scope"],
                            rule_level=snapshot.get('rule_level','unspecified'), region=snapshot.get('region','').strip(),
                            suggestion=snapshot["suggestion"], references=snapshot["references"],
                            requirement=snapshot["threshold_basis"], legacy_references_unverified=True)
            if table=='TaxRule' and row.get('conversion_json'):
                snapshot=json.loads(row['conversion_json'])['source_rule']
                node.pop('conversion_json',None)
                node.update(category=snapshot['category'],scope=snapshot['scope'],suggestion=snapshot['suggestion'],
                            rule_level=snapshot.get('rule_level','unspecified'),region=snapshot.get('region','').strip(),
                            references=snapshot['references'],requirement=snapshot['threshold_basis'],computation_converted=True)
            identifiers[(table, record_id)] = node["id"]
            nodes.append(node)
    edges = []
    for item in db.execute("SELECT * FROM Relationship ORDER BY relationship_id"):
        row = dict(item)
        source, target = identifiers.get((row["from_type"], row["from_id"])), identifiers.get((row["to_type"], row["to_id"]))
        if source and target:
            edges.append({**row, "source": source, "target": target, "label": RELATION_LABELS.get(row["relation_type"], "待核实关系"),
                          "schema_type": "Relationship"})
    # FK links are also traversable; they do not claim a reviewed legal derivation.
    seen = {(e["source"], e["target"], e["relation_type"]) for e in edges}
    def link(a_type, a, b_type, b, label):
        source, target = identifiers.get((a_type, a)), identifiers.get((b_type, b))
        if source and target and (source, target, label) not in seen:
            edges.append({"source": source, "target": target, "label": RELATION_LABELS[label], "relation_type": label, "structural": True})
            seen.add((source, target, label))
    for table in ("Clause", "Condition", "Exception"):
        for row in db.execute(f'SELECT * FROM "{table}"'):
            if table == "Clause":
                link("Policy", row["policy_id"], table, row[IDS[table]], "HAS_CLAUSE")
            else:
                link("TaxRule", row["rule_id"], table, row[IDS[table]], "HAS_" + table.upper())
                if table == "Condition":
                    for field in ("left_metric_id", "right_metric_id"):
                        if row[field]:
                            link(table, row[IDS[table]], "Metric", row[field], "USES_METRIC")
                if row["source_clause_id"]:
                    link("Clause", row["source_clause_id"], table, row[IDS[table]], "SOURCE_CLAUSE")
    return {"schema_version": 1, "nodes": nodes, "edges": edges, "audit_id": None,
            "boundary": "候选知识仅供查看；只有明确审核的版本可进入三态条件试算。现有审计结论仍使用原执行器。"}


def evaluate_rule(db, rule_id, facts, *, start, end, region, entities=()):
    """Preview only. Return a frozen evidence bundle, never a Finding or legal conclusion."""
    rule = get(db, "TaxRule", rule_id)
    result = {"rule_id": rule_id, "rule_code": rule.get("rule_code"), "version": rule["version"],
              "truth": conditions.UNKNOWN, "disposition": "needs_review", "reasons": [], "conditions": {}, "exceptions": {}}
    if rule["review_status"] != "approved" or not rule.get("reviewed_by") or not rule.get("reviewed_at"):
        result["reasons"].append("规则版本尚未审核。")
        return result
    try:
        validate_rule(db, rule_id, approval=True)
    except ValueError as exc:
        result["reasons"].append(str(exc))
        return result
    _dates({"effective_from": start, "effective_to": end})
    if not start or not end or not rule["valid_from"]:
        result["reasons"].append("所属期或规则生效日期未知。")
        return result
    if rule["valid_from"] > start or (rule["valid_to"] and end > rule["valid_to"]):
        result["reasons"].append("规则不能覆盖整个所属期，须按政策版本分段核对。")
        return result
    if not region or rule["applicable_region"] not in {"CN", region}:
        result["reasons"].append("适用地区未确认或不匹配。")
        return result
    clauses = set()
    relevant_edges = [dict(r) for r in db.execute(
        "SELECT * FROM Relationship WHERE (from_type='TaxRule' AND from_id=?) OR "
        "(to_type='TaxRule' AND to_id=?)", (rule_id, rule_id))]
    for edge in relevant_edges:
        if edge["review_status"] != "approved":
            result["reasons"].append("规则关联关系尚未审核。")
            return result
        if edge["relation_type"] == "APPLIES_TO" and edge["to_id"] not in entities:
            result["reasons"].append("适用主体概念尚未核实。")
            return result
        if (edge["effective_from"] and edge["effective_from"] > start) or (edge["effective_to"] and edge["effective_to"] < end):
            result["reasons"].append("关系有效期不能覆盖整个所属期。")
            return result
        if edge["evidence_clause_id"]:
            clauses.add(edge["evidence_clause_id"])
        if edge["from_type"] == "Clause":
            clauses.add(edge["from_id"])
    items = [dict(r) for r in db.execute('SELECT * FROM "Condition" WHERE rule_id=? ORDER BY condition_id', (rule_id,))]
    exceptions = [dict(r) for r in db.execute('SELECT * FROM "Exception" WHERE rule_id=? ORDER BY exception_id', (rule_id,))]
    clauses |= {r["source_clause_id"] for r in [*items, *exceptions] if r["source_clause_id"]}
    sources = []
    for cid in sorted(clauses):
        clause = get(db, "Clause", cid)
        policy = get(db, "Policy", clause["policy_id"])
        a, b = clause.get("effective_from") or policy["effective_from"], clause.get("effective_to") or policy["effective_to"]
        if (not a or a > start or (b and b < end)
                or (policy["validity_status"] in {"repealed", "expired", "partially_modified"} and not b)):
            result["reasons"].append("政策/条款效力不能覆盖所属期或终止日期待核实。")
            return result
        sources.append({"clause": clause, "policy": policy})
    definitions = {mid: get(db, "Metric", mid) for item in items
                   for mid in (item["left_metric_id"], item.get("right_metric_id")) if mid}
    values = {r["condition_id"]: conditions.compare(r, definitions, facts) for r in items}
    result["conditions"] = values
    result["truth"] = conditions.logic_value(json.loads(rule["condition_logic_json"]), values)
    if result["truth"] == conditions.UNKNOWN:
        result["reasons"].append("条件所需数据缺失、类型/单位不一致或来源待核实。")
    effects = []
    for item in exceptions:
        value = conditions.logic_value(json.loads(item["trigger_logic_json"]), values)
        result["exceptions"][item["exception_id"]] = value
        if value == conditions.UNKNOWN:
            effects.append("needs_review")
        elif value == conditions.TRUE:
            effects.append(item["effect"])
    if "needs_review" in effects or "adjust_outcome" in effects:
        result["truth"] = conditions.UNKNOWN
        result["disposition"] = "needs_review"
        result["reasons"].append("例外待核实或要求调整结果，保留人工复核。")
    elif "exclude" in effects:
        result["disposition"] = "excluded"
    elif result["truth"] != conditions.UNKNOWN:
        result["disposition"] = "condition_met" if result["truth"] == conditions.TRUE else "condition_not_met"
    snapshot = {"rule": rule, "conditions": items, "exceptions": exceptions, "metrics": definitions,
                "sources": sources, "relationships": relevant_edges,
                "facts": {key: facts.get(key) for key in definitions},
                "context": {"start": start, "end": end, "region": region, "entities": sorted(entities)}}
    result.update(snapshot=snapshot, snapshot_hash=fingerprint(snapshot))
    return result
