import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "services" / "manager-executor"))
from activity_adapters import get_activity_adapter


class ActivityAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def database(self, path, schema):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executescript(schema)
        return path

    def test_openclaw_collects_only_aggregate_activity(self):
        config = self.root / "config"
        database = self.database(config / "state" / "openclaw.sqlite", """
            CREATE TABLE schema_meta (role TEXT, schema_version INTEGER);
            INSERT INTO schema_meta VALUES ('global', 1);
            CREATE TABLE task_runs (status TEXT, created_at INTEGER, ended_at INTEGER, task TEXT);
            INSERT INTO task_runs VALUES ('succeeded', 1000, 2000, 'secret task');
            CREATE TABLE subagent_runs (ended_reason TEXT, created_at INTEGER, ended_at INTEGER, task TEXT);
            INSERT INTO subagent_runs VALUES ('complete', 2000, 3000, 'secret');
            CREATE TABLE cron_run_logs (status TEXT, run_at_ms INTEGER, total_tokens INTEGER, summary TEXT);
            INSERT INTO cron_run_logs VALUES ('ok', 4000, 12, 'secret');
        """)
        session_dir = config / "agents" / "main" / "sessions"
        session_dir.mkdir(parents=True)
        (session_dir / "one.jsonl").write_text("\n".join([
            json.dumps({"type": "message", "timestamp": "2026-08-05T00:00:05Z", "message": {"role": "user", "content": "secret"}}),
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "secret"}}),
            json.dumps({"type": "message", "message": {"role": "toolResult", "content": "secret"}}),
        ]), encoding="utf-8")
        result = get_activity_adapter("openclaw").collect({
            "data_path": str(self.root), "version": "2026.6.6",
        })
        self.assertEqual(result["metrics"], {
            "last_activity_at_ms": 1785888005000, "model_responses": 1, "scheduled_runs": 1,
            "scheduled_tokens": 12, "sessions": 1, "subagent_runs": 1,
            "task_runs": 1, "tool_calls": 1, "user_interactions": 1,
        })
        self.assertNotIn("secret", repr(result))

    def test_hermes_collects_structured_counts(self):
        self.database(self.root / "state.db", """
            CREATE TABLE schema_version (version INTEGER); INSERT INTO schema_version VALUES (23);
            CREATE TABLE sessions (started_at REAL, ended_at REAL, message_count INTEGER, tool_call_count INTEGER, api_call_count INTEGER, system_prompt TEXT);
            INSERT INTO sessions VALUES (10, 20, 4, 1, 2, 'secret');
        """)
        self.database(self.root / "kanban.db", """
            CREATE TABLE task_runs (status TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT);
            INSERT INTO task_runs VALUES ('done', 10, 30, 'secret');
        """)
        self.database(self.root / "cron" / "executions.db", """
            CREATE TABLE executions (status TEXT, started_at TEXT, finished_at TEXT, error TEXT);
            INSERT INTO executions VALUES ('ok', '1970-01-01T00:00:40+00:00', '1970-01-01T00:00:50+00:00', 'secret');
        """)
        result = get_activity_adapter("hermes").collect({
            "data_path": str(self.root), "version": "v2026.7.30",
        })
        self.assertEqual(result["metrics"], {
            "last_activity_at_s": 50.0, "messages": 4, "model_calls": 2,
            "scheduled_runs": 1, "sessions": 1, "task_runs": 1, "tool_calls": 1,
        })

    def test_hermes_v2026_7_20_accepts_state_schema_22(self):
        self.database(self.root / "state.db", """
            CREATE TABLE schema_version (version INTEGER); INSERT INTO schema_version VALUES (22);
            CREATE TABLE sessions (started_at REAL, ended_at REAL, message_count INTEGER, tool_call_count INTEGER, api_call_count INTEGER);
        """)
        self.database(self.root / "kanban.db", """
            CREATE TABLE task_runs (status TEXT, started_at INTEGER, ended_at INTEGER);
        """)
        self.database(self.root / "cron" / "executions.db", """
            CREATE TABLE executions (status TEXT, started_at TEXT, finished_at TEXT);
        """)

        result = get_activity_adapter("hermes").collect({
            "data_path": str(self.root), "version": "v2026.7.20",
        })

        self.assertEqual(result["source_schema"], "hermes-state-22")

    def test_hermes_version_and_state_schema_must_match(self):
        self.database(self.root / "state.db", """
            CREATE TABLE schema_version (version INTEGER); INSERT INTO schema_version VALUES (23);
        """)

        with self.assertRaisesRegex(ValueError, "unsupported Hermes state schema"):
            get_activity_adapter("hermes").collect({
                "data_path": str(self.root), "version": "v2026.7.20",
            })

    def test_evoscientist_never_reads_blob_payloads(self):
        database = self.database(self.root / "evoscientist-data" / "sessions.db", """
            CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB);
            CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB);
            INSERT INTO checkpoints VALUES ('thread', '', 'one', NULL, 'json', X'00', X'00');
            INSERT INTO writes VALUES ('thread', '', 'one', 'task', 0, 'secret', 'json', X'00');
        """)
        os.utime(database, ns=(1_000_000_000, 2_000_000_000))
        result = get_activity_adapter("evoscientist").collect({
            "data_path": str(self.root),
            "version": "sha256:ca1fd303d7ca2d1bfad97d9872b4ee910eea67c46047be1bf59463941fff3c47",
        })
        self.assertEqual(result["metrics"], {
            "checkpoints": 1, "last_activity_at_ms": 2000,
            "sessions": 1, "write_tasks": 1, "writes": 1,
        })

    def test_schema_mismatch_fails_closed(self):
        self.database(self.root / "state.db", """
            CREATE TABLE schema_version (version INTEGER); INSERT INTO schema_version VALUES (24);
        """)
        with self.assertRaisesRegex(ValueError, "unsupported Hermes state schema"):
            get_activity_adapter("hermes").collect({
                "data_path": str(self.root), "version": "v2026.7.30",
            })

    def test_unobserved_product_version_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported OpenClaw version"):
            get_activity_adapter("openclaw").collect({
                "data_path": str(self.root), "version": "latest",
            })


if __name__ == "__main__":
    unittest.main()
