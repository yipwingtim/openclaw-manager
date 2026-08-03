#!/usr/bin/env python3

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCHEMA = ROOT_DIR / "db" / "schema.sql"
MIGRATION = ROOT_DIR / "scripts" / "migrate_instance_provisioning_model.py"
INIT_DB = ROOT_DIR / "scripts" / "init_metadata_db.sh"


class InstanceProvisioningMigrationTests(unittest.TestCase):
    def make_v4_database(self, root):
        db_file = root / "manager.db"
        schema = SCHEMA.read_text(encoding="utf-8")
        schema = schema.replace(
            "('provisioning', 'active', 'stopped', 'deleted', 'failed')",
            "('active', 'stopped', 'deleted', 'failed')",
        )
        schema = schema.replace(
            "\nINSERT OR IGNORE INTO schema_migrations (version, name)\n"
            "VALUES (5, 'instance_provisioning');\n",
            "",
        )
        with sqlite3.connect(db_file) as conn:
            conn.executescript(schema)
            conn.execute("DELETE FROM schema_migrations WHERE version > 4")
            conn.execute(
                """
                INSERT INTO users (
                    public_id, username, normalized_username, status,
                    provisioning_source
                ) VALUES ('owner-public-id', 'owner', 'owner', 'active', 'local')
                """
            )
            owner_id = conn.execute("SELECT id FROM users").fetchone()[0]
            conn.execute(
                """
                INSERT INTO instances (
                    public_id, owner_user_id, product, instance_name,
                    runtime_identifier, status
                ) VALUES (
                    'instance-public-id', ?, 'openclaw', 'Primary',
                    'openclaw_owner', 'active'
                )
                """,
                (owner_id,),
            )
            instance_id = conn.execute("SELECT id FROM instances").fetchone()[0]
            conn.execute(
                """
                INSERT INTO instance_endpoints (
                    instance_id, endpoint_type, status
                ) VALUES (?, 'legacy_port', 'active')
                """,
                (instance_id,),
            )
        return db_file

    def run_migration(self, db_file, *args):
        return subprocess.run(
            [
                "python3", str(MIGRATION), "--db", str(db_file),
                "--schema", str(SCHEMA), *args,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_leaves_v4_database_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v4_database(Path(temp_dir))

            result = self.run_migration(db_file)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("schema v4 -> v5", result.stdout)
            with sqlite3.connect(db_file) as conn:
                self.assertEqual(
                    conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                    4,
                )

    def test_apply_preserves_instances_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v4_database(root)

            result = self.run_migration(db_file, "--apply")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                instance = conn.execute(
                    "SELECT public_id, status FROM instances"
                ).fetchone()
                conn.execute(
                    "UPDATE instances SET status = 'provisioning' "
                    "WHERE public_id = 'instance-public-id'"
                )
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()

            self.assertEqual(version, 5)
            self.assertEqual(instance, ("instance-public-id", "active"))
            self.assertEqual(violations, [])
            self.assertEqual(len(list(root.glob("manager.db.pre-v5-*.bak"))), 1)

    def test_initializer_refuses_to_mark_v4_database_as_v5(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v4_database(Path(temp_dir))
            result = subprocess.run(
                ["bash", str(INIT_DB)],
                env={
                    **os.environ,
                    "METADATA_DB_FILE": str(db_file),
                    "OPENCLAW_PUBLIC_DIR": temp_dir,
                },
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("migrate_instance_provisioning_model.py", result.stderr)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            self.assertEqual(version, 4)

    def test_failure_inside_migration_restores_v4_schema_and_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v4_database(root)
            with sqlite3.connect(db_file) as conn:
                conn.execute(
                    "INSERT INTO operation_records (action, status) "
                    "VALUES ('duplicate', 'success')"
                )
                conn.execute(
                    "INSERT INTO operation_records (action, status) "
                    "VALUES ('duplicate', 'failed')"
                )
            schema = root / "conflicting.sql"
            schema.write_text(
                SCHEMA.read_text(encoding="utf-8")
                + "\nCREATE UNIQUE INDEX force_migration_failure "
                + "ON operation_records(action);\n",
                encoding="utf-8",
            )

            result = self.run_migration(
                db_file, "--schema", str(schema), "--apply", "--no-backup"
            )

            self.assertNotEqual(result.returncode, 0)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                instance = conn.execute(
                    "SELECT public_id, status FROM instances"
                ).fetchone()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE instances SET status = 'provisioning' "
                        "WHERE public_id = 'instance-public-id'"
                    )
            self.assertEqual(version, 4)
            self.assertEqual(instance, ("instance-public-id", "active"))


if __name__ == "__main__":
    unittest.main()
