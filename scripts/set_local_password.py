#!/usr/bin/env python3

import argparse
import getpass
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "services" / "manager-web"))

import metadata_store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Enable or reset local login for an existing platform user.")
    parser.add_argument("username")
    parser.add_argument("--db", type=Path, default=metadata_store.DB_FILE)
    parser.add_argument("--role", choices=("admin", "user"))
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()

    metadata_store.initialize(args.db, ROOT_DIR / "db" / "schema.sql")
    user = metadata_store.get_user_by_username(args.username, db_file=args.db)
    if user is None or user["status"] == "deleted":
        parser.error("platform user does not exist or is deleted")

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("passwords do not match")
    if len(password) < 12:
        parser.error("password must contain at least 12 characters")

    subject = metadata_store.normalize_username(user["username"])
    metadata_store.upsert_identity(
        user["id"], "local", subject, user["username"], db_file=args.db
    )
    metadata_store.set_local_credential(
        user["id"], generate_password_hash(password, method="scrypt"), db_file=args.db
    )
    if args.role:
        metadata_store.set_user_role(user["id"], args.role, db_file=args.db)
    print(f"[INFO] Local login updated for {user['username']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
