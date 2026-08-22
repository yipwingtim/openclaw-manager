#!/usr/bin/env python3

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "services" / "manager-web"))

import metadata_store  # noqa: E402


REQUIRED = {"user_id", "name"}


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
            if row.get("email") and not metadata_store.valid_email(row["email"]):
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
    with metadata_store.connect(db_file) as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        if version < 6:
            raise ValueError(f"metadata schema version 6 is required; found {version}")
        created, updated, linked = metadata_store.import_uis_users(rows, conn=conn)
        metadata_store.record_operation(
            action="identity.import_uis", status="success", source_service="metadata-cli",
            message=f"rows={len(rows)} created={created} updated={updated} linked={linked}",
            conn=conn,
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
