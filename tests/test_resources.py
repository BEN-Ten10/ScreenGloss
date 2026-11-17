"""Resource import must never turn into a copy of personal installation data."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.import_resources import import_resources
from tools.verify_resources import load_manifest


class ResourceImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.target = root / "source", root / "target"
        self.source.mkdir()
        self.target.mkdir()
        body = b"synthetic resource"
        self.entry = {
            "path": "data/ocr/sample.onnx",
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        resource = self.source / self.entry["path"]
        resource.parent.mkdir(parents=True)
        resource.write_bytes(body)

    def test_only_allowlisted_resource_is_copied(self):
        (self.source / "data/config.json").write_text('{"api_key":"private"}')
        (self.source / "data/cards.db").write_bytes(b"private")
        self.assertEqual(import_resources(self.source, self.target, [self.entry]), 1)
        self.assertEqual(import_resources(self.source, self.target, [self.entry]), 0)
        self.assertFalse((self.target / "data/config.json").exists())
        self.assertFalse((self.target / "data/cards.db").exists())

    def test_bad_source_or_conflicting_target_is_not_overwritten(self):
        target = self.target / self.entry["path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing resource")
        with self.assertRaises(ValueError):
            import_resources(self.source, self.target, [self.entry])
        self.assertEqual(target.read_bytes(), b"existing resource")
        (self.source / self.entry["path"]).write_bytes(b"broken")
        with self.assertRaises(ValueError):
            import_resources(self.source, self.target, [self.entry])
        self.assertEqual(target.read_bytes(), b"existing resource")

    def test_manifest_rejects_path_traversal(self):
        manifest = self.source / "manifest.json"
        for path in (
            "data/../../config.json",
            "C:/private/key",
            "data/ocr/../../key",
            "data\\ocr\\key",
            "data/ocr/x:stream",
        ):
            manifest.write_text(
                json.dumps({"version": 1, "resources": [self.entry | {"path": path}]})
            )
            with self.assertRaises(ValueError):
                load_manifest(manifest)

    def test_manifest_rejects_empty_or_malformed_resources(self):
        manifest = self.source / "manifest.json"
        invalid = [
            [],
            {},
            {"version": 1, "resources": []},
            {"version": True, "resources": [self.entry]},
            {"version": 2, "resources": [self.entry]},
            {"version": 1, "resources": [None]},
            {"version": 1, "resources": [self.entry | {"bytes": True}]},
            {"version": 1, "resources": [self.entry | {"sha256": None}]},
        ]
        for value in invalid:
            with self.subTest(value=value):
                manifest.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_manifest(manifest)

    def test_import_rejects_private_path_before_copying(self):
        entry = self.entry | {"path": "data/config.json"}
        with self.assertRaises(ValueError):
            import_resources(self.source, self.target, [entry])
        self.assertEqual(list(self.target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
