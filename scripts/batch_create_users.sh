#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

SKIP_NGINX_REFRESH=false

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <input.csv> <output.csv>"
  echo
  echo "Input CSV columns:"
  echo "  user_id,basic_auth_password[,basic_auth_enabled]"
  exit 1
fi

INPUT_CSV="$1"
OUTPUT_CSV="$2"
shift 2

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-nginx-refresh)
      SKIP_NGINX_REFRESH=true
      shift
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [ ! -f "$INPUT_CSV" ]; then
  echo "[ERROR] Input CSV not found: $INPUT_CSV" >&2
  exit 1
fi

HEADER="$(head -n 1 "$INPUT_CSV" | tr -d '\r')"
FIRST_COLUMN="${HEADER%%,*}"

if [ "$FIRST_COLUMN" != "user_id" ]; then
  echo "[ERROR] Invalid input CSV header. First column must be user_id." >&2
  echo "[ERROR] Expected: user_id,basic_auth_password[,basic_auth_enabled]" >&2
  echo "[ERROR] Actual: $HEADER" >&2
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"
source "$SCRIPT_DIR/lib_nginx_auth.sh"
source "$SCRIPT_DIR/lib_tenant_network.sh"

BASE_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
PUBLIC_HOST="${PUBLIC_HOST:?Missing PUBLIC_HOST in config}"
USERS_DIR="$BASE_DIR/users"
NGINX_COMPOSE_DIR="${NGINX_COMPOSE_DIR:?Missing NGINX_COMPOSE_DIR in config}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"
PAUSE_EVERY="${BATCH_CREATE_PAUSE_EVERY:-5}"
PAUSE_SECONDS="${BATCH_CREATE_PAUSE_SECONDS:-30}"

if command -v sudo >/dev/null 2>&1; then
  sudo -v
fi

mkdir -p "$(dirname "$OUTPUT_CSV")"

csv_escape() {
  local value="${1:-}"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

generate_password() {
  python3 - <<'PY'
import secrets
import string

alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(16)))
PY
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

write_output_row() {
  local user_id="$1"
  local password="$2"
  local basic_auth_enabled="$3"
  local status="$4"
  local port token access_url container_name

  port="$(detect_port "$user_id")"
  token="$(read_token "$user_id")"
  container_name="openclaw_${user_id}"
  access_url=""
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
    csv_escape "$basic_auth_enabled"; printf ","
    csv_escape "$status"; printf "\n"
  } >> "$OUTPUT_CSV"
}

echo "user_id,basic_auth_username,basic_auth_password,openclaw_token,access_url,port,container_name,basic_auth_enabled,status" > "$OUTPUT_CSV"

created_count=0
line_no=0
while IFS=, read -r raw_user_id raw_password raw_basic_auth_enabled _rest; do
  line_no=$((line_no + 1))

  raw_user_id="${raw_user_id//$'\r'/}"
  raw_password="${raw_password//$'\r'/}"
  raw_basic_auth_enabled="${raw_basic_auth_enabled//$'\r'/}"

  if [ "$line_no" -eq 1 ] && [ "$raw_user_id" = "user_id" ]; then
    continue
  fi

  user_id="$(printf '%s' "$raw_user_id" | xargs)"
  password="$(printf '%s' "${raw_password:-}" | xargs)"
  basic_auth_enabled="$(normalize_basic_auth_enabled "${raw_basic_auth_enabled:-true}")" || {
    echo "[WARN] Invalid basic_auth_enabled at line $line_no: ${raw_basic_auth_enabled:-}" >&2
    write_output_row "$user_id" "$password" "${raw_basic_auth_enabled:-}" "invalid_basic_auth_enabled"
    continue
  }

  if [ -z "$user_id" ]; then
    continue
  fi

  if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "[WARN] Skip invalid user_id at line $line_no: $user_id" >&2
    write_output_row "$user_id" "$password" "$basic_auth_enabled" "invalid_user_id"
    continue
  fi

  if [ -z "$password" ]; then
    password="$(generate_password)"
  fi

  if [ -d "$USERS_DIR/$user_id" ]; then
    echo "[INFO] User exists, skip create: $user_id"
    write_output_row "$user_id" "$password" "$basic_auth_enabled" "exists"
    continue
  fi

  echo "[INFO] Creating user: $user_id"
  if "$SCRIPT_DIR/create_user.sh" "$user_id" --password "$password" --basic-auth-enabled "$basic_auth_enabled" --skip-nginx-reload; then
    write_output_row "$user_id" "$password" "$basic_auth_enabled" "created"
    created_count=$((created_count + 1))
  else
    echo "[ERROR] Failed to create user: $user_id" >&2
    write_output_row "$user_id" "$password" "$basic_auth_enabled" "failed"
  fi

  if [ "$created_count" -gt 0 ] && [ "$PAUSE_EVERY" -gt 0 ] && [ $((created_count % PAUSE_EVERY)) -eq 0 ]; then
    echo "[INFO] Pause for $PAUSE_SECONDS seconds after $created_count created users"
    sleep "$PAUSE_SECONDS"
  fi
done < "$INPUT_CSV"

if [ "$SKIP_NGINX_REFRESH" = "true" ]; then
  echo "[INFO] Skip nginx refresh requested."
  echo "[INFO] Batch create completed: $OUTPUT_CSV"
  exit 0
fi

if ! cd "$NGINX_COMPOSE_DIR"; then
  echo "[ERROR] Failed to enter nginx compose directory: $NGINX_COMPOSE_DIR" >&2
  exit 1
fi

if ! docker compose up -d; then
  echo "[ERROR] Failed to update nginx container" >&2
  exit 1
fi

connect_shared_services_to_tenant_networks \
  "$NGINX_CONTAINER_NAME" \
  "${MODEL_PROXY_CONTAINER_NAME:-openclaw-model-proxy}"

if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
  echo "[ERROR] Nginx configuration test failed" >&2
  exit 1
fi

if ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
  echo "[ERROR] Failed to reload nginx" >&2
  exit 1
fi

echo "[INFO] Batch create completed: $OUTPUT_CSV"
