"""Real local BGE/ONNX inference and FAISS, with versioned incremental clause vectors."""
from functools import lru_cache
import hashlib
import json
import os
import re
from pathlib import Path
from threading import RLock

from .common import candidate, clause_record, digest
from .passages import VERSION

_LOCK = RLock()


def default_model_dir():
    configured = os.getenv("TAXPEARLS_KG_MODEL_DIR")
    return Path(configured) if configured else Path(os.getenv("LOCALAPPDATA", Path.home() / ".cache")) / "TaxPearls/models/bge-small-zh-v1.5-onnx"


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS kg_clause_vectors (
        vector_id INTEGER PRIMARY KEY AUTOINCREMENT,
        clause_id TEXT NOT NULL REFERENCES Clause(clause_id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        model_fingerprint TEXT NOT NULL,
        vector BLOB NOT NULL,
        UNIQUE(clause_id,chunk_index,model_fingerprint)
    )''')
    db.execute("CREATE INDEX IF NOT EXISTS idx_kg_vectors_model ON kg_clause_vectors(model_fingerprint,clause_id)")
    columns = {r[1] for r in db.execute('PRAGMA table_info(kg_clause_vectors)')}
    for name, definition in [('source_char_start', 'INTEGER'), ('source_char_end', 'INTEGER'),
                             ('chunking_version', "TEXT NOT NULL DEFAULT 'legacy'")]:
        if name not in columns:
            db.execute(f'ALTER TABLE kg_clause_vectors ADD COLUMN {name} {definition}')


class LocalEncoder:
    def __init__(self, directory):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        directory = Path(directory).resolve()
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("pooling") != "cls" or manifest.get("max_tokens") != 512:
            raise ValueError("不支持的本地模型配置。")
        for name, checksum in manifest["files"].items():
            file = (directory / name).resolve()
            if not file.is_relative_to(directory) or hashlib.sha256(file.read_bytes()).hexdigest() != checksum:
                raise ValueError("本地模型完整性检查失败。")
        self.manifest = manifest
        self.fingerprint = digest(manifest)
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, os.cpu_count() or 1)
        self.session = ort.InferenceSession(str(directory / "onnx/model_quantized.onnx"),
                                           sess_options=options, providers=["CPUExecutionProvider"])

    def chunks(self, text):
        return [span['text'] for span in self.chunk_spans(text)]

    def chunk_spans(self, text):
        # Natural paragraphs stay intact when they fit. Oversized paragraphs
        # use overlapping token windows, mapped back to exact original offsets.
        result, pending, pending_tokens = [], None, 0
        def append(a, b):
            result.append({'text': text[a:b], 'source_char_start': a, 'source_char_end': b})
        for paragraph in re.finditer(r'[^\n]*\n|[^\n]+$', text):
            begin, end = paragraph.span()
            body = paragraph.group()
            with _LOCK:
                self.tokenizer.no_padding()
                try:
                    encoded = self.tokenizer.encode(body, add_special_tokens=False)
                finally:
                    self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            if len(encoded.ids) <= 480:
                if pending is not None and pending_tokens + len(encoded.ids) > 480:
                    append(pending, begin)
                    pending, pending_tokens = None, 0
                if pending is None:
                    pending = begin
                pending_tokens += len(encoded.ids)
                continue
            if pending is not None:
                append(pending, begin)
                pending, pending_tokens = None, 0
            for start in range(0, len(encoded.ids), 400):
                offsets = encoded.offsets[start:start + 480]
                a = begin if start == 0 else begin + offsets[0][0]
                b = end if start + 480 >= len(encoded.ids) else begin + offsets[-1][1]
                append(a, b)
                if start + 480 >= len(encoded.ids):
                    break
        if pending is not None:
            append(pending, len(text))
        return result or [{'text': text, 'source_char_start': 0, 'source_char_end': len(text)}]

    def encode(self, texts, *, query=False):
        np, vectors = self.np, []
        for start in range(0, len(texts), 8):
            batch = texts[start:start + 8]
            if query:
                batch = [self.manifest["query_prefix"] + text for text in batch]
            with _LOCK:
                encoded = self.tokenizer.encode_batch(batch)
            if any(len(row.ids) > 512 for row in encoded):
                raise ValueError("语义查询超过模型窗口，请缩短检查主题。")
            arrays = {"input_ids": np.asarray([e.ids for e in encoded], dtype="int64"),
                      "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype="int64"),
                      "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype="int64")}
            names = {item.name for item in self.session.get_inputs()}
            outputs = self.session.run(None, {name: arrays[name] for name in names})
            output = outputs[0]
            embeddings = output[:, 0, :] if output.ndim == 3 else output
            embeddings = np.asarray(embeddings, dtype="float32")
            embeddings /= np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-12)
            vectors.extend(embeddings)
        return np.asarray(vectors, dtype="float32")


@lru_cache(maxsize=2)
def encoder(directory, manifest_stat):
    # Replacing the manifest changes the cache key; all file checks are local.
    return LocalEncoder(directory)


def available(directory=None):
    directory = Path(directory) if directory else default_model_dir()
    try:
        stamp = (directory / "manifest.json").stat()
        model = encoder(str(directory.resolve()), (stamp.st_mtime_ns, stamp.st_size))
        import faiss  # noqa: F401
        return model, None
    except (OSError, ValueError, ImportError, RuntimeError, KeyError) as exc:
        return None, f"本地语义路线未就绪：{type(exc).__name__}"


def synchronize(db, model):
    """Embed only changed clauses. SQL cache is the durable ID ↔ clause/version mapping."""
    np = model.np
    initialize(db)
    old = {r["clause_id"]: (r["content_hash"], r['chunking_version']) for r in db.execute(
        "SELECT clause_id,content_hash,chunking_version FROM kg_clause_vectors WHERE model_fingerprint=?", (model.fingerprint,))}
    current = {r["clause_id"]: (r["content"], r["version"]) for r in db.execute("SELECT clause_id,content,version FROM Clause")}
    updated, removed = [], sorted(set(old) - current.keys())
    for cid in removed:
        db.execute("DELETE FROM kg_clause_vectors WHERE clause_id=? AND model_fingerprint=?", (cid, model.fingerprint))
    for cid, (text, version) in current.items():
        fingerprint = digest([text, version])
        if old.get(cid) == (fingerprint, VERSION):
            continue
        spans = model.chunk_spans(text)
        vectors = model.encode([s['text'] for s in spans])
        db.execute("DELETE FROM kg_clause_vectors WHERE clause_id=? AND model_fingerprint=?", (cid, model.fingerprint))
        for index, vector in enumerate(vectors):
            span = spans[index]
            db.execute("INSERT INTO kg_clause_vectors(clause_id,chunk_index,content_hash,model_fingerprint,vector,source_char_start,source_char_end,chunking_version) VALUES (?,?,?,?,?,?,?,?)",
                       (cid, index, fingerprint, model.fingerprint, np.asarray(vector, dtype="float32").tobytes(),
                        span['source_char_start'], span['source_char_end'], VERSION))
        updated.append(cid)
    return {"updated_clause_ids": updated, "removed_clause_ids": removed, "model_fingerprint": model.fingerprint}


def build_faiss(db, model):
    import faiss
    np = model.np
    records = list(db.execute("SELECT * FROM kg_clause_vectors WHERE model_fingerprint=? ORDER BY vector_id", (model.fingerprint,)))
    if not records:
        return None, {}, digest([])
    vectors = np.stack([np.frombuffer(r["vector"], dtype="float32") for r in records])
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(vectors.shape[1]))
    index.add_with_ids(vectors, np.asarray([r["vector_id"] for r in records], dtype="int64"))
    mapping = {r["vector_id"]: {"clause_id": r["clause_id"], "chunk_index": r["chunk_index"], "content_hash": r["content_hash"],
                               'source_char_start': r['source_char_start'], 'source_char_end': r['source_char_end']} for r in records}
    return index, mapping, digest([model.fingerprint, mapping])


def export_index(db, destination, directory=None):
    model, reason = available(directory)
    if model is None:
        return {"status": "not_ready", "reason": reason}
    result = synchronize(db, model)
    index, mapping, version = build_faiss(db, model)
    if index is None:
        return {"status": "not_ready", "reason": "没有可索引的条款。"}
    import faiss
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    # A versioned export never overwrites another generation; runtime rebuilds from SQL cache.
    file = destination / (version + ".faiss")
    if not file.exists():
        faiss.write_index(index, str(file))
    metadata = {"index_version": version, "model": model.manifest, "mapping": mapping,
                "index_sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    (destination / (version + ".json")).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ready", **result, "index_version": version, "vector_count": index.ntotal, "index_path": str(file)}


def search(db, query, *, limit=20, directory=None):
    if not 1 <= limit <= 100 or not query.strip() or len(query) > 2000:
        raise ValueError("语义查询长度或候选数无效。")
    model, reason = available(directory)
    if model is None:
        return {"status": "not_ready", "hits": [], "reason": reason}
    update = synchronize(db, model)
    index, mapping, version = build_faiss(db, model)
    if index is None:
        return {"status": "ready", "hits": [], "model_fingerprint": model.fingerprint}
    query_vector = model.encode([query], query=True)
    take = min(index.ntotal, limit * 2)
    while True:
        scores, identifiers = index.search(query_vector, take)
        unique = {mapping[int(identifier)]["clause_id"] for identifier in identifiers[0] if int(identifier) in mapping}
        if len(unique) >= limit or take >= index.ntotal:
            break
        take = min(index.ntotal, take * 2)
    hits, seen = [], {}
    for score, identifier in zip(scores[0], identifiers[0]):
        row = mapping.get(int(identifier))
        if not row:
            continue
        cid = row['clause_id']
        if cid not in seen:
            if len(hits) >= limit:
                continue
            clause, policy = clause_record(db, cid)
            hit = candidate(clause, policy, "semantic", score=float(score),
                            reason={"model_fingerprint": model.fingerprint, "chunk_index": row["chunk_index"], 'spans': []})
            seen[cid] = (hit, hashlib.sha256(clause['content'].encode()).hexdigest())
            hits.append(hit)
        hit, checksum = seen[cid]
        if float(score) >= max(.60, hit['score'] - .06) and row['source_char_start'] is not None:
            hit['reason']['spans'].append({'source_char_start': row['source_char_start'],
                'source_char_end': row['source_char_end'], 'original_text_sha256': checksum, 'score': float(score)})
    return {"status": "ready", "hits": hits, "index_version": version, "model_fingerprint": model.fingerprint, **update}
