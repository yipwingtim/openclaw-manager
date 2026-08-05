import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCHEMA = ROOT_DIR / "db" / "schema.sql"
MIGRATION = ROOT_DIR / "scripts" / "migrate_activity_snapshots.py"


class ActivitySnapshotsMigrationTests(unittest.TestCase):
    def make_v6_database(self, root):
        db_file = root / "manager.db"
        schema = SCHEMA.read_text(encoding="utf-8")
        start = schema.index("CREATE TABLE IF NOT EXISTS activity_snapshots")
        end = schema.index("INSERT OR IGNORE INTO schema_migrations (version, name)\nVALUES (3", start)
        schema = schema[:start] + schema[end:]
        schema = schema.replace(
            "\nINSERT OR IGNORE INTO schema_migrations (version, name)\nVALUES (7, 'activity_snapshots');\n",
            "",
        )
        with sqlite3.connect(db_file) as conn:
            conn.executescript(schema)
        return db_file

    def run_migration(self, db_file, *args):
        return subprocess.run(
            ["python3", str(MIGRATION), "--db", str(db_file), *args],
            text=True, capture_output=True, check=False,
        )

    def test_dry_run_and_apply(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v6_database(root)
            dry_run = self.run_migration(db_file)
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            with sqlite3.connect(db_file) as conn:
                self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 6)
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='activity_snapshots'").fetchone())

            applied = self.run_migration(db_file, "--apply")
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            with sqlite3.connect(db_file) as conn:
                self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 7)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(len(list(root.glob("manager.db.pre-v7-*.bak"))), 1)
            self.assertEqual(self.run_migration(db_file, "--apply").returncode, 0)


if __name__ == "__main__":
    unittest.main()
