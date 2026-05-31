#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
LIB_NGINX_AUTH="$SCRIPT_DIR/lib_nginx_auth.sh"

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <true|false> <user_id> [user_id ...]" >&2
  echo "Example: $0 false zongheban" >&2
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

source "$CONFIG_FILE"
source "$LIB_NGINX_AUTH"

NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:?Missing NGINX_USERS_CONF_DIR in config}"
NGINX_HTPASSWD_FILE_IN_CONTAINER="${NGINX_HTPASSWD_FILE_IN_CONTAINER:?Missing NGINX_HTPASSWD_FILE_IN_CONTAINER in config}"
NGINX_HTPASSWD_FILE="${NGINX_HTPASSWD_FILE:?Missing NGINX_HTPASSWD_FILE in config}"

BASIC_AUTH_ENABLED="$(normalize_basic_auth_enabled "$1")" || {
  echo "[ERROR] First argument must be true or false" >&2
  exit 1
}
shift

for user_id in "$@"; do
  if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "[ERROR] Invalid user_id: $user_id" >&2
    exit 1
  fi

  nginx_conf="$NGINX_USERS_CONF_DIR/${user_id}.conf"

  if [ ! -f "$nginx_conf" ]; then
    echo "[ERROR] Nginx config not found: $nginx_conf" >&2
    exit 1
  fi

  if [ "$BASIC_AUTH_ENABLED" = "true" ]; then
    if [ ! -f "$NGINX_HTPASSWD_FILE" ] || ! awk -F: -v user="$user_id" '$1 == user { found=1 } END { exit !found }' "$NGINX_HTPASSWD_FILE"; then
      echo "[ERROR] Basic Auth credentials not found for: $user_id" >&2
      echo "[ERROR] Create or update the password before enabling Basic Auth:" >&2
      echo "  htpasswd '$NGINX_HTPASSWD_FILE' '$user_id'" >&2
      exit 1
    fi
  fi

  python3 - "$nginx_conf" "$BASIC_AUTH_ENABLED" "$NGINX_HTPASSWD_FILE_IN_CONTAINER" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
enabled = sys.argv[2]
htpasswd_path = sys.argv[3]
text = path.read_text(encoding="utf-8")

if enabled == "true":
    replacement = (
        '        auth_basic "OpenClaw Login";\n'
        f'        auth_basic_user_file {htpasswd_path};'
    )
else:
    replacement = "        auth_basic off;"

patterns = [
    r'        auth_basic "OpenClaw Login";\n        auth_basic_user_file [^;]+;',
    r'        auth_basic off;',
]

updated = text
for pattern in patterns:
    updated = re.sub(pattern, replacement, updated)

if updated == text:
    print(f"[INFO] Basic Auth already set to {enabled}: {path}")
    raise SystemExit(0)

path.write_text(updated, encoding="utf-8")
PY

  echo "[INFO] Set Basic Auth $BASIC_AUTH_ENABLED for: $user_id"
done

echo "[INFO] Run nginx test and reload after reviewing changes:"
echo "  docker exec ${NGINX_CONTAINER_NAME:-openclaw-nginx} nginx -t"
echo "  docker exec ${NGINX_CONTAINER_NAME:-openclaw-nginx} nginx -s reload"
