#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
TEMPLATE="$MANAGER_DIR/templates/nginx/manager-web.conf.tpl"

[ -f "$CONFIG_FILE" ] || { echo "[ERROR] Config file not found: $CONFIG_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG_FILE"

MANAGER_AUTH_PROVIDER="${MANAGER_AUTH_PROVIDER:-nginx-basic}"
NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:?Missing NGINX_USERS_CONF_DIR}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"
NGINX_HTPASSWD_FILE_IN_CONTAINER="${NGINX_HTPASSWD_FILE_IN_CONTAINER:?Missing NGINX_HTPASSWD_FILE_IN_CONTAINER}"
NGINX_SSL_CERT="${NGINX_SSL_CERT:-/etc/nginx/certs/openclaw.crt}"
NGINX_SSL_KEY="${NGINX_SSL_KEY:-/etc/nginx/certs/openclaw.key}"
target="$NGINX_USERS_CONF_DIR/manager-web.conf"

case "$MANAGER_AUTH_PROVIDER" in
  nginx-basic)
    MANAGER_NGINX_AUTH_DIRECTIVES="    auth_basic \"OpenClaw Manager\";
    auth_basic_user_file $NGINX_HTPASSWD_FILE_IN_CONTAINER;"
    ;;
  local)
    MANAGER_NGINX_AUTH_DIRECTIVES="    auth_basic off;"
    ;;
  *)
    echo "[ERROR] Authentication provider is not implemented: $MANAGER_AUTH_PROVIDER" >&2
    exit 1
    ;;
esac

MANAGER_INTERNAL_TOKEN_HEADER=""
if [ -n "${OPENCLAW_INTERNAL_TOKEN:-}" ]; then
  MANAGER_INTERNAL_TOKEN_HEADER="        proxy_set_header X-OpenClaw-Internal-Token \"$OPENCLAW_INTERNAL_TOKEN\";"
fi
export NGINX_SSL_CERT NGINX_SSL_KEY MANAGER_NGINX_AUTH_DIRECTIVES MANAGER_INTERNAL_TOKEN_HEADER

tmp="$(mktemp "$NGINX_USERS_CONF_DIR/.manager-web.conf.XXXXXX")"
backup=""
python3 - "$TEMPLATE" > "$tmp" <<'PY'
import os
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for key in ("NGINX_SSL_CERT", "NGINX_SSL_KEY", "MANAGER_NGINX_AUTH_DIRECTIVES", "MANAGER_INTERNAL_TOKEN_HEADER"):
    text = text.replace("{{" + key + "}}", os.environ.get(key, ""))
print(text, end="")
PY

if [ -f "$target" ]; then
  backup="$target.bak.$(date +%Y%m%d_%H%M%S)"
  cp "$target" "$backup"
fi
mv "$tmp" "$target"
if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
  [ -n "$backup" ] && cp "$backup" "$target"
  echo "[ERROR] Nginx validation failed; previous config restored" >&2
  exit 1
fi
docker exec "$NGINX_CONTAINER_NAME" nginx -s reload
echo "[INFO] manager-web authentication provider configured: $MANAGER_AUTH_PROVIDER"
