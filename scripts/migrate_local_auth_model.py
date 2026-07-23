#!/usr/bin/env python3

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def schema_version(conn):
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def main():
    parser = argparse.ArgumentParser(description="Migrate metadata schema v2 to local-auth schema v3.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=Path(__file__).resolve().parents[1] / "db" / "schema.sql")
    parser.add_argument("--admins", default=os.environ.get("MANAGER_ADMIN_USERS", "openclaw"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if not args.db.is_file() or not args.schema.is_file():
        parser.error("database and schema files must exist")

    admins = [value.strip().casefold() for value in args.admins.split(",") if value.strip()]
    with sqlite3.connect(args.db) as conn:
        version = schema_version(conn)
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        matched_admins = conn.execute(
            f"SELECT COUNT(*) FROM users WHERE normalized_username IN ({','.join('?' for _ in admins)})",
            admins,
        ).fetchone()[0] if admins else 0

    if version not in {2, 3}:
        print(f"[ERROR] schema version 2 or 3 is required; found {version}")
        return 1

    print(f"[PLAN] users={users} admins={matched_admins} provider=nginx-basic")
    if not args.apply:
        print("[INFO] Dry-run completed; no database changes were made")
        return 0

    if not args.no_backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = args.db.with_name(f"{args.db.name}.pre-v3-{stamp}.bak")
        shutil.copy2(args.db, backup)
        print(f"[INFO] Backup created: {backup}")

    schema = args.schema.read_text(encoding="utf-8")
    with sqlite3.connect(args.db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "role" not in columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user'))"
            )
        if admins:
            conn.execute(
                f"UPDATE users SET role = 'admin' WHERE normalized_username IN ({','.join('?' for _ in admins)})",
                admins,
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_identities (
                user_id, provider, subject, external_username, created_at, updated_at
            )
            SELECT user_id, 'nginx-basic', subject, external_username,
                   COALESCE(created_at, datetime('now')), COALESCE(updated_at, datetime('now'))
            FROM user_identities
            WHERE provider = 'legacy'
            """
        )
        conn.executescript(schema)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name) VALUES (3, 'local_auth_session')"
        )
        conn.commit()
    print("[INFO] Metadata migration to schema version 3 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
