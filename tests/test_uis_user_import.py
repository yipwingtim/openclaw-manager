import csv
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import_uis_users.py"
SCHEMA = ROOT / "db" / "schema.sql"


class UISUserImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "manager.db"
        self.csv_file = root / "uis.csv"
        with sqlite3.connect(self.db) as conn:
            conn.executescript(SCHEMA.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def write_rows(self, rows):
        with self.csv_file.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["work_id", "username", "display_name", "email", "status"])
            writer.writeheader()
            writer.writerows(rows)

    def run_import(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--csv", str(self.csv_file), "--db", str(self.db), *extra],
            text=True, capture_output=True,
        )

    def test_dry_run_does_not_write(self):
        self.write_rows([{"work_id": "123", "username": "alice", "display_name": "Alice", "email": "a@example.test", "status": "active"}])
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_apply_is_idempotent_and_records_audit(self):
        self.write_rows([{"work_id": "123", "username": "alice", "display_name": "Alice", "email": "a@example.test", "status": "active"}])
        self.assertEqual(self.run_import("--apply").returncode, 0)
        self.assertEqual(self.run_import("--apply").returncode, 0)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_identities").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM operation_records WHERE action = 'identity.import_uis'").fetchone()[0], 2)

    def test_duplicate_work_id_fails_without_partial_write(self):
        self.write_rows([
            {"work_id": "123", "username": "alice", "display_name": "", "email": "", "status": "active"},
            {"work_id": "123", "username": "bob", "display_name": "", "email": "", "status": "active"},
        ])
        result = self.run_import("--apply")
        self.assertNotEqual(result.returncode, 0)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
