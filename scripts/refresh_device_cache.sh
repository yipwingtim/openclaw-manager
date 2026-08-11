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
USERS_DIR="$BASE_DIR/users"
LOG_DIR="$BASE_DIR/logs/scripts"
LOG_FILE="$LOG_DIR/refresh_device_cache.log"

mkdir -p "$LOG_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

if [ ! -d "$USERS_DIR" ]; then
  log "[ERROR] Users directory not found: $USERS_DIR"
  exit 1
fi

for user_dir in "$USERS_DIR"/*; do
  [ -d "$user_dir" ] || continue

  user_id="$(basename "$user_dir")"
  container_name="openclaw_${user_id}"

  if ! docker ps --format '{{.Names}}' | grep -Fxq "$container_name"; then
    log "[WARN] Skip $user_id: container not running ($container_name)"
    continue
  fi

  log "[INFO] Refresh device cache for user: $user_id"
  "$SCRIPT_DIR/approve_device.sh" "$user_id" --list-only >> "$LOG_FILE" 2>&1 || {
    log "[ERROR] Failed to refresh device cache for user: $user_id"
    continue
  }
done

log "[INFO] Device cache refresh completed."
