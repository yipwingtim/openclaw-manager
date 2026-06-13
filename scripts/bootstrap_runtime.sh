#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
CONFIG_EXAMPLE="$MANAGER_DIR/config/openclaw-manager.env.example"
NGINX_COMPOSE_TEMPLATE="$MANAGER_DIR/templates/nginx/docker-compose.tpl.yml"
MANAGER_WEB_CONF_TEMPLATE="$MANAGER_DIR/templates/nginx/manager-web.conf.tpl"
SCHEMA_FILE="$MANAGER_DIR/db/schema.sql"
BOOTSTRAP_OWNER="${BOOTSTRAP_OWNER:-$(id -u):$(id -g)}"
BOOTSTRAP_SKIP_DOCKER="${BOOTSTRAP_SKIP_DOCKER:-0}"

log() {
  echo "[INFO] $*"
}

warn() {
  echo "[WARN] $*" >&2
}

fail() {
  echo "[ERROR] $*" >&2
  exit 1
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Missing command: $1"
  fi
}

write_file() {
  local path="$1"
  local content="$2"
  local dir
  dir="$(dirname "$path")"
  mkdir_or_sudo "$dir"
  if [ -w "$dir" ]; then
    printf '%s\n' "$content" > "$path"
  else
    printf '%s\n' "$content" | sudo tee "$path" >/dev/null
    sudo chown "$BOOTSTRAP_OWNER" "$path"
  fi
}

mkdir_or_sudo() {
  local path="$1"
  if [ -d "$path" ]; then
    return
  fi
  if mkdir -p "$path" 2>/dev/null; then
    return
  fi
  sudo mkdir -p "$path"
  sudo chown "$BOOTSTRAP_OWNER" "$path"
}

touch_or_sudo() {
  local path="$1"
  local dir
  dir="$(dirname "$path")"
  mkdir_or_sudo "$dir"
  if [ -e "$path" ]; then
    return
  fi
  if touch "$path" 2>/dev/null; then
    return
  fi
  sudo touch "$path"
  sudo chown "$BOOTSTRAP_OWNER" "$path"
}

copy_if_missing() {
  local src="$1"
  local dest="$2"
  if [ -e "$dest" ]; then
    log "Keep existing file: $dest"
    return
  fi
  mkdir_or_sudo "$(dirname "$dest")"
  if cp "$src" "$dest" 2>/dev/null; then
    log "Created file: $dest"
    return
  fi
  sudo cp "$src" "$dest"
  sudo chown "$BOOTSTRAP_OWNER" "$dest"
  log "Created file: $dest"
}

render_template() {
  local template="$1"
  python3 - "$template" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
for key, value in os.environ.items():
    text = text.replace("{{" + key + "}}", value)
print(text, end="")
PY
}

create_network() {
  local name="$1"
  if docker network inspect "$name" >/dev/null 2>&1; then
    log "Docker network exists: $name"
    return
  fi
  docker network create "$name" >/dev/null
  log "Created Docker network: $name"
}

init_sqlite() {
  if [ -f "$METADATA_DB_FILE" ]; then
    log "Metadata database exists: $METADATA_DB_FILE"
    return
  fi
  if [ ! -f "$SCHEMA_FILE" ]; then
    warn "Schema file missing, skip metadata initialization: $SCHEMA_FILE"
    return
  fi
  mkdir_or_sudo "$(dirname "$METADATA_DB_FILE")"
  python3 - "$METADATA_DB_FILE" "$SCHEMA_FILE" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_file = Path(sys.argv[1])
schema_file = Path(sys.argv[2])
schema = schema_file.read_text(encoding="utf-8")
with sqlite3.connect(db_file) as conn:
    conn.executescript(schema)
print(f"[INFO] Initialized metadata database: {db_file}")
PY
}

need_cmd python3

if [ "$BOOTSTRAP_SKIP_DOCKER" = "1" ]; then
  warn "BOOTSTRAP_SKIP_DOCKER=1; skip Docker dependency checks and network creation."
else
  need_cmd docker
  docker compose version >/dev/null 2>&1 || fail "Missing Docker Compose plugin"
fi

if [ ! -f "$CONFIG_EXAMPLE" ]; then
  fail "Config example not found: $CONFIG_EXAMPLE"
fi

copy_if_missing "$CONFIG_EXAMPLE" "$CONFIG_FILE"

# shellcheck disable=SC1090
source "$CONFIG_FILE"

OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
PORT_FILE="${PORT_FILE:-$OPENCLAW_PUBLIC_DIR/ports.txt}"
USERS_CSV="${USERS_CSV:-$OPENCLAW_PUBLIC_DIR/users.csv}"
METADATA_DB_FILE="${METADATA_DB_FILE:-$OPENCLAW_PUBLIC_DIR/manager.db}"
PORT_START="${PORT_START:-30021}"
MODEL_PROXY_TOKEN_DIR="${MODEL_PROXY_TOKEN_DIR:-$OPENCLAW_PUBLIC_DIR/model-proxy-tokens}"

NGINX_COMPOSE_DIR="${NGINX_COMPOSE_DIR:-/data/docker/nginx/compose}"
NGINX_COMPOSE_FILE="${NGINX_COMPOSE_FILE:-$NGINX_COMPOSE_DIR/docker-compose.yml}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/data/docker/nginx/conf}"
NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:-$NGINX_CONF_DIR}"
NGINX_AUTH_DIR="${NGINX_AUTH_DIR:-/data/docker/nginx/auth}"
NGINX_AUTH_USERS_DIR="${NGINX_AUTH_USERS_DIR:-$NGINX_AUTH_DIR/users}"
NGINX_CERTS_DIR="${NGINX_CERTS_DIR:-/data/docker/nginx/certs}"
NGINX_LOGS_DIR="${NGINX_LOGS_DIR:-/data/docker/nginx/logs}"
NGINX_HTPASSWD_FILE_IN_CONTAINER="${NGINX_HTPASSWD_FILE_IN_CONTAINER:-/etc/nginx/auth/.htpasswd}"
NGINX_SSL_CERT="${NGINX_SSL_CERT:-/etc/nginx/certs/openclaw.crt}"
NGINX_SSL_KEY="${NGINX_SSL_KEY:-/etc/nginx/certs/openclaw.key}"

export NGINX_CONF_DIR
export NGINX_CERTS_DIR
export NGINX_LOGS_DIR
export NGINX_AUTH_DIR
export NGINX_SSL_CERT
export NGINX_SSL_KEY
export NGINX_HTPASSWD_FILE_IN_CONTAINER

mkdir_or_sudo "$OPENCLAW_PUBLIC_DIR/users"
mkdir_or_sudo "$OPENCLAW_PUBLIC_DIR/deleted"
mkdir_or_sudo "$OPENCLAW_PUBLIC_DIR/logs"
mkdir_or_sudo "$MODEL_PROXY_TOKEN_DIR"
mkdir_or_sudo "$NGINX_CONF_DIR"
mkdir_or_sudo "$NGINX_CERTS_DIR"
mkdir_or_sudo "$NGINX_LOGS_DIR"
mkdir_or_sudo "$NGINX_AUTH_USERS_DIR"
mkdir_or_sudo "$NGINX_COMPOSE_DIR"

touch_or_sudo "$NGINX_AUTH_DIR/.htpasswd"

if [ ! -f "$USERS_CSV" ]; then
  write_file "$USERS_CSV" "user_id,port,created_at,status"
  log "Initialized users.csv: $USERS_CSV"
else
  log "users.csv exists: $USERS_CSV"
fi

if [ ! -f "$PORT_FILE" ]; then
  write_file "$PORT_FILE" "$PORT_START"
  log "Initialized port file: $PORT_FILE"
else
  log "Port file exists: $PORT_FILE"
fi

if [ "$BOOTSTRAP_SKIP_DOCKER" = "1" ]; then
  log "Skip Docker network creation."
else
  create_network "agent-net"
  create_network "manager-net"
fi

init_sqlite

if [ ! -f "$NGINX_COMPOSE_FILE" ]; then
  [ -f "$NGINX_COMPOSE_TEMPLATE" ] || fail "Nginx compose template not found: $NGINX_COMPOSE_TEMPLATE"
  rendered="$(render_template "$NGINX_COMPOSE_TEMPLATE")"
  write_file "$NGINX_COMPOSE_FILE" "$rendered"
  log "Generated Nginx compose: $NGINX_COMPOSE_FILE"
else
  log "Keep existing Nginx compose: $NGINX_COMPOSE_FILE"
fi

manager_conf="$NGINX_USERS_CONF_DIR/manager-web.conf"
if [ ! -f "$manager_conf" ]; then
  [ -f "$MANAGER_WEB_CONF_TEMPLATE" ] || fail "Manager web Nginx template not found: $MANAGER_WEB_CONF_TEMPLATE"
  rendered="$(render_template "$MANAGER_WEB_CONF_TEMPLATE")"
  write_file "$manager_conf" "$rendered"
  log "Generated manager web Nginx config: $manager_conf"
else
  log "Keep existing manager web Nginx config: $manager_conf"
fi

cat <<EOF

[INFO] Bootstrap completed.

Next steps:
1. Review and edit: $CONFIG_FILE
2. Put TLS certificate and key at paths referenced by NGINX_SSL_CERT and NGINX_SSL_KEY.
3. Create the global manager Basic Auth user in: $NGINX_AUTH_DIR/.htpasswd
4. Review Nginx compose: $NGINX_COMPOSE_FILE
5. Start Nginx from: $NGINX_COMPOSE_DIR
6. Start manager services from: $MANAGER_DIR/services

This script does not overwrite existing runtime files and does not start containers.
EOF
