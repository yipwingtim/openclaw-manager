#!/usr/bin/env python3

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT_DIR / "db" / "schema.sql"


def schema_version(conn):
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def execute_schema(conn, schema_file):
    statement = ""
    for line in schema_file.read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise RuntimeError("incomplete SQL statement in schema")


def validate_schema(schema_file):
    with sqlite3.connect(":memory:") as conn:
        execute_schema(conn, schema_file)


def backup_database(db_file):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_file.with_name(f"{db_file.name}.pre-v4-{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = db_file.with_name(f"{db_file.name}.pre-v4-{stamp}-{suffix}.bak")
        suffix += 1
    with sqlite3.connect(db_file) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def migrate(conn, schema_file):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        local_columns = table_columns(conn, "local_credentials")
        if "must_change_password" not in local_columns:
            conn.execute(
                """
                ALTER TABLE local_credentials
                ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 1
                    CHECK (must_change_password IN (0, 1))
                """
            )

        session_columns = table_columns(conn, "user_sessions")
        if "session_kind" not in session_columns:
            conn.execute(
                """
                ALTER TABLE user_sessions
                ADD COLUMN session_kind TEXT NOT NULL DEFAULT 'user'
                    CHECK (session_kind IN ('user', 'admin', 'emergency'))
                """
            )

        operation_columns = table_columns(conn, "operation_records")
        if "request_id" not in operation_columns:
            conn.execute("ALTER TABLE operation_records ADD COLUMN request_id TEXT")
        if "source_service" not in operation_columns:
            conn.execute("ALTER TABLE operation_records ADD COLUMN source_service TEXT")

        execute_schema(conn, schema_file)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Migrate metadata schema v3 to control-plane schema v4."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if not args.db.is_file() or not args.schema.is_file():
        parser.error("database and schema files must exist")
    try:
        validate_schema(args.schema)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"[ERROR] invalid schema: {exc}", file=sys.stderr)
        return 1

    with sqlite3.connect(args.db) as conn:
        version = schema_version(conn)
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        instances = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    if version == 4:
        print("[INFO] already at schema version 4")
        return 0
    if version != 3:
        print(f"[ERROR] schema version 3 is required; found {version}", file=sys.stderr)
        return 1

    print(f"[PLAN] schema v3 -> v4 users={users} instances={instances}")
    if not args.apply:
        print("[INFO] Dry-run completed; no database changes were made")
        return 0

    if not args.no_backup:
        backup = backup_database(args.db)
        print(f"[INFO] Backup created: {backup}")

    try:
        with sqlite3.connect(args.db, isolation_level=None) as conn:
            migrate(conn, args.schema)
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"[ERROR] migration failed: {exc}", file=sys.stderr)
        return 1

    print("[INFO] Metadata migration to schema version 4 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
