#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

BASE_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
METADATA_DB_FILE="${METADATA_DB_FILE:-$BASE_DIR/manager.db}"
LOG_DIR="$BASE_DIR/logs/scripts"
LOG_FILE="$LOG_DIR/refresh_device_cache.log"

mkdir -p "$LOG_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

if ! INSTANCE_ROWS="$(python3 - "$METADATA_DB_FILE" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    rows = connection.execute(
        """
        SELECT legacy_user_id, data_path, runtime_identifier
        FROM instances
        WHERE product = 'openclaw' AND status = 'active'
        ORDER BY legacy_user_id
        """
    )
    for row in rows:
        if not all(row) or any("\t" in value or "\n" in value for value in row):
            raise ValueError("invalid OpenClaw instance metadata")
        print("\t".join(row))
PY
)"; then
  log "[ERROR] Could not read OpenClaw instance metadata: $METADATA_DB_FILE"
  exit 1
fi

while IFS=$'\t' read -r user_id data_path runtime_target; do
  [ -n "$user_id" ] || continue
  log "[INFO] Refresh device cache for user: $user_id"
  OPENCLAW_DATA_PATH="$data_path" \
  OPENCLAW_RUNTIME_TARGET="$runtime_target" \
  "$SCRIPT_DIR/approve_device.sh" "$user_id" --list-only >> "$LOG_FILE" 2>&1 || {
    log "[ERROR] Failed to refresh device cache for user: $user_id"
    continue
  }
done <<< "$INSTANCE_ROWS"

log "[INFO] Device cache refresh completed."
