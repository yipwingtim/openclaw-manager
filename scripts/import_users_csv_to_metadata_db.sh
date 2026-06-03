#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
SCHEMA_FILE="$MANAGER_DIR/db/schema.sql"

if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
USERS_CSV="${USERS_CSV:-$OPENCLAW_PUBLIC_DIR/users.csv}"
NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:-/data/docker/nginx/conf}"
METADATA_DB_FILE="${METADATA_DB_FILE:-$OPENCLAW_PUBLIC_DIR/manager.db}"

if [ ! -f "$SCHEMA_FILE" ]; then
  echo "[ERROR] Schema file not found: $SCHEMA_FILE" >&2
  exit 1
fi

if [ ! -f "$USERS_CSV" ]; then
  echo "[ERROR] users.csv not found: $USERS_CSV" >&2
  exit 1
fi

mkdir -p "$(dirname "$METADATA_DB_FILE")"

python3 - "$METADATA_DB_FILE" "$SCHEMA_FILE" "$USERS_CSV" "$OPENCLAW_PUBLIC_DIR" "$PUBLIC_HOST" "$NGINX_USERS_CONF_DIR" <<'PY'
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

db_file = Path(sys.argv[1])
schema_file = Path(sys.argv[2])
users_csv = Path(sys.argv[3])
public_dir = Path(sys.argv[4])
public_host = sys.argv[5]
nginx_conf_dir = Path(sys.argv[6])


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_status(value):
    value = (value or "").strip().lower()
    if value in {"active", "stopped", "deleted", "failed"}:
        return value
    return "active"


def detect_version(compose_file):
    if not compose_file.is_file():
        return None
    text = compose_file.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"image:\s*ghcr\.io/openclaw/openclaw:([^\s]+)", text)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def detect_basic_auth_enabled(nginx_conf):
    if not nginx_conf.is_file():
        return 1
    text = nginx_conf.read_text(encoding="utf-8", errors="ignore")
    return 1 if "auth_basic" in text and "auth_basic off" not in text else 0


def iter_user_rows(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    if not rows:
        return

    first = [value.strip() for value in rows[0]]
    has_header = {"user_id", "port", "created_at"}.issubset(set(first))

    if has_header:
        header = first
        for values in rows[1:]:
            if not values or not any(value.strip() for value in values):
                continue
            row = {header[index]: values[index].strip() if index < len(values) else "" for index in range(len(header))}
            yield {
                "user_id": row.get("user_id", ""),
                "port": row.get("port", ""),
                "created_at": row.get("created_at", ""),
                "status": row.get("status", "active"),
            }
        return

    for values in rows:
        if not values or not any(value.strip() for value in values):
            continue
        yield {
            "user_id": values[0].strip() if len(values) > 0 else "",
            "port": values[1].strip() if len(values) > 1 else "",
            "created_at": values[2].strip() if len(values) > 2 else "",
            "status": values[3].strip() if len(values) > 3 else "active",
        }


schema = schema_file.read_text(encoding="utf-8")
now = utc_now()
imported = 0
skipped = 0
ports = 0

with sqlite3.connect(db_file) as conn:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema)

    for row in iter_user_rows(users_csv):
        user_id = (row.get("user_id") or "").strip()
        if not user_id:
            skipped += 1
            continue

        port_text = (row.get("port") or "").strip()
        try:
            port = int(port_text) if port_text else None
        except ValueError:
            port = None

        status = normalize_status(row.get("status"))
        created_at = (row.get("created_at") or "").strip() or now
        user_dir = public_dir / "users" / user_id
        compose_file = user_dir / "docker-compose.yml"
        nginx_conf = nginx_conf_dir / f"{user_id}.conf"
        access_url = f"https://{public_host}:{port}" if public_host and port else ""
        admin_url = f"{access_url}/admin/" if access_url else ""

        conn.execute(
            """
                INSERT INTO instances (
                    user_id,
                    product,
                    port,
                    status,
                    openclaw_version,
                    basic_auth_enabled,
                    container_name,
                    access_url,
                    admin_url,
                    data_path,
                    nginx_conf_path,
                    created_at,
                    updated_at,
                    deleted_at
                )
                VALUES (?, 'openclaw', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    port = excluded.port,
                    status = excluded.status,
                    openclaw_version = excluded.openclaw_version,
                    basic_auth_enabled = excluded.basic_auth_enabled,
                    container_name = excluded.container_name,
                    access_url = excluded.access_url,
                    admin_url = excluded.admin_url,
                    data_path = excluded.data_path,
                    nginx_conf_path = excluded.nginx_conf_path,
                    updated_at = excluded.updated_at,
                    deleted_at = excluded.deleted_at
            """,
            (
                    user_id,
                    port,
                    status,
                    detect_version(compose_file),
                    detect_basic_auth_enabled(nginx_conf),
                    f"openclaw_{user_id}",
                    access_url,
                    admin_url,
                    str(user_dir),
                    str(nginx_conf),
                    created_at,
                    now,
                    now if status == "deleted" else None,
            ),
        )
        imported += 1

        if port is not None:
            port_status = "released" if status == "deleted" else "allocated"
            conn.execute(
                """
                    INSERT INTO ports (port, user_id, status, created_at, released_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(port) DO UPDATE SET
                        user_id = excluded.user_id,
                        status = excluded.status,
                        created_at = excluded.created_at,
                        released_at = excluded.released_at
                """,
                (port, user_id, port_status, created_at, now if port_status == "released" else None),
            )
            ports += 1

    conn.commit()

print(f"[INFO] Metadata database: {db_file}")
print(f"[INFO] Imported instances: {imported}")
print(f"[INFO] Imported ports: {ports}")
print(f"[INFO] Skipped rows: {skipped}")
PY
