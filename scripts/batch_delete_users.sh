#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <input.csv> <output.csv>"
  echo
  echo "Input CSV columns:"
  echo "  user_id"
  exit 1
fi

INPUT_CSV="$1"
OUTPUT_CSV="$2"

if [ ! -f "$INPUT_CSV" ]; then
  echo "[ERROR] Input CSV not found: $INPUT_CSV" >&2
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

NGINX_COMPOSE_DIR="${NGINX_COMPOSE_DIR:?Missing NGINX_COMPOSE_DIR in config}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"

mkdir -p "$(dirname "$OUTPUT_CSV")"

csv_escape() {
  local value="${1:-}"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

trim() {
  local value="${1:-}"
  value="${value//$'\r'/}"
  printf '%s' "$value" | xargs
}

write_output_row() {
  local user_id="$1"
  local status="$2"
  local message="$3"
  {
    csv_escape "$user_id"; printf ","
    csv_escape "$status"; printf ","
    csv_escape "$message"; printf "\n"
  } >> "$OUTPUT_CSV"
}

HEADER="$(head -n 1 "$INPUT_CSV" | tr -d '\r')"
FIRST_COLUMN="${HEADER%%,*}"

if [ "$FIRST_COLUMN" != "user_id" ]; then
  echo "[ERROR] Invalid input CSV header. First column must be user_id." >&2
  echo "[ERROR] Expected: user_id" >&2
  echo "[ERROR] Actual: $HEADER" >&2
  exit 1
fi

echo "user_id,status,message" > "$OUTPUT_CSV"

line_no=0
while IFS=, read -r raw_user_id _rest; do
  line_no=$((line_no + 1))

  if [ "$line_no" -eq 1 ]; then
    continue
  fi

  user_id="$(trim "$raw_user_id")"

  if [ -z "$user_id" ]; then
    continue
  fi

  if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "[WARN] Skip invalid user_id at line $line_no: $user_id" >&2
    write_output_row "$user_id" "invalid_user_id" "Invalid user_id"
    continue
  fi

  echo "[INFO] Deleting user: $user_id"
  output="$("$SCRIPT_DIR/delete_user.sh" "$user_id" --skip-nginx-reload 2>&1)" || true
  if echo "$output" | grep -q '\[WARN\] User not found:'; then
    write_output_row "$user_id" "skipped" "User already removed"
  elif echo "$output" | grep -q '\[ERROR\]'; then
    echo "[ERROR] Failed to delete user: $user_id" >&2
    write_output_row "$user_id" "failed" "$output"
  else
    write_output_row "$user_id" "deleted" "Moved to recycle bin"
  fi
done < "$INPUT_CSV"

cd "$NGINX_COMPOSE_DIR"

if ! docker compose up -d; then
  echo "[ERROR] Failed to update nginx container" >&2
  exit 1
fi

if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
  echo "[ERROR] Nginx configuration test failed" >&2
  exit 1
fi

if ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
  echo "[ERROR] Failed to reload nginx" >&2
  exit 1
fi

echo "[INFO] Batch delete completed: $OUTPUT_CSV"
