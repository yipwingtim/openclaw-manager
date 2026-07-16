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
MODEL_PROXY_CONTAINER_NAME="${MODEL_PROXY_CONTAINER_NAME:-openclaw-model-proxy}"
MODEL_PROXY_TOKEN_DIR="${MODEL_PROXY_TOKEN_DIR:-$OPENCLAW_PUBLIC_DIR/model-proxy-tokens}"
USER_CONTAINER_PREFIX="${USER_CONTAINER_PREFIX:-openclaw_}"
OPENCLAW_TENANT_NETWORK_PREFIX="${OPENCLAW_TENANT_NETWORK_PREFIX:-openclaw-user}"

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

if [ -d "$MODEL_PROXY_TOKEN_DIR" ]; then
  ok "model proxy token dir exists: $MODEL_PROXY_TOKEN_DIR"
  model_proxy_tokens="$(find "$MODEL_PROXY_TOKEN_DIR" -maxdepth 1 -type f -name '*.token' 2>/dev/null | sort || true)"
  if [ -n "$model_proxy_tokens" ]; then
    while IFS= read -r token_file; do
      user_id="$(basename "$token_file" .token)"
      models_file="$MODEL_PROXY_TOKEN_DIR/${user_id}.models"
      if [ -s "$models_file" ]; then
        ok "model allowlist exists: $models_file"
      elif [ -f "$models_file" ]; then
        error "model allowlist is empty: $models_file"
      else
        error "model allowlist missing for token: $token_file"
      fi
    done <<EOF
$model_proxy_tokens
EOF
  else
    warn "no model proxy token files found in: $MODEL_PROXY_TOKEN_DIR"
  fi
else
  warn "model proxy token dir missing: $MODEL_PROXY_TOKEN_DIR"
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
      warn "$NGINX_CONTAINER_NAME is attached to legacy agent-net"
    else
      ok "$NGINX_CONTAINER_NAME is not attached to legacy agent-net"
    fi
    if container_has_network "$NGINX_CONTAINER_NAME" manager-net; then
      ok "$NGINX_CONTAINER_NAME is attached to manager-net"
    else
      error "$NGINX_CONTAINER_NAME is not attached to manager-net"
    fi
  else
    warn "container not found: $NGINX_CONTAINER_NAME"
  fi

  if docker inspect "$MODEL_PROXY_CONTAINER_NAME" >/dev/null 2>&1; then
    ok "container exists: $MODEL_PROXY_CONTAINER_NAME"
    if container_has_network "$MODEL_PROXY_CONTAINER_NAME" agent-net; then
      warn "$MODEL_PROXY_CONTAINER_NAME is attached to legacy agent-net"
    else
      ok "$MODEL_PROXY_CONTAINER_NAME is not attached to legacy agent-net"
    fi
  else
    warn "container not found: $MODEL_PROXY_CONTAINER_NAME"
  fi

  user_containers="$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep "^${USER_CONTAINER_PREFIX}" || true)"
  if [ -n "$user_containers" ]; then
    while IFS= read -r container; do
      user_id="${container#"$USER_CONTAINER_PREFIX"}"
      service_id="$(printf '%s' "$user_id" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
      tenant_network="${OPENCLAW_TENANT_NETWORK_PREFIX}-${service_id}"
      if container_has_network "$container" manager-net; then
        error "user container is attached to manager-net: $container"
      else
        ok "user container is not attached to manager-net: $container"
      fi
      if container_has_network "$container" agent-net; then
        error "user container is attached to shared agent-net: $container"
      else
        ok "user container is not attached to shared agent-net: $container"
      fi
      if container_has_network "$container" "$tenant_network"; then
        ok "user container is attached to tenant network: $container -> $tenant_network"
      else
        error "user container is missing tenant network: $container -> $tenant_network"
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
