#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path


REQUIRED = {"user_id", "name"}


def internal_username(user_id):
    return "uis_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def valid_email(value):
    return (
        len(value) <= 320 and not any(character.isspace() for character in value)
        and value.count("@") == 1 and all(value.split("@"))
    )


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
            if not row["user_id"] or not row["name"]:
                raise ValueError(f"row {number}: user_id and name are required")
            if len(row["user_id"]) > 128 or len(row["name"]) > 128:
                raise ValueError(f"row {number}: user_id or name is too long")
            if row.get("email") and not valid_email(row["email"]):
                raise ValueError(f"row {number}: invalid email")
            if row.get("status") not in {"", "active", "disabled", "locked"}:
                raise ValueError(f"row {number}: invalid status {row['status']!r}")
            rows.append(row)
    return rows


def validate_rows(rows):
    user_ids = set()
    for row in rows:
        if row["user_id"] in user_ids:
            raise ValueError(f"duplicate user_id in CSV: {row['user_id']!r}")
        user_ids.add(row["user_id"])


def apply_rows(db_file, rows):
    created = updated = linked = 0
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        if version < 6:
            raise ValueError(f"metadata schema version 6 is required; found {version}")
        for row in rows:
            user = conn.execute(
                """SELECT u.* FROM users u JOIN user_identities i ON i.user_id = u.id
                   WHERE i.provider = 'campus-uis' AND i.subject = ?""",
                (row["user_id"],),
            ).fetchone()
            if user is None:
                username = internal_username(row["user_id"])
                if conn.execute(
                    "SELECT 1 FROM users WHERE normalized_username = ?", (username,)
                ).fetchone():
                    raise ValueError(f"generated username collision for user_id: {row['user_id']!r}")
                conn.execute(
                    """INSERT INTO users (
                        public_id, username, normalized_username, display_name,
                        email, status, provisioning_source
                    ) VALUES (?, ?, ?, ?, ?, ?, 'uis-import')""",
                    (str(uuid.uuid4()), username, username, row["name"],
                     row.get("email") or None, row.get("status") or "active"),
                )
                user = conn.execute(
                    "SELECT * FROM users WHERE normalized_username = ?", (username,)
                ).fetchone()
                created += 1
            else:
                conn.execute(
                    """UPDATE users SET display_name = ?,
                       email = CASE WHEN ? = '' THEN email ELSE ? END,
                       status = CASE WHEN ? = '' THEN status ELSE ? END,
                       updated_at = datetime('now') WHERE id = ?""",
                    (row["name"], row.get("email", ""), row.get("email", ""),
                     row.get("status", ""), row.get("status", ""), user["id"]),
                )
                updated += 1
            profile = {"source": "uis-import", "user_id": row["user_id"], "user_name": row["name"]}
            if row.get("email"):
                profile["email"] = row["email"]
            conn.execute(
                """INSERT INTO user_identities (
                    user_id, provider, subject, external_username, profile_json
                ) VALUES (?, 'campus-uis', ?, ?, ?)
                ON CONFLICT(provider, subject) DO UPDATE SET
                    external_username = excluded.external_username,
                    profile_json = excluded.profile_json,
                    updated_at = datetime('now')""",
                (user["id"], row["user_id"], row["name"],
                 json.dumps(profile, ensure_ascii=False, separators=(",", ":"))),
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
