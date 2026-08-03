#!/usr/bin/env python3

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def schema_version(conn):
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def backup_database(db_file):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_file.with_name(f"{db_file.name}.pre-v6-{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = db_file.with_name(f"{db_file.name}.pre-v6-{stamp}-{suffix}.bak")
        suffix += 1
    with sqlite3.connect(db_file) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def migrate(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_session_tokens (
                external_token_hash TEXT NOT NULL,
                session_token_hash TEXT NOT NULL UNIQUE,
                PRIMARY KEY (external_token_hash, session_token_hash),
                FOREIGN KEY (session_token_hash)
                    REFERENCES user_sessions(token_hash) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_external_session_tokens_external_token
            ON external_session_tokens(external_token_hash)
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (6, ?)",
            ("external_session_tokens",),
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Migrate metadata schema v5 to external session tokens schema v6."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("database file must exist")
    with sqlite3.connect(args.db) as conn:
        version = schema_version(conn)
    if version == 6:
        print("[INFO] already at schema version 6")
        return 0
    if version != 5:
        print(f"[ERROR] schema version 5 is required; found {version}", file=sys.stderr)
        return 1
    print("[PLAN] schema v5 -> v6 external_session_tokens")
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
    print("[INFO] Metadata migration to schema version 6 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
