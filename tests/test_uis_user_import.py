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
            writer = csv.DictWriter(handle, fieldnames=["user_id", "name", "email", "status"])
            writer.writeheader()
            writer.writerows(rows)

    def run_import(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--csv", str(self.csv_file), "--db", str(self.db), *extra],
            text=True, capture_output=True,
        )

    def test_dry_run_does_not_write(self):
        self.write_rows([{"user_id": "123", "name": "Alice", "email": "a@example.test", "status": "active"}])
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_apply_is_idempotent_and_records_audit(self):
        self.write_rows([{"user_id": "123", "name": "Alice", "email": "a@example.test", "status": "active"}])
        self.assertEqual(self.run_import("--apply").returncode, 0)
        self.assertEqual(self.run_import("--apply").returncode, 0)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_identities").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM operation_records WHERE action = 'identity.import_uis'").fetchone()[0], 2)
            user = conn.execute("SELECT username, display_name, email, status, provisioning_source FROM users").fetchone()
            self.assertEqual(user, ("uis_a665a45920422f9d", "Alice", "a@example.test", "active", "uis-import"))

    def test_duplicate_user_id_fails_without_partial_write(self):
        self.write_rows([
            {"user_id": "123", "name": "Alice", "email": "", "status": "active"},
            {"user_id": "123", "name": "Bob", "email": "", "status": "active"},
        ])
        result = self.run_import("--apply")
        self.assertNotEqual(result.returncode, 0)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_existing_binding_updates_profile_without_replacing_local_username_or_status(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO users (public_id, username, normalized_username, display_name, email, status) VALUES (?, ?, ?, ?, ?, ?)",
                ("user-1", "alice", "alice", "Old Name", "old@example.test", "disabled"),
            )
            user_id = conn.execute("SELECT id FROM users WHERE public_id = 'user-1'").fetchone()[0]
            conn.execute(
                "INSERT INTO user_identities (user_id, provider, subject) VALUES (?, 'campus-uis', '123')",
                (user_id,),
            )
        self.write_rows([{"user_id": "123", "name": "New Name", "email": "new@example.test", "status": ""}])

        self.assertEqual(self.run_import("--apply").returncode, 0)
        with sqlite3.connect(self.db) as conn:
            user = conn.execute(
                "SELECT username, display_name, email, status FROM users WHERE public_id = 'user-1'"
            ).fetchone()
            self.assertEqual(user, ("alice", "New Name", "new@example.test", "disabled"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_import_does_not_merge_an_unbound_local_user(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO users (public_id, username, normalized_username, display_name) VALUES (?, ?, ?, ?)",
                ("local-1", "alice", "alice", "Local Alice"),
            )
        self.write_rows([{"user_id": "123", "name": "Alice", "email": "", "status": "active"}])

        self.assertEqual(self.run_import("--apply").returncode, 0)
        with sqlite3.connect(self.db) as conn:
            users = conn.execute(
                "SELECT username, display_name FROM users ORDER BY username"
            ).fetchall()
            self.assertEqual(users, [("alice", "Local Alice"), ("uis_a665a45920422f9d", "Alice")])

    def test_generated_username_collision_fails_without_partial_write(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO users (public_id, username, normalized_username) VALUES (?, ?, ?)",
                ("collision", "uis_a665a45920422f9d", "uis_a665a45920422f9d"),
            )
        self.write_rows([
            {"user_id": "123", "name": "Alice", "email": "", "status": "active"},
        ])

        result = self.run_import("--apply")

        self.assertNotEqual(result.returncode, 0)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_identities").fetchone()[0], 0)

    def test_invalid_row_rolls_back_the_whole_import(self):
        self.write_rows([
            {"user_id": "123", "name": "Alice", "email": "a@example.test", "status": "active"},
            {"user_id": "456", "name": "Bob", "email": "not-an-email", "status": "active"},
        ])

        result = self.run_import("--apply")
        self.assertNotEqual(result.returncode, 0)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
