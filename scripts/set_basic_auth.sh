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

  user_htpasswd_file="$(nginx_user_htpasswd_file "$user_id" "$NGINX_HTPASSWD_FILE")"
  user_htpasswd_file_in_container="$(nginx_user_htpasswd_file_in_container "$user_id" "$NGINX_HTPASSWD_FILE_IN_CONTAINER")"
  user_htpasswd_ref="$(nginx_user_htpasswd_ref "$user_id" "$NGINX_HTPASSWD_FILE_IN_CONTAINER")"

  if [ "$BASIC_AUTH_ENABLED" = "true" ]; then
    if [ ! -f "$user_htpasswd_file" ] || ! awk -F: -v user="$user_id" '$1 == user { found=1 } END { exit !found }' "$user_htpasswd_file"; then
      echo "[ERROR] Basic Auth credentials not found for: $user_id" >&2
      echo "[ERROR] Create or update the password before enabling Basic Auth:" >&2
      echo "  htpasswd '$user_htpasswd_file' '$user_id'" >&2
      exit 1
    fi
    ensure_nginx_htpasswd_permissions "$user_htpasswd_file"
  fi

  python3 - "$nginx_conf" "$BASIC_AUTH_ENABLED" "$user_htpasswd_file_in_container" <<'PY'
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

location_match = re.search(
    r'(?ms)^    location / \{\n(?P<body>.*?)(?=^    \})^    \}',
    text,
)
if not location_match:
    raise SystemExit(f"[ERROR] Could not find root location block in {path}")

body = location_match.group("body")
patterns = [
    r'        auth_basic "OpenClaw Login";\n        auth_basic_user_file [^;]+;',
    r'        auth_basic off;',
]

updated_body = body
for pattern in patterns:
    updated_body = re.sub(pattern, replacement, updated_body, count=1)

if updated_body == body:
    print(f"[INFO] Basic Auth already set to {enabled}: {path}")
    raise SystemExit(0)

updated = text[: location_match.start("body")] + updated_body + text[location_match.end("body"):]

if updated == text:
    print(f"[INFO] Basic Auth already set to {enabled}: {path}")
    raise SystemExit(0)

path.write_text(updated, encoding="utf-8")
PY

  echo "[INFO] Set Basic Auth $BASIC_AUTH_ENABLED for: $user_id"
  if [ "${OPENCLAW_SKIP_METADATA_WRITE:-0}" != "1" ]; then
    python3 "$SCRIPT_DIR/metadata_cli.py" set-basic-auth \
      --user-id "$user_id" \
      --enabled "$BASIC_AUTH_ENABLED" \
      --basic-auth-password-ref "$user_htpasswd_ref" \
      || echo "[WARN] Metadata update failed for Basic Auth: $user_id"
  fi
done

echo "[INFO] Run nginx test and reload after reviewing changes:"
echo "  docker exec ${NGINX_CONTAINER_NAME:-openclaw-nginx} nginx -t"
echo "  docker exec ${NGINX_CONTAINER_NAME:-openclaw-nginx} nginx -s reload"
