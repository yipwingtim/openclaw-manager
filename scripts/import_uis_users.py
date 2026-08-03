#!/usr/bin/env python3

import argparse
import csv
import sqlite3
import sys
import unicodedata
import uuid
from pathlib import Path


REQUIRED = {"work_id", "username"}


def normalize(value):
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def read_rows(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = REQUIRED - fields
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")
        rows = []
        for number, raw in enumerate(reader, 2):
            row = {key: (value or "").strip() for key, value in raw.items()}
            if not row["work_id"] or not row["username"]:
                raise ValueError(f"row {number}: work_id and username are required")
            status = row.get("status") or "active"
            if status not in {"active", "disabled", "locked"}:
                raise ValueError(f"row {number}: invalid status {status!r}")
            row["status"] = status
            rows.append(row)
    return rows


def validate_rows(rows):
    usernames = {}
    work_ids = {}
    for row in rows:
        username = normalize(row["username"])
        work_id = row["work_id"]
        if username in usernames:
            raise ValueError(f"duplicate username in CSV: {row['username']!r}")
        if work_id in work_ids:
            raise ValueError(f"duplicate work_id in CSV: {work_id!r}")
        usernames[username] = row["username"]
        work_ids[work_id] = row["username"]


def apply_rows(db_file, rows):
    created = updated = linked = 0
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        if version < 6:
            raise ValueError(f"metadata schema version 6 is required; found {version}")
        for row in rows:
            normalized = normalize(row["username"])
            user = conn.execute(
                "SELECT * FROM users WHERE normalized_username = ?", (normalized,)
            ).fetchone()
            if user is None:
                conn.execute(
                    """INSERT INTO users (
                        public_id, username, normalized_username, display_name,
                        email, status, provisioning_source
                    ) VALUES (?, ?, ?, ?, ?, ?, 'uis-import')""",
                    (str(uuid.uuid4()), row["username"], normalized, row.get("display_name") or None,
                     row.get("email") or None, row["status"]),
                )
                user = conn.execute(
                    "SELECT * FROM users WHERE normalized_username = ?", (normalized,)
                ).fetchone()
                created += 1
            else:
                conn.execute(
                    """UPDATE users SET username = ?, display_name = ?, email = ?,
                       status = ?, updated_at = datetime('now') WHERE id = ?""",
                    (row["username"], row.get("display_name") or None,
                     row.get("email") or None, row["status"], user["id"]),
                )
                updated += 1
            existing = conn.execute(
                "SELECT user_id FROM user_identities WHERE provider = 'campus-uis' AND subject = ?",
                (row["work_id"],),
            ).fetchone()
            if existing is not None and existing["user_id"] != user["id"]:
                raise ValueError(f"work_id already linked to another user: {row['work_id']!r}")
            conn.execute(
                """INSERT INTO user_identities (
                    user_id, provider, subject, external_username, profile_json
                ) VALUES (?, 'campus-uis', ?, ?, ?)
                ON CONFLICT(provider, subject) DO UPDATE SET
                    external_username = excluded.external_username,
                    updated_at = datetime('now')""",
                (user["id"], row["work_id"], row["username"],
                 '{"source":"uis-import"}'),
            )
            linked += 1
        conn.execute(
            "INSERT INTO operation_records (action, status, source_service, message) VALUES (?, ?, ?, ?)",
            ("identity.import_uis", "success", "metadata-cli",
             f"rows={len(rows)} created={created} updated={updated} linked={linked}"),
        )
    return created, updated, linked


def main():
    parser = argparse.ArgumentParser(description="Import UIS identities from CSV.")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.csv.is_file() or not args.db.is_file():
        parser.error("CSV and database files must exist")
    try:
        rows = read_rows(args.csv)
        validate_rows(rows)
        print(f"[PLAN] UIS rows={len(rows)}")
        if not args.apply:
            print("[INFO] Dry-run completed; no database changes were made")
            return 0
        created, updated, linked = apply_rows(args.db, rows)
        print(f"[INFO] Imported UIS users: created={created} updated={updated} linked={linked}")
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[ERROR] UIS import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
