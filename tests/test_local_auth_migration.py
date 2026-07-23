#!/usr/bin/env python3

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
MIGRATION = ROOT_DIR / "scripts" / "migrate_local_auth_model.py"
SCHEMA_V1 = ROOT_DIR / "tests" / "fixtures" / "metadata_schema_v1.sql"
V2_MIGRATION = ROOT_DIR / "scripts" / "migrate_identity_instance_model.py"


class LocalAuthMigrationTests(unittest.TestCase):
    def test_v2_users_are_migrated_without_creating_local_passwords(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "manager.db"
            public_dir = Path(tmp) / "public"
            public_dir.mkdir()
            with sqlite3.connect(db_file) as conn:
                conn.executescript(SCHEMA_V1.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO instances (user_id, status) VALUES ('openclaw', 'active')"
                )
            subprocess.run(
                ["python3", str(V2_MIGRATION), "--db", str(db_file), "--public-dir", str(public_dir), "--apply", "--no-backup"],
                check=True,
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                ["python3", str(MIGRATION), "--db", str(db_file), "--admins", "openclaw", "--apply", "--no-backup"],
                check=True,
                capture_output=True,
                text=True,
            )

            with sqlite3.connect(db_file) as conn:
                role = conn.execute("SELECT role FROM users WHERE username = 'openclaw'").fetchone()[0]
                provider = conn.execute("SELECT provider FROM user_identities WHERE provider = 'nginx-basic'").fetchone()[0]
                credentials = conn.execute("SELECT COUNT(*) FROM local_credentials").fetchone()[0]
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

            self.assertIn("schema version 3 completed", result.stdout)
            self.assertEqual((role, provider, credentials, version), ("admin", "nginx-basic", 0, 3))


if __name__ == "__main__":
    unittest.main()
