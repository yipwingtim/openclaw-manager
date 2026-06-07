#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

MISSING=0
WARNINGS=0

ok() {
  echo "[OK] $*"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  echo "[WARN] $*"
}

missing() {
  MISSING=$((MISSING + 1))
  echo "[MISSING] $*"
}

check_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "command exists: $1"
  else
    missing "command not found: $1"
  fi
}

check_file() {
  if [ -f "$1" ]; then
    ok "file exists: $1"
  else
    missing "file missing: $1"
  fi
}

check_nonempty_file() {
  if [ -s "$1" ]; then
    ok "file exists and is non-empty: $1"
  elif [ -f "$1" ]; then
    warn "file exists but is empty: $1"
  else
    missing "file missing: $1"
  fi
}

check_dir() {
  if [ -d "$1" ]; then
    ok "directory exists: $1"
  else
    missing "directory missing: $1"
  fi
}

check_network() {
  if command -v docker >/dev/null 2>&1 && docker network inspect "$1" >/dev/null 2>&1; then
    ok "Docker network exists: $1"
  else
    missing "Docker network missing or Docker unavailable: $1"
  fi
}

echo "[INFO] OpenClaw Manager readiness check"
echo "[INFO] Manager dir: $MANAGER_DIR"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [ "${ID:-}" = "ubuntu" ]; then
    ok "OS is Ubuntu: ${VERSION_ID:-unknown}"
  else
    warn "OS is not Ubuntu: ${PRETTY_NAME:-unknown}"
  fi
else
  warn "could not read /etc/os-release"
fi

check_cmd bash
check_cmd python3
check_cmd sudo
check_cmd docker
check_cmd htpasswd

if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then
    ok "Docker Compose plugin works"
  else
    missing "Docker Compose plugin not available"
  fi

  if docker ps >/dev/null 2>&1; then
    ok "current user can run docker ps"
  else
    missing "current user cannot run docker ps"
  fi
fi

if [ -f "$CONFIG_FILE" ]; then
  ok "config exists: $CONFIG_FILE"
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
else
  missing "config missing: $CONFIG_FILE"
fi

OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
PORT_FILE="${PORT_FILE:-$OPENCLAW_PUBLIC_DIR/ports.txt}"
USERS_CSV="${USERS_CSV:-$OPENCLAW_PUBLIC_DIR/users.csv}"
METADATA_DB_FILE="${METADATA_DB_FILE:-$OPENCLAW_PUBLIC_DIR/manager.db}"

NGINX_COMPOSE_DIR="${NGINX_COMPOSE_DIR:-/data/docker/nginx/compose}"
NGINX_COMPOSE_FILE="${NGINX_COMPOSE_FILE:-$NGINX_COMPOSE_DIR/docker-compose.yml}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/data/docker/nginx/conf}"
NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:-$NGINX_CONF_DIR}"
NGINX_CERTS_DIR="${NGINX_CERTS_DIR:-/data/docker/nginx/certs}"
NGINX_LOGS_DIR="${NGINX_LOGS_DIR:-/data/docker/nginx/logs}"
NGINX_AUTH_DIR="${NGINX_AUTH_DIR:-/data/docker/nginx/auth}"
NGINX_AUTH_USERS_DIR="${NGINX_AUTH_USERS_DIR:-$NGINX_AUTH_DIR/users}"
NGINX_HTPASSWD_FILE="${NGINX_HTPASSWD_FILE:-$NGINX_AUTH_DIR/.htpasswd}"
NGINX_SSL_CERT="${NGINX_SSL_CERT:-/etc/nginx/certs/openclaw.crt}"
NGINX_SSL_KEY="${NGINX_SSL_KEY:-/etc/nginx/certs/openclaw.key}"

check_dir "$OPENCLAW_PUBLIC_DIR"
check_dir "$OPENCLAW_PUBLIC_DIR/users"
check_dir "$OPENCLAW_PUBLIC_DIR/deleted"
check_dir "$OPENCLAW_PUBLIC_DIR/logs"
check_file "$USERS_CSV"
check_file "$PORT_FILE"
check_file "$METADATA_DB_FILE"

check_dir "$NGINX_CONF_DIR"
check_dir "$NGINX_USERS_CONF_DIR"
check_dir "$NGINX_CERTS_DIR"
check_dir "$NGINX_LOGS_DIR"
check_dir "$NGINX_AUTH_DIR"
check_dir "$NGINX_AUTH_USERS_DIR"
check_file "$NGINX_COMPOSE_FILE"
check_nonempty_file "$NGINX_HTPASSWD_FILE"

cert_host_path="$NGINX_SSL_CERT"
key_host_path="$NGINX_SSL_KEY"
case "$cert_host_path" in
  /etc/nginx/certs/*)
    cert_host_path="$NGINX_CERTS_DIR/${cert_host_path#/etc/nginx/certs/}"
    ;;
esac
case "$key_host_path" in
  /etc/nginx/certs/*)
    key_host_path="$NGINX_CERTS_DIR/${key_host_path#/etc/nginx/certs/}"
    ;;
esac
check_file "$cert_host_path"
check_file "$key_host_path"

check_network agent-net
check_network manager-net

echo "[SUMMARY] missing=$MISSING warnings=$WARNINGS"

if [ "$MISSING" -gt 0 ]; then
  exit 1
fi

exit 0
