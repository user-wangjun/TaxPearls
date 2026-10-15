"""Explicit online preparation of a pinned, local BGE model; retrieval never downloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REVISION = "9507db33464b5da99a532ac26b2a251767cbc62b"
UPSTREAM_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
REPO = "onnx-community/bge-small-zh-v1.5-ONNX"
FILES = {
    "onnx/model_quantized.onnx": "99a6e522710c00220c89f8c52e0cc5aa09d4cbb1c34c0e932eab3a9dfdc65df3",
    "onnx/model_quantized.onnx_data": "952623481ca8beea884e3d3c9ecaf8a3c7bf1d0c21de29e970cd31af9d37a90b",
    "tokenizer.json": None,
    "config.json": None,
}


def prepare(destination):
    destination = Path(destination).resolve()
    manifest = {"model_id": "BAAI/bge-small-zh-v1.5", "upstream_revision": UPSTREAM_REVISION,
                "onnx_repository": REPO, "revision": REVISION, "license": "MIT", "pooling": "cls",
                "max_tokens": 512, "query_prefix": "为这个句子生成表示以用于检索相关文章：", "files": {}}
    urls = {name: f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}" for name in FILES}
    urls["UPSTREAM_README.md"] = f"https://huggingface.co/BAAI/bge-small-zh-v1.5/resolve/{UPSTREAM_REVISION}/README.md"
    destination.mkdir(parents=True, exist_ok=True)
    for name, url in urls.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = FILES.get(name)
        if target.exists():
            raw = target.read_bytes()
        else:
            # Only the explicit prepare command has network access; no executable model code.
            with urlopen(url, timeout=60) as response:
                raw = response.read()
            if expected and hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"模型文件哈希不匹配：{name}")
            with target.open("xb") as stream:
                stream.write(raw)
        checksum = hashlib.sha256(raw).hexdigest()
        if expected and checksum != expected:
            raise ValueError(f"已有模型文件哈希不匹配，不覆盖：{name}")
        manifest["files"][name] = checksum
        print(f"verified {name} ({len(raw)} bytes)", flush=True)
    if "license: mit" not in (destination / "UPSTREAM_README.md").read_text(encoding="utf-8").lower():
        raise ValueError("上游许可声明与预期不一致。")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    with tempfile.NamedTemporaryFile(dir=destination, prefix="manifest-", suffix=".tmp", delete=False) as stream:
        stream.write(payload)
        temporary = stream.name
    os.replace(temporary, destination / "manifest.json")
    return manifest


def main():
    from src.kg_retrieval.semantic_search import default_model_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=default_model_dir())
    args = parser.parse_args()
    prepare(args.destination)
    print(str(args.destination.resolve()))


if __name__ == "__main__":
    main()
