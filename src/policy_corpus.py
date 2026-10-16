"""Offline arrangement of retained policy originals into schema-v1 candidates.

Every clause is an exact source-text slice. Legal applicability, executable
conditions and reviewer approval are deliberately left to the existing workflow.
"""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3


FORMAT = "policy-corpus-v1"
PARSER_VERSION = 2
TAX_TYPES = {"增值税": "VAT", "个人所得税": "PIT", "企业所得税": "EIT",
             "征管通用": "TAX_ADMIN", "会计规范": "ACCOUNTING", "社保依据": "SOCIAL_INSURANCE"}
NUM = "一二三四五六七八九十百零〇两0123456789"
ARTICLE = re.compile(rf"(?m)^[\t 　]*(第[{NUM}]+条)")
HEADING = re.compile(rf"(?m)^[\t 　]*(第[{NUM}]+[章节])[^\n]*$")
SECTION = re.compile(rf"(?m)^[\t 　]*([{NUM}]+、)[^\n]*")
PAGE = re.compile(r"(?m)^【PDF第(\d+)页】")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def checked_file(root, relative, expected):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("来源文件须使用清单内的相对路径。")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("来源路径超出原文目录。")
    data = path.read_bytes()
    if not isinstance(expected, str) or sha(data) != expected:
        raise ValueError(f"来源文件哈希不一致：{relative}")
    return path, data


def number(text):
    digits = dict(zip("零〇一二三四五六七八九", [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]))
    if text.isdigit():
        return int(text)
    total, current = 0, 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char in {"十", "百"}:
            total += (current or 1) * {"十": 10, "百": 100}[char]
            current = 0
        elif char == "两":
            current = 2
        else:
            raise ValueError("无法识别中文条号。")
    return total + current


def segments(text, *, amending=False, prefix=""):
    """Partition all characters, retaining notices, tables, headings and exceptions.

    Articles stay intact, including their paragraphs/list items. Splitting an
    exception away from its article would weaken the current retrieval context.
    """
    if not text.strip():
        raise ValueError("提取正文为空。")
    matches = [] if amending else list(ARTICLE.finditer(text))
    if matches:
        labels = [m[1] for m in matches]
        nums = [number(label[1:-1]) for label in labels]
        if nums != list(range(1, len(nums) + 1)):
            raise ValueError("条号重复、缺失或不连续，不能自动整理为完整法规。")
        markers = [(m.start(), m[1], "article") for m in matches]
        # Ignore table-of-contents headings; the last heading before article 1
        # is the actual first chapter. Later headings become exact small nodes.
        heads = list(HEADING.finditer(text))
        before = [h for h in heads if h.start() < matches[0].start()]
        chapters = [h for h in before if h[1].endswith("章")]
        first_headers = ([h for h in before if h.start() >= chapters[-1].start()]
                         if chapters else before[-1:])
        chosen = first_headers + [h for h in heads if h.start() > matches[0].start()]
        markers += [(h.start(), h.group().strip(), "heading") for h in chosen]
    else:
        sections = list(SECTION.finditer(text))
        labels = [s[1] for s in sections]
        if len(set(labels)) != len(labels):
            # A repeated sequence could be several documents or a table.
            # Preserve it as one explicitly unresolved unit instead of guessing.
            markers = [(0, "未可靠分条正文", "unresolved")]
        else:
            markers = [(s.start(), s[1], "amendment_item" if amending else "section") for s in sections]
    if not markers:
        markers = [(0, "未分条正文", "unresolved")]
    elif min(m[0] for m in markers) > 0:
        markers.append((0, "修订决定与版本说明" if amending else "前言与版本说明", "preamble"))
    markers.sort()
    result, chapter, section = [], None, None
    for index, (start, label, kind) in enumerate(markers):
        end = markers[index + 1][0] if index + 1 < len(markers) else len(text)
        # Preserve newlines and whitespace as well: coverage is exactly [0,n).
        if not text[start:end].strip():
            continue
        full_label = prefix + label
        parent = section or chapter
        if kind == "heading":
            if re.match(rf"第[{NUM}]+章", label):
                parent, chapter, section = None, full_label, None
            else:
                # Section labels can repeat in different chapters. Namespace
                # them under their chapter to satisfy Clause's unique label.
                full_label = (chapter + "/" + label) if chapter else full_label
                parent, section = chapter, full_label
        result.append({"label": full_label, "kind": kind, "start": start, "end": end,
                       "parent_label": parent if kind in {"heading", "article"} else None})
    if "".join(text[s["start"]:s["end"]] for s in result) != text:
        raise ValueError("条款整理未覆盖完整原文。")
    if len({s["label"] for s in result}) != len(result):
        raise ValueError("条款标签重复，须核对原文结构。")
    return result


def prepare(manifest_path, document_ids=None):
    """Read-only preparation, no DB, files, network or LLM side effects."""
    path = Path(manifest_path).resolve()
    root = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    config = manifest.get("graph_preparation", {})
    if config.get("format") != FORMAT:
        raise ValueError("来源清单缺少当前图谱整理配置。")
    records = manifest["documents"]
    documents = {d["id"]: d for d in records}
    if len(documents) != len(records):
        raise ValueError("清单文档ID重复。")
    ids = list(documents) if document_ids is None else list(document_ids)
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= documents.keys():
        raise ValueError("须选择存在且不重复的政策文档ID。")
    aliases = []
    selected = []
    for key in ids:
        alias = documents[key].get("same_document_as")
        if alias:
            if alias not in documents:
                raise ValueError("同文异源主记录不存在。")
            aliases.append({"document_id": key, "canonical_document_id": alias,
                            "source_url": documents[key]["source_url"]})
            key = alias
        if key not in selected:
            selected.append(key)
    bundle = {"Policy": [], "Clause": [], "Relationship": []}
    sources, summaries, files, warnings = [], [], {}, []

    def remember(relative, checksum):
        file, data = checked_file(root, relative, checksum)
        files[relative] = {"path": str(file), "sha256": checksum,
                           "suffix": file.suffix.lower(), "bytes": len(data)}
        return file, data

    # Validate alias snapshots too; they remain preserved evidence, never a
    # duplicate Policy that would bias search scores and node counts.
    for alias in aliases:
        item = documents[alias["document_id"]]
        remember(item["raw_path"], item["raw_sha256"])
        remember(item["text_path"], item["text_sha256"])

    for key in selected:
        item = documents[key]
        annotation = config.get("documents", {}).get(key, {})
        classification = item.get("classification")
        if classification not in TAX_TYPES:
            raise ValueError("税种或支撑依据尚未分类。")
        raw_path, _ = remember(item["raw_path"], item["raw_sha256"])
        _, text_bytes = remember(item["text_path"], item["text_sha256"])
        # Include interpretation/metadata version as well as original and text
        # hashes. The same HTML with improved extraction needs a new node ID.
        version_input = {"format": FORMAT, "parser_version": PARSER_VERSION, "source": item, "annotation": annotation}
        version = sha(canonical(version_input).encode())
        pid = f"{key}:organized:{version[:20]}"
        end = item.get("version_effective_to_exclusive")
        if end:
            end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
        policy = {"policy_id": pid, "title": item["title"], "document_no": annotation.get("document_no"),
                  "issuer": annotation.get("issuer") or "发布机关待核实", "tax_type": TAX_TYPES[classification],
                  "publish_date": annotation.get("publish_date"),
                  "effective_from": item.get("version_effective_from"), "effective_to": end,
                  "region_code": "CN", "source_url": item["source_url"], "raw_file_path": str(raw_path),
                  "file_sha256": item["raw_sha256"], "version": version,
                  "fetched_at": item["fetched_at"], "validity_status": "unverified"}
        bundle["Policy"].append(policy)
        summary = {"document_id": key, "policy_id": pid, "classification": classification,
                   "tax_type": policy["tax_type"], "domain": item.get("domain"), "legal_level": item.get("legal_level"),
                   "edition": item.get("edition"), "legal_document_date": item.get("legal_document_date"),
                   "source_page_metadata": item.get("page_metadata", {}),
                   "temporal_note": item.get("temporal_note"), "phase_in_conditions": item.get("phase_in_conditions", []),
                   "existing_rule_citations": item.get("existing_rule_citations", []), "sources": [], "deferred_attachments": [],
                   "supporting_sources": item.get("supporting_sources", [])}
        for evidence in item.get("supporting_sources", []):
            remember(evidence["raw_path"], evidence["raw_sha256"])
            if evidence.get("text_path"):
                remember(evidence["text_path"], evidence["text_sha256"])

        def add_text(text_data, relative, checksum, raw_relative, raw_checksum, *, prefix="", attachment=False, amending=False):
            text = text_data.decode("utf-8-sig")
            pieces = segments(text, amending=amending, prefix=prefix)
            ids_by_label = {s["label"]: pid + ":" + s["label"] for s in pieces}
            prior_count = sum(c["policy_id"] == pid for c in bundle["Clause"])
            for index, part in enumerate(pieces, prior_count + 1):
                start, stop = part["start"], part["end"]
                location = {"text_path": relative, "text_sha256": checksum,
                            "raw_path": raw_relative, "raw_sha256": raw_checksum,
                            "start_char": start, "end_char_exclusive": stop,
                            "start_line": text[:start].count("\n") + 1,
                            "end_line": text[:stop].rstrip("\n").count("\n") + 1}
                if attachment and raw_relative.lower().endswith(".pdf"):
                    page_positions = list(PAGE.finditer(text))
                    covered = [int(p[1]) for p in page_positions if start <= p.start() < stop]
                    before = [int(p[1]) for p in page_positions if p.start() < start]
                    if before:
                        covered.insert(0, before[-1])
                    location["pdf_pages"] = sorted(set(covered))
                status = "needs_review" if part["kind"] in {"preamble", "unresolved", "amendment_item"} or attachment else "parsed"
                clause = {"clause_id": ids_by_label[part["label"]], "policy_id": pid,
                          "parent_clause_id": ids_by_label.get(part["parent_label"]),
                          "clause_label": part["label"], "sort_index": index,
                          "content": text[start:stop], "source_location": canonical(location),
                          "version": checksum, "effective_from": None, "effective_to": None,
                          "extraction_status": status}
                bundle["Clause"].append(clause)
            source = {"policy_id": pid, "document_id": key, "text_path": relative,
                      "text_sha256": checksum, "raw_path": raw_relative, "raw_sha256": raw_checksum,
                      "clause_ids": [ids_by_label[p["label"]] for p in pieces],
                      "coverage_chars": len(text), "exact_coverage": True, "attachment": attachment}
            sources.append(source)
            summary["sources"].append(source)

        add_text(text_bytes, item["text_path"], item["text_sha256"], item["raw_path"], item["raw_sha256"],
                 amending=item.get("document_kind") == "amending_decision")
        attachment_urls = {a.get("source_url") for a in item.get("attachments", [])}
        if any(asset["source_url"] not in attachment_urls for asset in item.get("embedded_assets", [])):
            raise ValueError("正文内嵌资源尚未留存，不能声称原文收集完整。")
        for index, attachment in enumerate(item.get("attachments", []), 1):
            remember(attachment["path"], attachment["sha256"])
            if attachment.get("text_path"):
                _, content = remember(attachment["text_path"], attachment["text_sha256"])
                if key == "INVOICE-DETAILS-2024":
                    summary["deferred_attachments"].append({"path": attachment["path"],
                        "reason": "修订决定及细则的PDF复本已留存；已按同页面HTML全文整理，不重复建立同文条款。"})
                else:
                    add_text(content, attachment["text_path"], attachment["text_sha256"],
                             attachment["path"], attachment["sha256"], prefix=f"附件{index}/", attachment=True)
            else:
                summary["deferred_attachments"].append({"path": attachment["path"],
                    "reason": "官方DOC附件原件已保留，表格/样式尚未人工核验，不自动转换为计算条件。"})
        if not policy["publish_date"]:
            warnings.append({"document_id": key, "kind": "publish_date_unknown",
                             "detail": "保留网页日期和成文日期；未将页面生成或转载日期推定为政策公布日期。"})
        if not policy["effective_from"]:
            warnings.append({"document_id": key, "kind": "version_effective_date_unknown",
                             "detail": "修订版本施行日待核实，不使用原法末条替代。"})
        if summary["phase_in_conditions"]:
            warnings.append({"document_id": key, "kind": "subject_specific_phase_in",
                             "detail": "保留分主体实施时间及提前执行条件，尚未自动生成为可执行规则。"})
        summaries.append(summary)

    by_doc = {s["document_id"]: s["policy_id"] for s in summaries}
    by_clause = {(c["policy_id"], c["clause_label"]): c for c in bundle["Clause"]}
    for relation in config.get("relationships", []):
        if not {relation["from_document_id"], relation["to_document_id"], relation["evidence_document_id"]} <= by_doc.keys():
            continue
        evidence = by_clause[(by_doc[relation["evidence_document_id"]], relation["evidence_clause_label"])]
        edge = {"from_type": "Policy", "from_id": by_doc[relation["from_document_id"]],
                "relation_type": relation["relation_type"], "to_type": "Policy", "to_id": by_doc[relation["to_document_id"]],
                "evidence_clause_id": evidence["clause_id"], "effective_from": relation["effective_from"],
                "review_status": "candidate"}
        edge["relationship_id"] = "corpus:" + sha(canonical(edge).encode())[:32]
        bundle["Relationship"].append(edge)

    return {"format": FORMAT, "parser_version": PARSER_VERSION, "schema_version": 1, "manifest_sha256": sha(path.read_bytes()),
            "bundle": bundle, "documents": summaries, "source_coverage": sources, "files": files,
            "source_aliases": aliases, "warnings": warnings,
            "unresolved_relations": config.get("unresolved_relations", []),
            "boundary": "已整理为待审核候选；没有自动核实政策效力、适用性或批准规则。"}


def import_plan(db, plan, originals_dir, *, rule_ids=None):
    """Retain every original and atomically import bounded candidate batches."""
    from webapp import policy_graph as pg
    destination = Path(originals_dir).resolve()
    rows = json.loads(json.dumps(plan["bundle"], ensure_ascii=False))
    created, retained = [], {}
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    db.execute("SAVEPOINT prepared_corpus")
    try:
        # Recheck all inputs immediately before any writes; even source aliases
        # and deferred attachments must pass hash validation.
        contents = {}
        for relative, info in plan["files"].items():
            data = Path(info["path"]).read_bytes()
            if sha(data) != info["sha256"]:
                raise ValueError("导入前来源文件哈希发生变化。")
            contents[relative] = data
        destination.mkdir(parents=True, exist_ok=True)
        for relative, data in contents.items():
            info = plan["files"][relative]
            target = destination / (info["sha256"] + info["suffix"])
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError("留存原件哈希冲突。")
            else:
                with target.open("xb") as output:
                    output.write(data)
                created.append(target)
            retained[relative] = str(target)
        for row in rows["Policy"]:
            suffix = Path(row["raw_file_path"]).suffix.lower()
            row["raw_file_path"] = str(destination / (row["file_sha256"] + suffix))
        # source_location records include both original relative coordinates and
        # retained paths, so they remain traceable if the collection moves.
        for row in rows["Clause"]:
            location = json.loads(row["source_location"])
            location["retained_raw_path"] = retained[location["raw_path"]]
            location["retained_text_path"] = retained[location["text_path"]]
            row["source_location"] = canonical(location)
        if rule_ids:
            by_clause = {(c["policy_id"], c["clause_label"]): c for c in rows["Clause"]}
            for item in plan["documents"]:
                for citation in item["existing_rule_citations"]:
                    if citation["rule_id"] not in rule_ids:
                        continue
                    for label in citation["article_labels"]:
                        clause = by_clause[(item["policy_id"], label)]
                        edge = {"from_type": "Clause", "from_id": clause["clause_id"], "relation_type": "PROVIDES_CONTEXT",
                                "to_type": "TaxRule", "to_id": rule_ids[citation["rule_id"]],
                                "evidence_clause_id": clause["clause_id"], "review_status": "candidate"}
                        edge["relationship_id"] = "corpus:" + sha(canonical(edge).encode())[:32]
                        rows["Relationship"].append(edge)
        for table, items in rows.items():
            for start in range(0, len(items), 1500):
                pg.import_bundle(db, {table: items[start:start + 1500]})
        db.execute("RELEASE prepared_corpus")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        db.execute("ROLLBACK TO prepared_corpus")
        db.execute("RELEASE prepared_corpus")
        for path in created:
            path.unlink()
        raise ValueError(str(exc)) from None
    return {table: len(items) for table, items in rows.items()}
