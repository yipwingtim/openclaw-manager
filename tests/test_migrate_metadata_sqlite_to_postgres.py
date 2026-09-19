import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sqlite_to_postgres", ROOT / "scripts" / "migrate_metadata_sqlite_to_postgres.py"
)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class Result:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class Target:
    def __init__(self, counts=None, fail_on=None, version=8):
        self.counts = {table: 0 for table in MIGRATION.TABLES}
        self.counts.update(counts or {})
        self.fail_on = fail_on
        self.version = version
        self.inserted = []

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("injected failure")
        if sql == "SELECT MAX(version) FROM schema_migrations":
            return Result(self.version)
        if sql.startswith("SELECT COUNT(*) FROM "):
            return Result(self.counts[sql.rsplit(" ", 1)[-1]])
        return Result(None)

    def cursor(self):
        return Cursor(self)


class Cursor:
    def __init__(self, target):
        self.target = target

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def executemany(self, sql, rows):
        table = sql.split()[2]
        if self.target.fail_on and self.target.fail_on in sql:
            raise RuntimeError("injected failure")
        self.target.counts[table] += len(rows)
        self.target.inserted.extend((table, row) for row in rows)


class MigrationTests(unittest.TestCase):
    def test_nonempty_target_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "not empty"):
            MIGRATION.ensure_empty(Target({"users": 1}))

    def test_wrong_target_schema_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "target schema version must be 8"):
            MIGRATION.ensure_empty(Target(version=7))

    def test_execution_jobs_are_parent_first(self):
        with sqlite3.connect(":memory:") as source:
            source.execute("CREATE TABLE execution_jobs(id INTEGER, request_id TEXT, parent_request_id TEXT)")
            source.executemany("INSERT INTO execution_jobs VALUES(?,?,?)", [(2, "child", "parent"), (1, "parent", None)])
            _, rows = MIGRATION.source_rows(source, "execution_jobs")
        self.assertEqual([row[1] for row in rows], ["parent", "child"])

    def test_import_failure_reaches_caller_for_transaction_rollback(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = sqlite3.connect(Path(temp.name) / "source.db")
        for table in MIGRATION.TABLES:
            source.execute(f"CREATE TABLE {table}(id INTEGER)")
        source.execute("INSERT INTO users VALUES(1)")
        expected = {table: 0 for table in MIGRATION.TABLES}
        expected["users"] = 1
        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            MIGRATION.import_data(source, Target(fail_on="INSERT INTO users"), expected)
        source.close()

    def test_import_copies_rows_and_updates_identity_sequences(self):
        with sqlite3.connect(":memory:") as source:
            source.executescript((ROOT / "db" / "schema.sql").read_text())
            source.execute(
                "INSERT INTO users(id,public_id,username,normalized_username) VALUES(7,?,?,?)",
                ("77777777-7777-7777-7777-777777777777", "alice", "alice"),
            )
            expected = {table: 0 for table in MIGRATION.TABLES}
            expected["users"] = 1
            target = Target()
            MIGRATION.import_data(source, target, expected)
        self.assertTrue(any(table == "users" and row[0] == 7 for table, row in target.inserted))


if __name__ == "__main__":
    unittest.main()
