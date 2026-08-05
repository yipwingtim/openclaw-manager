import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCHEMA = ROOT_DIR / "db" / "schema.sql"
MIGRATION = ROOT_DIR / "scripts" / "migrate_external_session_tokens.py"


class ExternalSessionTokensMigrationTests(unittest.TestCase):
    def make_v5_database(self, root):
        db_file = root / "manager.db"
        schema = SCHEMA.read_text(encoding="utf-8")
        start = schema.index("CREATE TABLE IF NOT EXISTS activity_snapshots")
        end = schema.index(
            "INSERT OR IGNORE INTO schema_migrations (version, name)\nVALUES (3",
            start,
        )
        schema = schema[:start] + schema[end:]
        schema = schema.replace(
            """
CREATE TABLE IF NOT EXISTS external_session_tokens (
    external_token_hash TEXT NOT NULL,
    session_token_hash TEXT NOT NULL UNIQUE,
    PRIMARY KEY (external_token_hash, session_token_hash),
    FOREIGN KEY (session_token_hash) REFERENCES user_sessions(token_hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_external_session_tokens_external_token
    ON external_session_tokens(external_token_hash);
""",
            "",
        ).replace(
            """
INSERT OR IGNORE INTO schema_migrations (version, name)
VALUES (6, 'external_session_tokens');
""",
            "",
        ).replace(
            """
INSERT OR IGNORE INTO schema_migrations (version, name)
VALUES (7, 'activity_snapshots');
""",
            "",
        )
        with sqlite3.connect(db_file) as conn:
            conn.executescript(schema)
        return db_file

    def run_migration(self, db_file, *args):
        return subprocess.run(
            ["python3", str(MIGRATION), "--db", str(db_file), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_does_not_change_v5_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v5_database(Path(temp_dir))

            result = self.run_migration(db_file)

            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='external_session_tokens'"
                ).fetchone()
            self.assertEqual(version, 5)
            self.assertIsNone(table)

    def test_apply_creates_table_backup_and_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v5_database(root)

            result = self.run_migration(db_file, "--apply")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(external_session_tokens)"
                    )
                }
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(version, 6)
            self.assertEqual(
                columns, {"external_token_hash", "session_token_hash"}
            )
            self.assertEqual(violations, [])
            self.assertEqual(len(list(root.glob("manager.db.pre-v6-*.bak"))), 1)

    def test_apply_is_idempotent_at_v6(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v5_database(Path(temp_dir))
            self.assertEqual(self.run_migration(db_file, "--apply").returncode, 0)

            repeated = self.run_migration(db_file, "--apply")

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("already at schema version 6", repeated.stdout)


if __name__ == "__main__":
    unittest.main()
