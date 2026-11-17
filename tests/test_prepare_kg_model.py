"""Pinned model installation must be reproducible and preserve existing files."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import prepare_kg_model as installer


class ModelInstallationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.destination = self.root / "installed"
        self.payloads = {name: name.encode() for name in installer.PACKAGE_FILES - {"manifest.json"}}
        manifest = {
            "model_id": "synthetic-test-model", "pooling": "cls", "max_tokens": 512,
            "files": {name: self.sha(data) for name, data in self.payloads.items() if name != "LICENSE"},
            "score_calibration": {"version": 1, "scale": .85, "offset": .13,
                                  "weights_sha256": self.sha(self.payloads["onnx/model_quantized.onnx"])},
        }
        self.payloads["manifest.json"] = json.dumps(manifest).encode()
        self.archive = self.root / "model.zip"
        self.write_archive(self.payloads)
        for name, value in (("MANIFEST_SHA256", self.sha(self.payloads["manifest.json"])),
                            ("LICENSE_SHA256", self.sha(self.payloads["LICENSE"])),
                            ("ARCHIVE_SHA256", installer.checksum(self.archive))):
            self.enterContext(patch.object(installer, name, value))

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()

    def write_archive(self, payloads):
        with zipfile.ZipFile(self.archive, "w") as package:
            for name, data in payloads.items():
                package.writestr(name, data)

    def test_online_install_and_repeat_preserve_exact_payloads_and_calibration(self):
        response = io.BytesIO(self.archive.read_bytes())
        with patch.object(installer, "urlopen", return_value=response) as download:
            manifest = installer.prepare(self.destination)
        download.assert_called_once_with(installer.MODEL_URL, timeout=60)
        self.assertEqual(manifest["score_calibration"]["offset"], .13)
        for name, data in self.payloads.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        with patch.object(installer, "urlopen", side_effect=AssertionError("unexpected network")):
            self.assertEqual(installer.prepare(self.destination), manifest)
        self.assertEqual(list(self.root.glob(".taxpearls-model-*")), [])

    def test_offline_install_never_downloads(self):
        with patch.object(installer, "urlopen", side_effect=AssertionError("unexpected network")):
            installer.prepare(self.destination, archive=self.archive)
        self.assertTrue((self.destination / "onnx/model_quantized.onnx").is_file())

    def test_wrong_archive_checksum_never_installs(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            installer.prepare(self.destination, archive=self.archive)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".taxpearls-model-*")), [])

    def test_bad_payload_or_manifest_never_installs_even_if_archive_hash_matches(self):
        for name in ("manifest.json", "onnx/model_quantized.onnx", "LICENSE"):
            with self.subTest(name=name):
                self.write_archive({**self.payloads, name: b"tampered"})
                with patch.object(installer, "ARCHIVE_SHA256", installer.checksum(self.archive)), \
                     self.assertRaises(ValueError):
                    installer.prepare(self.destination, archive=self.archive)
                self.assertFalse(self.destination.exists())

    def test_existing_other_or_damaged_model_is_preserved_without_network(self):
        self.destination.mkdir()
        original = self.destination / "manifest.json"
        original.write_bytes(b"existing model")
        with patch.object(installer, "urlopen", side_effect=AssertionError("unexpected network")), \
             self.assertRaises(FileExistsError):
            installer.prepare(self.destination)
        self.assertEqual(original.read_bytes(), b"existing model")
        self.assertEqual(list(self.destination.iterdir()), [original])

    def test_unexpected_archive_path_and_duplicates_are_rejected(self):
        self.write_archive({**self.payloads, "../escape": b"not a model"})
        with patch.object(installer, "ARCHIVE_SHA256", installer.checksum(self.archive)), \
             self.assertRaisesRegex(ValueError, "清单"):
            installer.prepare(self.destination, archive=self.archive)
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse(self.destination.exists())
        self.write_archive(self.payloads)
        with self.assertWarns(UserWarning), zipfile.ZipFile(self.archive, "a") as package:
            package.writestr("manifest.json", self.payloads["manifest.json"])
        with patch.object(installer, "ARCHIVE_SHA256", installer.checksum(self.archive)), \
             self.assertRaisesRegex(ValueError, "清单"):
            installer.prepare(self.destination, archive=self.archive)
        self.assertFalse(self.destination.exists())

    def test_interrupted_download_leaves_no_partial_install(self):
        with patch.object(installer, "urlopen", side_effect=OSError("network unavailable")), \
             self.assertRaises(OSError):
            installer.prepare(self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".taxpearls-model-*")), [])

    def test_cli_defaults_to_finetuned_model_and_retains_explicit_upstream_option(self):
        with patch("sys.argv", ["prepare_kg_model.py", "--destination", str(self.destination)]), \
             patch.object(installer, "prepare") as selected, patch.object(installer, "prepare_upstream") as original:
            installer.main()
        selected.assert_called_once_with(self.destination, archive=None)
        original.assert_not_called()
        with patch("sys.argv", ["prepare_kg_model.py", "--variant", "upstream",
                                "--destination", str(self.destination)]), \
             patch.object(installer, "prepare") as selected, patch.object(installer, "prepare_upstream") as original:
            installer.main()
        original.assert_called_once_with(self.destination)
        selected.assert_not_called()
