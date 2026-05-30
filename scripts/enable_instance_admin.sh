#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

source "$CONFIG_FILE"

NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:?Missing NGINX_USERS_CONF_DIR in config}"
NGINX_HTPASSWD_FILE_IN_CONTAINER="${NGINX_HTPASSWD_FILE_IN_CONTAINER:?Missing NGINX_HTPASSWD_FILE_IN_CONTAINER in config}"

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

  if grep -q 'X-OpenClaw-User' "$nginx_conf"; then
    echo "[INFO] Instance admin already enabled: $user_id"
    continue
  fi

  python3 - "$nginx_conf" "$user_id" "$NGINX_HTPASSWD_FILE_IN_CONTAINER" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
user_id = sys.argv[2]
htpasswd_path = sys.argv[3]

text = path.read_text(encoding="utf-8")
marker = "    location / {\n"

admin_block = f"""    location = /admin {{
        return 302 /admin/;
    }}

    location /admin/ {{
        auth_basic "OpenClaw Login";
        auth_basic_user_file {htpasswd_path};

        proxy_pass http://openclaw-manager-web:8080/instance-admin/;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-OpenClaw-User "{user_id}";

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
