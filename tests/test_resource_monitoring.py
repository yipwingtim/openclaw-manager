#!/usr/bin/env python3

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "activity_adapters_resource_test", ROOT / "services/manager-executor/activity_adapters.py"
)
STORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STORE)


class ResourceMonitoringTests(unittest.TestCase):
    def test_collects_bytes_and_session_files_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sessions").mkdir()
            (root / "sessions" / "one.jsonl").write_text("abc")
            (root / "config.json").write_text("12345")
            (root / "sessions" / "external.jsonl").symlink_to(root / "config.json")
            usage = STORE.resource_metrics(root)
            self.assertEqual(usage["disk_bytes"], 8)
            self.assertEqual(usage["session_files"], 1)

    def test_reports_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            STORE.resource_metrics("/does/not/exist")


if __name__ == "__main__":
    unittest.main()
