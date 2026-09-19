#!/usr/bin/env python3
"""Copy a complete Manager metadata database from SQLite to empty PostgreSQL."""

import argparse
import os
import sqlite3
import sys


TABLES = (
    "users", "user_identities", "local_credentials", "user_sessions",
    "external_session_tokens", "auth_settings", "instances",
    "instance_members", "instance_credentials", "instance_endpoints", "ports",
    "operation_records", "execution_jobs", "activity_snapshots",
    "hermes_auth_clients", "hermes_auth_grants",
)
IDENTITY_TABLES = (
    "users", "user_identities", "instances", "instance_members",
    "instance_credentials", "instance_endpoints", "operation_records",
    "execution_jobs", "activity_snapshots", "hermes_auth_clients",
)


def source_rows(source, table):
    columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
    if not columns:
        raise RuntimeError(f"source table is missing: {table}")
    rows = [tuple(row) for row in source.execute(f"SELECT * FROM {table}")]
    if table == "execution_jobs":
        parent = columns.index("parent_request_id")
        request = columns.index("request_id")
        pending, ordered, available = rows[:], [], {None}
        while pending:
            ready = [row for row in pending if row[parent] in available]
            if not ready:
                raise RuntimeError("execution_jobs contains an unresolved parent chain")
            for row in ready:
                pending.remove(row)
                ordered.append(row)
                available.add(row[request])
        rows = ordered
    return columns, rows


def source_inventory(source):
    violations = source.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"source foreign key violations: {violations[:5]}")
    version = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if version != 8:
        raise RuntimeError(f"source schema version must be 8, got {version}")
    return {table: len(source_rows(source, table)[1]) for table in TABLES}


def ensure_empty(target):
    version = target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if version != 8:
        raise RuntimeError(f"target schema version must be 8, got {version}")
    counts = {
        table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    }
    occupied = {table: count for table, count in counts.items() if count}
    if occupied:
        raise RuntimeError(f"target PostgreSQL business tables are not empty: {occupied}")


def import_data(source, target, expected):
    target.execute("LOCK TABLE " + ", ".join(TABLES) + " IN EXCLUSIVE MODE")
    ensure_empty(target)
    for table in TABLES:
        columns, rows = source_rows(source, table)
        if rows:
            placeholders = ",".join(["%s"] * len(columns))
            target.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                rows,
            )
    actual = {
        table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    }
    if actual != expected:
        raise RuntimeError(f"row count mismatch: expected={expected} actual={actual}")
    for table in IDENTITY_TABLES:
        target.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
            f"EXISTS (SELECT 1 FROM {table}))",
            (table,),
        )


def connect_postgres(url):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required") from exc
    return psycopg.connect(url)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--postgres-url", default=os.environ.get("METADATA_DATABASE_URL", ""))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.postgres_url:
        parser.error("--postgres-url or METADATA_DATABASE_URL is required")

    try:
        source = sqlite3.connect(f"file:{os.path.abspath(args.sqlite)}?mode=ro", uri=True)
        expected = source_inventory(source)
        with connect_postgres(args.postgres_url) as target:
            ensure_empty(target)
            print("[PLAN] " + " ".join(f"{table}={expected[table]}" for table in TABLES))
            if not args.apply:
                print("[INFO] Dry-run completed; no data was changed")
                return 0
            import_data(source, target, expected)
        print("[INFO] SQLite metadata imported into PostgreSQL")
        return 0
    except Exception as exc:
        print(f"[ERROR] migration failed; PostgreSQL transaction rolled back: {exc}", file=sys.stderr)
        return 1
    finally:
        if "source" in locals():
            source.close()


if __name__ == "__main__":
    raise SystemExit(main())
