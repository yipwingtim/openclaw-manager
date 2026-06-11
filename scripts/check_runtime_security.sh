#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

ERRORS=0
WARNINGS=0

ok() {
  echo "[OK] $*"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  echo "[WARN] $*"
}

error() {
  ERRORS=$((ERRORS + 1))
  echo "[ERROR] $*"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

container_networks() {
  local container="$1"
  docker inspect "$container" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s\n" $name}}{{end}}' 2>/dev/null
}

container_has_network() {
  local container="$1"
  local network="$2"
  container_networks "$container" | grep -Fxq "$network"
}

nginx_internal_token_header_exists() {
  local file="$1"
  awk '
    $1 == "proxy_set_header" && $2 == "X-OpenClaw-Internal-Token" {
      found = 1
    }
    END {
      exit found ? 0 : 1
    }
  ' "$file"
}

nginx_internal_token_matches() {
  local file="$1"
  local expected="$2"
  awk -v expected="$expected" '
    $1 == "proxy_set_header" && $2 == "X-OpenClaw-Internal-Token" {
      value = $3
      sub(/;$/, "", value)
      sub(/^"/, "", value)
      sub(/"$/, "", value)
      if (value == expected) {
        found = 1
      }
    }
    END {
      exit found ? 0 : 1
    }
  ' "$file"
}

echo "[INFO] OpenClaw Manager runtime security check"
echo "[INFO] Manager dir: $MANAGER_DIR"

if [ -f "$CONFIG_FILE" ]; then
  ok "config exists: $CONFIG_FILE"
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
else
  error "config missing: $CONFIG_FILE"
fi

OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/data/docker/nginx/conf}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"
MANAGER_WEB_CONTAINER_NAME="${MANAGER_WEB_CONTAINER_NAME:-openclaw-manager-web}"
USER_CONTAINER_PREFIX="${USER_CONTAINER_PREFIX:-openclaw_}"

if [ -n "${OPENCLAW_INTERNAL_TOKEN:-}" ]; then
  ok "OPENCLAW_INTERNAL_TOKEN is configured"
else
  error "OPENCLAW_INTERNAL_TOKEN is empty; manager-web internal token checks will be disabled"
fi

if [ -d "$NGINX_CONF_DIR" ]; then
  ok "Nginx conf dir exists: $NGINX_CONF_DIR"

  manager_proxy_files="$(grep -rl --include='*.conf' "openclaw-manager-web:8080" "$NGINX_CONF_DIR" 2>/dev/null || true)"
  if [ -n "$manager_proxy_files" ]; then
    while IFS= read -r file; do
      if nginx_internal_token_header_exists "$file"; then
        ok "internal token header exists: $file"
        if [ -n "${OPENCLAW_INTERNAL_TOKEN:-}" ]; then
          if nginx_internal_token_matches "$file" "$OPENCLAW_INTERNAL_TOKEN"; then
            ok "internal token header value matches config: $file"
          else
            error "internal token header value does not match OPENCLAW_INTERNAL_TOKEN: $file"
          fi
        fi
      else
        error "internal token header missing: $file"
      fi
    done <<EOF
$manager_proxy_files
EOF
  else
    warn "no Nginx conf proxies to openclaw-manager-web:8080"
  fi
else
  error "Nginx conf dir missing: $NGINX_CONF_DIR"
fi

if has_cmd docker; then
  if docker inspect "$MANAGER_WEB_CONTAINER_NAME" >/dev/null 2>&1; then
    ok "container exists: $MANAGER_WEB_CONTAINER_NAME"
    if container_has_network "$MANAGER_WEB_CONTAINER_NAME" manager-net; then
      ok "$MANAGER_WEB_CONTAINER_NAME is attached to manager-net"
    else
      error "$MANAGER_WEB_CONTAINER_NAME is not attached to manager-net"
    fi
    if container_has_network "$MANAGER_WEB_CONTAINER_NAME" agent-net; then
      error "$MANAGER_WEB_CONTAINER_NAME is attached to agent-net"
    else
      ok "$MANAGER_WEB_CONTAINER_NAME is not attached to agent-net"
    fi
  else
    warn "container not found: $MANAGER_WEB_CONTAINER_NAME"
  fi

  if docker inspect "$NGINX_CONTAINER_NAME" >/dev/null 2>&1; then
    ok "container exists: $NGINX_CONTAINER_NAME"
    if container_has_network "$NGINX_CONTAINER_NAME" agent-net; then
      ok "$NGINX_CONTAINER_NAME is attached to agent-net"
    else
      error "$NGINX_CONTAINER_NAME is not attached to agent-net"
    fi
    if container_has_network "$NGINX_CONTAINER_NAME" manager-net; then
      ok "$NGINX_CONTAINER_NAME is attached to manager-net"
    else
      error "$NGINX_CONTAINER_NAME is not attached to manager-net"
    fi
  else
    warn "container not found: $NGINX_CONTAINER_NAME"
  fi

  user_containers="$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep "^${USER_CONTAINER_PREFIX}" || true)"
  if [ -n "$user_containers" ]; then
    while IFS= read -r container; do
      if container_has_network "$container" manager-net; then
        error "user container is attached to manager-net: $container"
      else
        ok "user container is not attached to manager-net: $container"
      fi
    done <<EOF
$user_containers
EOF
  else
    warn "no user containers found with prefix: $USER_CONTAINER_PREFIX"
  fi

  if docker inspect "$NGINX_CONTAINER_NAME" >/dev/null 2>&1; then
    if docker exec "$NGINX_CONTAINER_NAME" sh -lc 'wget -S -O- -T 3 http://openclaw-manager-web:8080/admin/users 2>&1 | grep -q "403 FORBIDDEN"' >/dev/null 2>&1; then
      ok "manager-web rejects direct internal admin request without token"
    else
      warn "could not verify 403 for direct internal admin request without token"
    fi
  fi
else
  error "docker command not found"
fi

echo "[SUMMARY] errors=$ERRORS warnings=$WARNINGS"

if [ "$ERRORS" -gt 0 ]; then
  exit 1
fi

exit 0
