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


def execute_schema(conn, schema_file):
    statement = ""
    for line in schema_file.read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql, statement = statement.strip(), ""
            if sql.upper() not in {"BEGIN;", "COMMIT;"}:
                conn.execute(sql)
    if statement.strip():
        raise RuntimeError("incomplete SQL statement in schema")


def validate_schema(schema_file):
    with sqlite3.connect(":memory:") as conn:
        execute_schema(conn, schema_file)


def backup_database(db_file):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_file.with_name(f"{db_file.name}.pre-v5-{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = db_file.with_name(f"{db_file.name}.pre-v5-{stamp}-{suffix}.bak")
        suffix += 1
    with sqlite3.connect(db_file) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def migrate(conn, schema_file):
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE instances RENAME TO instances_v4")
        conn.execute(
            """
            CREATE TABLE instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                legacy_user_id TEXT UNIQUE,
                owner_user_id INTEGER NOT NULL,
                product TEXT NOT NULL DEFAULT 'openclaw',
                instance_name TEXT NOT NULL,
                runtime_identifier TEXT NOT NULL UNIQUE,
                port INTEGER,
                status TEXT NOT NULL DEFAULT 'active' CHECK (
                    status IN ('provisioning', 'active', 'stopped', 'deleted', 'failed')
                ),
                restore_state TEXT NOT NULL DEFAULT 'not_applicable' CHECK (
                    restore_state IN ('not_applicable', 'restorable', 'incomplete')
                ),
                openclaw_version TEXT,
                basic_auth_enabled INTEGER NOT NULL DEFAULT 1 CHECK (
                    basic_auth_enabled IN (0, 1)
                ),
                container_name TEXT,
                access_url TEXT,
                admin_url TEXT,
                data_path TEXT,
                nginx_conf_path TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                deleted_at TEXT,
                FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE RESTRICT,
                UNIQUE (owner_user_id, product, instance_name)
            )
            """
        )
        columns = [row[1] for row in conn.execute("PRAGMA table_info(instances_v4)")]
        names = ", ".join(columns)
        conn.execute(f"INSERT INTO instances ({names}) SELECT {names} FROM instances_v4")
        conn.execute("DROP TABLE instances_v4")
        execute_schema(conn, schema_file)
        conn.execute("DELETE FROM schema_migrations WHERE version > 5")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate metadata schema v4 to instance provisioning schema v5."
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
        instances = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    if version == 5:
        print("[INFO] already at schema version 5")
        return 0
    if version != 4:
        print(f"[ERROR] schema version 4 is required; found {version}", file=sys.stderr)
        return 1
    print(f"[PLAN] schema v4 -> v5 instances={instances}")
    if not args.apply:
        print("[INFO] Dry-run completed; no database changes were made")
        return 0
    if not args.no_backup:
        print(f"[INFO] Backup created: {backup_database(args.db)}")
    try:
        with sqlite3.connect(args.db, isolation_level=None) as conn:
            migrate(conn, args.schema)
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"[ERROR] migration failed: {exc}", file=sys.stderr)
        return 1
    print("[INFO] Metadata migration to schema version 5 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
