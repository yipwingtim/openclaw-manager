#!/usr/bin/env python3

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def schema_version(conn):
    return int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)


def backup_database(db_file):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_file.with_name(f"{db_file.name}.pre-v7-{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = db_file.with_name(f"{db_file.name}.pre-v7-{stamp}-{suffix}.bak")
        suffix += 1
    with sqlite3.connect(db_file) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def migrate(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""
            CREATE TABLE activity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
                source_version TEXT,
                source_schema TEXT,
                source_cursor TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error_summary TEXT,
                collected_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX idx_activity_snapshots_instance_collected
            ON activity_snapshots(instance_id, collected_at DESC, id DESC);
        """)
        conn.execute("""
            CREATE UNIQUE INDEX idx_activity_snapshots_success_cursor
            ON activity_snapshots(instance_id, source_cursor)
            WHERE status = 'success' AND source_cursor IS NOT NULL
        """)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (7, 'activity_snapshots')"
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description="Migrate metadata schema v6 to activity snapshots schema v7.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("database file must exist")
    with sqlite3.connect(args.db) as conn:
        version = schema_version(conn)
    if version == 7:
        print("[INFO] already at schema version 7")
        return 0
    if version != 6:
        print(f"[ERROR] schema version 6 is required; found {version}", file=sys.stderr)
        return 1
    print("[PLAN] schema v6 -> v7 activity_snapshots")
    if not args.apply:
        print("[INFO] Dry-run completed; no database changes were made")
        return 0
    if not args.no_backup:
        print(f"[INFO] Backup created: {backup_database(args.db)}")
    try:
        with sqlite3.connect(args.db, isolation_level=None) as conn:
            migrate(conn)
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"[ERROR] migration failed: {exc}", file=sys.stderr)
        return 1
    print("[INFO] Metadata migration to schema version 7 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
