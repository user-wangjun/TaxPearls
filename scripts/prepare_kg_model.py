"""Explicit online preparation of a pinned, local BGE model; retrieval never downloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen
import zipfile

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
RELEASE = "retrieval-model-20261117-v1"
ASSET = "taxpearls-bge-small-zh-v1.5-fp32-20261117-v1.zip"
MODEL_URL = f"https://github.com/user-wangjun/TaxPearls/releases/download/{RELEASE}/{ASSET}"
ARCHIVE_SHA256 = "be368f9ba0541f1c055dfbfc38a89c560ff34cfcd9384dc63bd6be46b9efa4ba"
MANIFEST_SHA256 = "78d0ddf521869e06ca500eb5b7ffa6e55970c60b475ddff0389f0391a58204cd"
LICENSE_SHA256 = "587a673933425dbc36ec61268d3b954051b2d3ef3c9b322ede357976055ffdd5"
PACKAGE_FILES = frozenset({"manifest.json", "config.json", "tokenizer.json", "tokenizer_config.json",
                           "UPSTREAM_README.md", "LICENSE", "onnx/model_quantized.onnx"})
MAX_PACKAGE_BYTES = 128 * 1024 * 1024


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_install(directory):
    """Verify the pinned manifest, calibration and every model payload without loading ONNX."""
    from src.kg_retrieval.semantic_search import score_calibration
    directory = Path(directory)
    if checksum(directory / "manifest.json") != MANIFEST_SHA256:
        raise ValueError("模型 manifest 与固定版本不一致。")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if set(manifest["files"]) != PACKAGE_FILES - {"manifest.json", "LICENSE"}:
        raise ValueError("模型文件清单与固定版本不一致。")
    if score_calibration(manifest) == (1., 0.):
        raise ValueError("微调模型缺少分数校准配置。")
    for name, expected in {**manifest["files"], "LICENSE": LICENSE_SHA256}.items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or checksum(path) != expected:
            raise ValueError(f"模型文件校验失败：{name}")
    return manifest


def prepare(destination, archive=None):
    """Install the released fine-tuned model, or verify an existing identical installation."""
    destination = Path(destination).resolve()
    if destination.exists():
        try:
            manifest = verify_install(destination)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FileExistsError("目标目录已有其他或不完整的模型，不覆盖；请用 --destination 指定新目录，"
                                  "并将 TAXPEARLS_KG_MODEL_DIR 设置为该目录。") from exc
        print(f"verified {RELEASE}: {destination}", flush=True)
        return manifest
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Staging beside the destination keeps installation on one filesystem.
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".taxpearls-model-") as temporary:
        temporary = Path(temporary)
        source = Path(archive) if archive is not None else temporary / ASSET
        if archive is None:
            print(f"downloading {MODEL_URL}", flush=True)
            with urlopen(MODEL_URL, timeout=60) as response, source.open("xb") as stream:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_PACKAGE_BYTES:
                        raise ValueError("模型下载超出预期大小。")
                    stream.write(chunk)
        if checksum(source) != ARCHIVE_SHA256:
            raise ValueError("模型压缩包 SHA-256 不匹配，未安装。")
        staged = temporary / "model"
        staged.mkdir()
        with zipfile.ZipFile(source) as package:
            entries = package.infolist()
            if (len(entries) != len(PACKAGE_FILES) or {item.filename for item in entries} != PACKAGE_FILES
                    or sum(item.file_size for item in entries) > MAX_PACKAGE_BYTES):
                raise ValueError("模型压缩包文件清单或大小不符合固定版本。")
            # Only fixed names are written; never extract arbitrary archive paths.
            for name in sorted(PACKAGE_FILES):
                path = staged / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(package.read(name))
        manifest = verify_install(staged)
        if destination.exists():
            raise FileExistsError("安装期间目标目录被创建，未覆盖。")
        staged.rename(destination)
    print(f"installed {RELEASE}: {destination}", flush=True)
    return manifest


def prepare_upstream(destination):
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
    parser.add_argument("--variant", choices=("taxpearls", "upstream"), default="taxpearls",
                        help="taxpearls: 固定微调版本（默认）；upstream: 原版 BGE 对照模型")
    parser.add_argument("--archive", type=Path, help="离线安装已下载的微调模型 ZIP，仍校验固定 SHA-256")
    args = parser.parse_args()
    if args.variant == "upstream":
        if args.archive is not None:
            parser.error("--archive 仅用于 taxpearls 微调版本")
        prepare_upstream(args.destination)
    else:
        prepare(args.destination, archive=args.archive)
    print(str(args.destination.resolve()))


if __name__ == "__main__":
    main()
