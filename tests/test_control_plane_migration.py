#!/usr/bin/env python3

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCHEMA = ROOT_DIR / "db" / "schema.sql"
MIGRATION = ROOT_DIR / "scripts" / "migrate_control_plane_model.py"


class ControlPlaneMigrationTests(unittest.TestCase):
    def make_v3_database(self, root):
        db_file = root / "manager.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA.read_text(encoding="utf-8"))
            conn.execute("DELETE FROM schema_migrations WHERE version > 3")
            conn.executescript(
                """
                ALTER TABLE local_credentials RENAME TO local_credentials_v4;
                CREATE TABLE local_credentials (
                    user_id INTEGER PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    password_changed_at TEXT NOT NULL,
                    failed_login_count INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                DROP TABLE local_credentials_v4;

                ALTER TABLE user_sessions RENAME TO user_sessions_v4;
                CREATE TABLE user_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    csrf_token TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                DROP TABLE user_sessions_v4;

                ALTER TABLE operation_records RENAME TO operation_records_v4;
                CREATE TABLE operation_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT,
                    actor_user_id INTEGER,
                    action TEXT NOT NULL,
                    user_id TEXT,
                    instance_id INTEGER,
                    status TEXT NOT NULL
                        CHECK (status IN ('success', 'failed', 'skipped', 'running')),
                    message TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT,
                    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE SET NULL
                );
                DROP TABLE operation_records_v4;
                DROP TABLE IF EXISTS instance_members;
                DROP TABLE IF EXISTS execution_jobs;
                """
            )
            conn.execute(
                """
                INSERT INTO users (
                    public_id, username, normalized_username, role, status,
                    provisioning_source
                ) VALUES ('user-public-id', 'alice', 'alice', 'admin', 'active', 'local')
                """
            )
            user_id = conn.execute("SELECT id FROM users").fetchone()[0]
            conn.execute(
                """
                INSERT INTO local_credentials (
                    user_id, password_hash, password_changed_at
                ) VALUES (?, 'scrypt:test', '2026-01-01T00:00:00+00:00')
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO user_sessions (
                    token_hash, user_id, provider, csrf_token, expires_at,
                    created_at, last_seen_at
                ) VALUES (
                    'token-hash', ?, 'local', 'csrf',
                    '2999-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00'
                )
                """,
                (user_id,),
            )
        return db_file

    def run_migration(self, db_file, *args):
        return subprocess.run(
            [
                "python3",
                str(MIGRATION),
                "--db",
                str(db_file),
                "--schema",
                str(SCHEMA),
                *args,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_does_not_change_v3_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v3_database(Path(temp_dir))

            result = self.run_migration(db_file)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("schema v3 -> v4", result.stdout)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                member_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instance_members'"
                ).fetchone()
            self.assertEqual(version, 3)
            self.assertIsNone(member_table)

    def test_apply_adds_control_plane_model_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v3_database(root)

            result = self.run_migration(db_file, "--apply")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                credential = conn.execute(
                    "SELECT password_hash, must_change_password FROM local_credentials"
                ).fetchone()
                session_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(user_sessions)")
                }
                session_kind = conn.execute(
                    "SELECT session_kind FROM user_sessions WHERE token_hash = 'token-hash'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()

            self.assertEqual(version, 4)
            self.assertEqual(credential, ("scrypt:test", 1))
            self.assertIn("session_kind", session_columns)
            self.assertEqual(session_kind, "user")
            self.assertTrue({"instance_members", "execution_jobs"} <= tables)
            self.assertEqual(violations, [])
            backups = list(root.glob("manager.db.pre-v4-*.bak"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as backup:
                self.assertEqual(
                    backup.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    3,
                )

    def test_failed_schema_validation_leaves_v3_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v3_database(root)
            bad_schema = root / "bad.sql"
            bad_schema.write_text("this is not sql;\n", encoding="utf-8")

            result = subprocess.run(
                [
                    "python3",
                    str(MIGRATION),
                    "--db",
                    str(db_file),
                    "--schema",
                    str(bad_schema),
                    "--apply",
                    "--no-backup",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            self.assertEqual(version, 3)

    def test_failure_inside_migration_rolls_back_schema_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v3_database(root)
            with sqlite3.connect(db_file) as conn:
                conn.execute(
                    "INSERT INTO operation_records (action, status) VALUES ('duplicate', 'success')"
                )
                conn.execute(
                    "INSERT INTO operation_records (action, status) VALUES ('duplicate', 'failed')"
                )
            conflicting_schema = root / "conflicting.sql"
            conflicting_schema.write_text(
                SCHEMA.read_text(encoding="utf-8")
                + "\nCREATE UNIQUE INDEX force_migration_failure "
                + "ON operation_records(action);\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "python3",
                    str(MIGRATION),
                    "--db",
                    str(db_file),
                    "--schema",
                    str(conflicting_schema),
                    "--apply",
                    "--no-backup",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                credential_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(local_credentials)")
                }
                member_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instance_members'"
                ).fetchone()
            self.assertEqual(version, 3)
            self.assertNotIn("must_change_password", credential_columns)
            self.assertIsNone(member_table)


if __name__ == "__main__":
    unittest.main()
