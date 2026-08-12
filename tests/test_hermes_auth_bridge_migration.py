import importlib.util
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "migrate_hermes_auth_bridge.py"
spec = importlib.util.spec_from_file_location("migrate_hermes_auth_bridge", SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def make_v7_database(self, root):
        db_file = root / "manager.db"
        schema = (ROOT / "db" / "schema.sql").read_text()
        start = schema.index("CREATE TABLE IF NOT EXISTS hermes_auth_clients")
        end = schema.index(
            "INSERT OR IGNORE INTO schema_migrations (version, name)\nVALUES (3",
            start,
        )
        schema = schema[:start] + schema[end:]
        schema = schema.replace(
            "\nINSERT OR IGNORE INTO schema_migrations (version, name)\n"
            "VALUES (8, 'hermes_auth_bridge');\n",
            "",
        )
        with sqlite3.connect(db_file) as conn:
            conn.executescript(schema)
        return db_file

    def run_migration(self, db_file, *args):
        return subprocess.run(
            ["python3", str(SCRIPT), "--db", str(db_file), *args],
            capture_output=True, text=True, check=False,
        )

    def test_apply_creates_v8_schema_and_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v7_database(root)
            result = self.run_migration(db_file, "--apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(db_file) as conn:
                self.assertEqual(migration.schema_version(conn), 8)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(len(list(root.glob("manager.db.pre-v8-*.bak"))), 1)

    def test_migration_rolls_back_all_statements_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v7_database(Path(temp_dir))
            with sqlite3.connect(db_file) as conn:
                conn.execute("CREATE TABLE hermes_auth_grants(blocker INTEGER)")
            with sqlite3.connect(db_file, isolation_level=None) as conn:
                with self.assertRaises(sqlite3.Error):
                    migration.migrate(conn)
                self.assertEqual(migration.schema_version(conn), 7)
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='hermes_auth_clients'"
                ).fetchone())

    def test_dry_run_and_already_current_are_noops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v7_database(Path(temp_dir))
            self.assertEqual(self.run_migration(db_file).returncode, 0)
            self.assertEqual(self.run_migration(db_file, "--apply").returncode, 0)
            self.assertIn("already at schema version 8", self.run_migration(db_file, "--apply").stdout)


if __name__ == "__main__":
    unittest.main()
