#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
LIB_NGINX_AUTH="$SCRIPT_DIR/lib_nginx_auth.sh"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

source "$CONFIG_FILE"
source "$LIB_NGINX_AUTH"

NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:?Missing NGINX_USERS_CONF_DIR in config}"
NGINX_HTPASSWD_FILE_IN_CONTAINER="${NGINX_HTPASSWD_FILE_IN_CONTAINER:?Missing NGINX_HTPASSWD_FILE_IN_CONTAINER in config}"
NGINX_HTPASSWD_FILE="${NGINX_HTPASSWD_FILE:?Missing NGINX_HTPASSWD_FILE in config}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <user_id> [user_id ...]" >&2
  exit 1
fi

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
  if ! grep -q 'X-OpenClaw-User' "$nginx_conf"; then
    if ! grep -q '^    location / {$' "$nginx_conf"; then
      echo "[ERROR] Nginx root location marker not found: $nginx_conf" >&2
      exit 1
    fi
    user_htpasswd_file="$(nginx_user_htpasswd_file "$user_id" "$NGINX_HTPASSWD_FILE")"
    if [ ! -f "$user_htpasswd_file" ] || ! awk -F: -v user="$user_id" '$1 == user { found=1 } END { exit !found }' "$user_htpasswd_file"; then
      echo "[ERROR] Basic Auth credentials not found for: $user_id" >&2
      exit 1
    fi
  fi
done

NGINX_ADMIN_PROVIDER_GUARD="$(render_instance_admin_provider_guard "${MANAGER_AUTH_PROVIDER:-nginx-basic}" "${PUBLIC_HOST:-}" "${MANAGER_AUTH_TYPE:-}")" || {
  echo "[ERROR] Unsupported manager authentication configuration" >&2
  exit 1
}
backup_dir="$(mktemp -d)"
for user_id in "$@"; do
  nginx_conf="$NGINX_USERS_CONF_DIR/${user_id}.conf"
  cp "$nginx_conf" "$backup_dir/${user_id}.conf"
done

rollback_admin_configs() {
  local status="$?"
  trap - ERR
  set +e
  for user_id in "$@"; do
    backup="$backup_dir/${user_id}.conf"
    [ ! -f "$backup" ] || cp "$backup" "$NGINX_USERS_CONF_DIR/${user_id}.conf"
  done
  docker exec "$NGINX_CONTAINER_NAME" nginx -t >/dev/null 2>&1 \
    && docker exec "$NGINX_CONTAINER_NAME" nginx -s reload >/dev/null 2>&1 \
    || true
  rm -r -- "$backup_dir"
  echo "[ERROR] Could not enable instance admin; previous configs restored" >&2
  exit "$status"
}
trap 'rollback_admin_configs "$@"' ERR

for user_id in "$@"; do
  nginx_conf="$NGINX_USERS_CONF_DIR/${user_id}.conf"

  if grep -q 'X-OpenClaw-User' "$nginx_conf"; then
    python3 - "$nginx_conf" "${OPENCLAW_INTERNAL_TOKEN:-}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
token = sys.argv[2]
text = path.read_text(encoding="utf-8")
header = re.compile(
    r'^\s*proxy_set_header\s+X-OpenClaw-Internal-Token\s+"[^"]*";\s*$',
    re.MULTILINE,
)
replacement = (
    f'        proxy_set_header X-OpenClaw-Internal-Token "{token}";'
    if token
    else ""
)
updated, count = header.subn(lambda _: replacement, text)
if count > 1:
    raise SystemExit(f"Expected at most one internal token header in {path}, found {count}")
if count == 0 and token:
    actor_header = re.compile(
        r'(^\s*proxy_set_header\s+X-OpenClaw-User\s+"[^"]*";\s*$)',
        re.MULTILINE,
    )
    updated, actor_count = actor_header.subn(
        lambda match: f"{match.group(1)}\n{replacement}", text
    )
    if actor_count != 1:
        raise SystemExit(f"Expected one instance actor header in {path}, found {actor_count}")
path.write_text(updated, encoding="utf-8")
PY
    echo "[INFO] Refreshed instance admin token: $user_id"
    continue
  fi

  user_htpasswd_file="$(nginx_user_htpasswd_file "$user_id" "$NGINX_HTPASSWD_FILE")"
  user_htpasswd_file_in_container="$(nginx_user_htpasswd_file_in_container "$user_id" "$NGINX_HTPASSWD_FILE_IN_CONTAINER")"

  ensure_nginx_htpasswd_permissions "$user_htpasswd_file"

  auth_block="$(render_nginx_auth_lines "true" "$user_htpasswd_file_in_container")"
  internal_token_header=""
  if [ -n "${OPENCLAW_INTERNAL_TOKEN:-}" ]; then
    internal_token_header="        proxy_set_header X-OpenClaw-Internal-Token \"${OPENCLAW_INTERNAL_TOKEN}\";"
  fi

  python3 - "$nginx_conf" "$user_id" "$auth_block" "$NGINX_ADMIN_PROVIDER_GUARD" "$internal_token_header" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
user_id = sys.argv[2]
auth_block = sys.argv[3]
provider_guard = sys.argv[4]
internal_token_header = sys.argv[5]

text = path.read_text(encoding="utf-8")
marker = "    location / {\n"

admin_block = f"""    location = /admin {{
        return 302 /admin/;
    }}

    location /admin/ {{
{provider_guard}
{auth_block}
        proxy_pass http://openclaw-manager-user-web:8080/instance-admin/;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-OpenClaw-User "{user_id}";
{internal_token_header}

        proxy_read_timeout 300;
        proxy_send_timeout 300;
    }}

"""

if marker not in text:
    raise SystemExit(f"Could not find nginx location block marker in {path}")

path.write_text(text.replace(marker, admin_block + marker, 1), encoding="utf-8")
PY

  echo "[INFO] Enabled instance admin: $user_id"
done

bash "$SCRIPT_DIR/update_manager_auth.sh"
trap - ERR
rm -r -- "$backup_dir"
