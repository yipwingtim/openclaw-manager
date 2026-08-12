#!/usr/bin/env python3

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


STATEMENTS = (
    """
    CREATE TABLE hermes_auth_clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id INTEGER NOT NULL UNIQUE,
        client_id TEXT NOT NULL UNIQUE,
        client_secret_hash TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        rotated_at INTEGER,
        revoked_at INTEGER,
        FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE,
        UNIQUE (id, instance_id)
    )
    """,
    """
    CREATE TABLE hermes_auth_grants (
        code_hash TEXT PRIMARY KEY,
        client_id INTEGER NOT NULL,
        instance_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        manager_session_id TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        code_challenge TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER,
        FOREIGN KEY (client_id, instance_id)
            REFERENCES hermes_auth_clients(id, instance_id) ON DELETE CASCADE,
        FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (manager_session_id) REFERENCES user_sessions(token_hash) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_hermes_auth_grants_expires ON hermes_auth_grants(expires_at)",
    "INSERT INTO schema_migrations(version, name) VALUES(8, 'hermes_auth_bridge')",
)


def schema_version(conn):
    return int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)


def backup_database(db_file):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_file.with_name(f"{db_file.name}.pre-v8-{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = db_file.with_name(f"{db_file.name}.pre-v8-{stamp}-{suffix}.bak")
        suffix += 1
    with sqlite3.connect(db_file) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def migrate(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in STATEMENTS:
            conn.execute(statement)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Migrate metadata schema v7 to Hermes auth bridge schema v8."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("database file must exist")
    with sqlite3.connect(args.db) as conn:
        version = schema_version(conn)
    if version == 8:
        print("[INFO] already at schema version 8")
        return 0
    if version != 7:
        print(f"[ERROR] schema version 7 is required; found {version}", file=sys.stderr)
        return 1
    print("[PLAN] schema v7 -> v8 hermes_auth_bridge")
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
    print("[INFO] Metadata migration to schema version 8 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
