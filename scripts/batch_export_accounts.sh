#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <input.csv> <output.csv>"
  echo
  echo "Input CSV supported columns:"
  echo "  user_id"
  echo "  user_id,basic_auth_password"
  echo "  user_id,basic_auth_username,basic_auth_password,..."
  echo
  echo "Optional column:"
  echo "  model_group"
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

BASE_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
PUBLIC_HOST="${PUBLIC_HOST:?Missing PUBLIC_HOST in config}"
USERS_DIR="$BASE_DIR/users"

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

read_token() {
  local user_id="$1"
  local config_file="$USERS_DIR/$user_id/config/openclaw.json"

  if [ ! -f "$config_file" ]; then
    return 0
  fi

  python3 - "$config_file" <<'PY' 2>/dev/null || true
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(data.get("gateway", {}).get("auth", {}).get("token", ""))
except Exception:
    print("")
PY
}

detect_port() {
  local user_id="$1"
  local nginx_conf="${NGINX_USERS_CONF_DIR:-/data/docker/nginx/conf}/${user_id}.conf"

  if [ -f "$nginx_conf" ]; then
    grep -E '^[[:space:]]*listen[[:space:]]+[0-9]+' "$nginx_conf" \
      | head -n 1 \
      | sed -E 's/^[[:space:]]*listen[[:space:]]+([0-9]+).*/\1/' || true
  fi
}

container_status() {
  local user_id="$1"
  local container_name="openclaw_${user_id}"

  if docker ps --format '{{.Names}}' | grep -Fxq "$container_name"; then
    echo "running"
  elif docker ps -a --format '{{.Names}}' | grep -Fxq "$container_name"; then
    echo "stopped"
  else
    echo "missing"
  fi
}

column_index() {
  local header="$1"
  local name="$2"
  python3 - "$header" "$name" <<'PY'
import csv
import sys

header, name = sys.argv[1:3]
columns = [item.strip() for item in next(csv.reader([header]))]
try:
    print(columns.index(name) + 1)
except ValueError:
    print(0)
PY
}

csv_field() {
  local line="$1"
  local index="$2"
  python3 - "$line" "$index" <<'PY'
import csv
import sys

line, index = sys.argv[1], int(sys.argv[2])
row = next(csv.reader([line]))
if index <= 0 or index > len(row):
    print("")
else:
    print(row[index - 1])
PY
}

HEADER="$(head -n 1 "$INPUT_CSV" | tr -d '\r')"
USER_ID_INDEX="$(column_index "$HEADER" "user_id")"
PASSWORD_INDEX="$(column_index "$HEADER" "basic_auth_password")"
MODEL_GROUP_INDEX="$(column_index "$HEADER" "model_group")"

if [ "$USER_ID_INDEX" -eq 0 ]; then
  echo "[ERROR] Invalid input CSV header. Missing user_id column." >&2
  echo "[ERROR] Actual: $HEADER" >&2
  exit 1
fi

echo "user_id,basic_auth_username,basic_auth_password,openclaw_token,access_url,port,container_name,model_group,status" > "$OUTPUT_CSV"

line_no=0
while IFS= read -r line || [ -n "$line" ]; do
  line_no=$((line_no + 1))

  if [ "$line_no" -eq 1 ]; then
    continue
  fi

  line="${line//$'\r'/}"
  user_id="$(trim "$(csv_field "$line" "$USER_ID_INDEX")")"
  password=""
  model_group=""

  if [ "$PASSWORD_INDEX" -ne 0 ]; then
    password="$(trim "$(csv_field "$line" "$PASSWORD_INDEX")")"
  fi

  if [ "$MODEL_GROUP_INDEX" -ne 0 ]; then
    model_group="$(trim "$(csv_field "$line" "$MODEL_GROUP_INDEX")")"
  fi

  if [ -z "$user_id" ]; then
    continue
  fi

  port="$(detect_port "$user_id")"
  token="$(read_token "$user_id")"
  container_name="openclaw_${user_id}"
  access_url=""
  status="$(container_status "$user_id")"

  if [ -n "$port" ]; then
    access_url="https://${PUBLIC_HOST}:${port}"
  fi

  {
    csv_escape "$user_id"; printf ","
    csv_escape "$user_id"; printf ","
    csv_escape "$password"; printf ","
    csv_escape "$token"; printf ","
    csv_escape "$access_url"; printf ","
    csv_escape "$port"; printf ","
    csv_escape "$container_name"; printf ","
    csv_escape "$model_group"; printf ","
    csv_escape "$status"; printf "\n"
  } >> "$OUTPUT_CSV"
done < "$INPUT_CSV"

echo "[INFO] Batch account export completed: $OUTPUT_CSV"
