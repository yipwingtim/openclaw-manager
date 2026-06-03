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
METADATA_DB_FILE="${METADATA_DB_FILE:-$OPENCLAW_PUBLIC_DIR/manager.db}"

if [ ! -f "$SCHEMA_FILE" ]; then
  echo "[ERROR] Schema file not found: $SCHEMA_FILE" >&2
  exit 1
fi

mkdir -p "$(dirname "$METADATA_DB_FILE")"

python3 - "$METADATA_DB_FILE" "$SCHEMA_FILE" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_file = Path(sys.argv[1])
schema_file = Path(sys.argv[2])

schema = schema_file.read_text(encoding="utf-8")
with sqlite3.connect(db_file) as conn:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema)
    conn.commit()

print(f"[INFO] Metadata database initialized: {db_file}")
PY
