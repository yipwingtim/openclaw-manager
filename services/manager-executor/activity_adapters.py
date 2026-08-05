import glob
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def readonly_database(path):
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def table_columns(connection, table):
    return {row["name"] for row in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def require_columns(connection, table, expected):
    if not expected <= table_columns(connection, table):
        raise ValueError(f"unsupported {table} schema")


def snapshot_result(metrics, source_version, source_schema, source_mtime_ns):
    canonical = json.dumps(metrics, sort_keys=True, separators=(",", ":"))
    cursor = hashlib.sha256(
        f"{source_version}\n{source_schema}\n{source_mtime_ns}\n{canonical}".encode()
    ).hexdigest()
    return {
        "status": "success",
        "source_version": source_version,
        "source_schema": source_schema,
        "source_cursor": cursor,
        "metrics": metrics,
    }


def timestamp_ms(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value * 1000) if value < 10_000_000_000 else int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


class OpenClawActivityAdapter:
    def collect(self, instance):
        if instance.get("version") != "2026.6.6":
            raise ValueError("unsupported OpenClaw version")
        root = Path(instance["data_path"]) / "config"
        database = root / "state" / "openclaw.sqlite"
        sessions = root / "agents" / "main" / "sessions"
        with readonly_database(database) as connection:
            require_columns(connection, "schema_meta", {"role", "schema_version"})
            schema = connection.execute(
                "SELECT schema_version FROM schema_meta WHERE role = 'global'"
            ).fetchone()
            if schema is None or schema[0] != 1:
                raise ValueError("unsupported OpenClaw schema")
            required = {
                "task_runs": {"status", "created_at", "ended_at"},
                "subagent_runs": {"ended_reason", "created_at", "ended_at"},
                "cron_run_logs": {"status", "run_at_ms", "total_tokens"},
            }
            for table, columns in required.items():
                require_columns(connection, table, columns)
            metrics = {
                "task_runs": connection.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0],
                "subagent_runs": connection.execute("SELECT COUNT(*) FROM subagent_runs").fetchone()[0],
                "scheduled_runs": connection.execute("SELECT COUNT(*) FROM cron_run_logs").fetchone()[0],
                "scheduled_tokens": connection.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) FROM cron_run_logs"
                ).fetchone()[0],
            }
            activity_values = [
                connection.execute("SELECT MAX(COALESCE(ended_at, created_at)) FROM task_runs").fetchone()[0],
                connection.execute("SELECT MAX(COALESCE(ended_at, created_at)) FROM subagent_runs").fetchone()[0],
                connection.execute("SELECT MAX(run_at_ms) FROM cron_run_logs").fetchone()[0],
            ]

        metrics.update({"sessions": 0, "user_interactions": 0, "model_responses": 0, "tool_calls": 0})
        jsonl_mtime = 0
        for path_value in glob.glob(str(sessions / "*.jsonl")):
            if path_value.endswith(".trajectory.jsonl"):
                continue
            path = Path(path_value)
            metrics["sessions"] += 1
            jsonl_mtime = max(jsonl_mtime, path.stat().st_mtime_ns)
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    item = json.loads(line)
                    if not isinstance(item, dict) or item.get("type") != "message":
                        continue
                    message = item.get("message")
                    role = message.get("role") if isinstance(message, dict) else None
                    metric = {
                        "user": "user_interactions",
                        "assistant": "model_responses",
                        "toolResult": "tool_calls",
                    }.get(role)
                    if metric:
                        metrics[metric] += 1
                    activity_values.append(timestamp_ms(
                        item.get("timestamp") or (
                            message.get("timestamp") if isinstance(message, dict) else None
                        )
                    ))
        metrics["last_activity_at_ms"] = max(
            [value for value in activity_values if isinstance(value, (int, float))] or [0]
        )
        version = instance.get("version") or "unknown"
        return snapshot_result(metrics, version, "openclaw-global-1", max(database.stat().st_mtime_ns, jsonl_mtime))


class HermesActivityAdapter:
    def collect(self, instance):
        supported_schemas = {"v2026.7.20": 22, "v2026.7.30": 23}
        expected_schema = supported_schemas.get(instance.get("version"))
        if expected_schema is None:
            raise ValueError("unsupported Hermes version")
        root = Path(instance["data_path"])
        state = root / "state.db"
        kanban = root / "kanban.db"
        cron = root / "cron" / "executions.db"
        with readonly_database(state) as connection:
            require_columns(connection, "schema_version", {"version"})
            observed_schema = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            if observed_schema != expected_schema:
                raise ValueError("unsupported Hermes state schema")
            require_columns(connection, "sessions", {
                "started_at", "ended_at", "message_count", "tool_call_count", "api_call_count",
            })
            row = connection.execute("""
                SELECT COUNT(*) sessions,
                       COALESCE(SUM(message_count), 0) messages,
                       COALESCE(SUM(tool_call_count), 0) tool_calls,
                       COALESCE(SUM(api_call_count), 0) model_calls,
                       COALESCE(MAX(COALESCE(ended_at, started_at)), 0) last_activity
                FROM sessions
            """).fetchone()
            metrics = {
                "sessions": row["sessions"], "messages": row["messages"],
                "tool_calls": row["tool_calls"], "model_calls": row["model_calls"],
                "last_activity_at_s": row["last_activity"],
            }
        with readonly_database(kanban) as connection:
            require_columns(connection, "task_runs", {"status", "started_at", "ended_at"})
            metrics["task_runs"] = connection.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
            task_activity = connection.execute(
                "SELECT MAX(COALESCE(ended_at, started_at)) FROM task_runs"
            ).fetchone()[0]
        with readonly_database(cron) as connection:
            require_columns(connection, "executions", {"status", "started_at", "finished_at"})
            metrics["scheduled_runs"] = connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            cron_activity = connection.execute(
                "SELECT MAX(COALESCE(finished_at, started_at)) FROM executions"
            ).fetchone()[0]
        metrics["last_activity_at_s"] = max(
            metrics["last_activity_at_s"],
            timestamp_ms(task_activity) / 1000,
            timestamp_ms(cron_activity) / 1000,
        )
        return snapshot_result(
            metrics, instance["version"], f"hermes-state-{observed_schema}",
            max(path.stat().st_mtime_ns for path in (state, kanban, cron)),
        )


class EvoScientistActivityAdapter:
    def collect(self, instance):
        if instance.get("version") != "sha256:ca1fd303d7ca2d1bfad97d9872b4ee910eea67c46047be1bf59463941fff3c47":
            raise ValueError("unsupported EvoScientist version")
        database = Path(instance["data_path"]) / "evoscientist-data" / "sessions.db"
        with readonly_database(database) as connection:
            require_columns(connection, "checkpoints", {
                "thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id",
                "type", "checkpoint", "metadata",
            })
            require_columns(connection, "writes", {
                "thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx",
                "channel", "type", "value",
            })
            checkpoints = connection.execute("""
                SELECT COUNT(*) checkpoints, COUNT(DISTINCT thread_id) sessions
                FROM checkpoints
            """).fetchone()
            writes = connection.execute("""
                SELECT COUNT(*) writes, COUNT(DISTINCT task_id) write_tasks FROM writes
            """).fetchone()
        metrics = {
            "sessions": checkpoints["sessions"], "checkpoints": checkpoints["checkpoints"],
            "writes": writes["writes"], "write_tasks": writes["write_tasks"],
            "last_activity_at_ms": database.stat().st_mtime_ns // 1_000_000,
        }
        return snapshot_result(
            metrics, instance.get("version") or "unknown",
            "evoscientist-checkpoints-writes-v1", database.stat().st_mtime_ns,
        )


def get_activity_adapter(product):
    adapter = {
        "openclaw": OpenClawActivityAdapter,
        "hermes": HermesActivityAdapter,
        "evoscientist": EvoScientistActivityAdapter,
    }.get(product)
    if adapter is None:
        raise ValueError("unsupported activity product")
    return adapter()
