#!/usr/bin/env python3

import argparse
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from legacy_recycle import deleted_payload


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT_DIR / "db" / "schema.sql"


def normalize_username(value):
    return unicodedata.normalize("NFKC", value).casefold()


def schema_version(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def load_v1_instances(conn):
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute("SELECT * FROM instances ORDER BY id")]


def validate_instances(instances):
    by_name = {}
    runtimes = {}
    data_paths = {}
    errors = []
    for instance in instances:
        legacy_id = instance["user_id"]
        normalized = normalize_username(legacy_id)
        previous = by_name.get(normalized)
        if previous is not None and previous != legacy_id:
            errors.append(
                f"normalized username collision: {previous!r} and {legacy_id!r}"
            )
        by_name[normalized] = legacy_id

        runtime = instance.get("container_name") or f"{instance.get('product') or 'openclaw'}_{legacy_id}"
        previous_runtime = runtimes.get(runtime)
        if previous_runtime is not None and previous_runtime != legacy_id:
            errors.append(
                f"runtime identifier collision: {runtime!r} used by {previous_runtime!r} and {legacy_id!r}"
            )
        runtimes[runtime] = legacy_id
        data_path = instance.get("data_path")
        if data_path:
            previous_path = data_paths.get(data_path)
            if previous_path is not None and previous_path != legacy_id:
                errors.append(
                    f"data path collision: {data_path!r} used by {previous_path!r} and {legacy_id!r}"
                )
            data_paths[data_path] = legacy_id
    return errors


def validate_relations(conn, instances):
    known = {row["user_id"] for row in instances}
    errors = [f"foreign key violation: {tuple(row)}" for row in conn.execute("PRAGMA foreign_key_check")]
    for table in ("instance_credentials", "ports"):
        for row in conn.execute(f"SELECT DISTINCT user_id FROM {table} WHERE user_id IS NOT NULL"):
            if row[0] not in known:
                errors.append(f"orphan {table} row for user_id={row[0]!r}")
    return errors


def execute_schema(conn, schema_file):
    statement = ""
    for line in schema_file.read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise RuntimeError("incomplete SQL statement in schema")


def backup_database(db_file):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_file.with_name(f"{db_file.name}.pre-v2-{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = db_file.with_name(f"{db_file.name}.pre-v2-{stamp}-{suffix}.bak")
        suffix += 1
    with sqlite3.connect(db_file) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def migrate(conn, instances, schema_file, public_dir):
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in ("instances", "instance_credentials", "ports", "operation_records"):
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")

        execute_schema(conn, schema_file)

        user_ids = {}
        normalized_user_ids = {}
        instance_ids = {}
        for old in instances:
            legacy_id = old["user_id"]
            now = old.get("updated_at") or old.get("created_at") or datetime.now(timezone.utc).isoformat()
            public_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO users (
                    public_id, username, normalized_username, status,
                    provisioning_source, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', 'legacy', ?, ?)
                """,
                (public_id, legacy_id, normalize_username(legacy_id), old.get("created_at") or now, now),
            )
            owner_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            user_ids[legacy_id] = owner_id
            normalized_user_ids[normalize_username(legacy_id)] = owner_id
            conn.execute(
                """
                INSERT INTO user_identities (
                    user_id, provider, subject, external_username, created_at, updated_at
                ) VALUES (?, 'legacy', ?, ?, ?, ?)
                """,
                (owner_id, legacy_id, legacy_id, old.get("created_at") or now, now),
            )

            runtime = old.get("container_name") or f"{old.get('product') or 'openclaw'}_{legacy_id}"
            restore_state = "not_applicable"
            data_path = old.get("data_path")
            if old.get("status") == "deleted":
                restore_state, recycle_path = deleted_payload(public_dir, legacy_id)
                if recycle_path is not None:
                    data_path = str(recycle_path)
            conn.execute(
                """
                INSERT INTO instances (
                    id, public_id, legacy_user_id, owner_user_id, product,
                    instance_name, runtime_identifier, port, status, restore_state,
                    openclaw_version, basic_auth_enabled, container_name,
                    access_url, admin_url, data_path, nginx_conf_path,
                    created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    old["id"], str(uuid.uuid4()), legacy_id, owner_id,
                    old.get("product") or "openclaw", legacy_id, runtime,
                    old.get("port"), old.get("status") or "active",
                    restore_state,
                    old.get("openclaw_version"), old.get("basic_auth_enabled", 1),
                    old.get("container_name") or runtime, old.get("access_url"),
                    old.get("admin_url"), data_path, old.get("nginx_conf_path"),
                    old.get("created_at") or now, now, old.get("deleted_at"),
                ),
            )
            instance_ids[legacy_id] = old["id"]

            if old.get("port") is not None:
                conn.execute(
                    """
                    INSERT INTO instance_endpoints (
                        instance_id, endpoint_type, external_port, access_url,
                        status, created_at, updated_at
                    ) VALUES (?, 'legacy_port', ?, ?, ?, ?, ?)
                    """,
                    (
                        old["id"], old["port"], old.get("access_url"),
                        "inactive" if old.get("status") == "deleted" else "active",
                        old.get("created_at") or now, now,
                    ),
                )

        for row in conn.execute("SELECT * FROM instance_credentials_v1"):
            conn.execute(
                """
                INSERT INTO instance_credentials (
                    id, instance_id, basic_auth_username, basic_auth_password_ref,
                    openclaw_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], instance_ids[row["user_id"]], row["basic_auth_username"],
                    row["basic_auth_password_ref"], row["openclaw_token"],
                    row["created_at"], row["updated_at"],
                ),
            )

        for row in conn.execute("SELECT * FROM ports_v1"):
            instance_id = instance_ids.get(row["user_id"])
            conn.execute(
                "INSERT INTO ports (port, instance_id, status, created_at, released_at) VALUES (?, ?, ?, ?, ?)",
                (row["port"], instance_id, row["status"], row["created_at"], row["released_at"]),
            )

        for row in conn.execute("SELECT * FROM operation_records_v1"):
            actor_id = (
                normalized_user_ids.get(normalize_username(row["actor"]))
                if row["actor"]
                else None
            )
            instance_id = instance_ids.get(row["user_id"])
            conn.execute(
                """
                INSERT INTO operation_records (
                    id, actor, actor_user_id, action, user_id, instance_id,
                    status, message, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["actor"], actor_id, row["action"], row["user_id"],
                    instance_id, row["status"], row["message"], row["created_at"], row["finished_at"],
                ),
            )

        for table in ("instance_credentials_v1", "ports_v1", "operation_records_v1", "instances_v1"):
            conn.execute(f"DROP TABLE {table}")
        execute_schema(conn, schema_file)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after migration: {violations}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def main():
    parser = argparse.ArgumentParser(description="Migrate metadata to the user/identity/instance model.")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--public-dir", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if not args.schema.is_file():
        print(f"[ERROR] schema file not found: {args.schema}", file=sys.stderr)
        return 1
    try:
        with sqlite3.connect(":memory:") as schema_conn:
            execute_schema(schema_conn, args.schema)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"[ERROR] invalid schema: {exc}", file=sys.stderr)
        return 1

    if not args.db.is_file():
        print(f"[ERROR] metadata database not found: {args.db}", file=sys.stderr)
        return 1

    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        version = schema_version(conn)
        if version >= 2:
            print(f"[INFO] already at schema version {version}")
            return 0
        if version != 1:
            print(f"[ERROR] unsupported schema version: {version}", file=sys.stderr)
            return 1
        instances = load_v1_instances(conn)
        errors = validate_instances(instances) + validate_relations(conn, instances)
        if errors:
            for error in errors:
                print(f"[ERROR] {error}", file=sys.stderr)
            return 1
        deleted = sum(1 for row in instances if row.get("status") == "deleted")
        public_dir = args.public_dir or args.db.parent
        restore_states = [
            deleted_payload(public_dir, row["user_id"])[0]
            for row in instances
            if row.get("status") == "deleted"
        ]
        print(
            f"[PLAN] users={len(instances)} instances={len(instances)} deleted={deleted} "
            f"restorable={restore_states.count('restorable')} "
            f"incomplete={restore_states.count('incomplete')}"
        )
        if args.dry_run:
            print("[INFO] Dry-run completed; no database changes were made")
            return 0

    if not args.no_backup:
        backup = backup_database(args.db)
        print(f"[INFO] Backup created: {backup}")

    with sqlite3.connect(args.db, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        migrate(conn, instances, args.schema, public_dir)
    print("[INFO] Metadata migration to schema version 2 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
