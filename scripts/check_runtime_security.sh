#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib_tenant_network.sh"

ERRORS=0
WARNINGS=0
declare -A ERROR_TYPES=()

ok() {
  echo "[OK] $*"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  echo "[WARN] $*"
}

error() {
  local type="other"
  case "$*" in
    "internal token header value does not match"*) type="nginx_internal_token_mismatch" ;;
    "internal token header missing"*) type="nginx_internal_token_missing" ;;
    "model allowlist is empty"*) type="model_allowlist_empty" ;;
    "model allowlist missing"*) type="model_allowlist_missing" ;;
    *"not attached to manager-net"*) type="manager_network_missing" ;;
    *"attached to manager-net"*) type="tenant_attached_to_manager_network" ;;
    *"attached to shared agent-net"*) type="tenant_attached_to_shared_network" ;;
    *"missing tenant network"*) type="tenant_network_missing" ;;
    *"can reach cloud metadata endpoint"*) type="metadata_endpoint_reachable" ;;
    *"can reach cloud metadata endpoint"*) type="metadata_endpoint_reachable" ;;
    "docker command not found") type="docker_missing" ;;
    "config missing"*) type="config_missing" ;;
    "Nginx conf dir missing"*) type="nginx_conf_dir_missing" ;;
    "OPENCLAW_INTERNAL_TOKEN is empty"*) type="internal_token_missing" ;;
  esac
  ERRORS=$((ERRORS + 1))
  ERROR_TYPES["$type"]=$(( ${ERROR_TYPES["$type"]:-0} + 1 ))
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

metadata_endpoint_reachable() {
  local container="$1"
  local endpoint="$2"
  docker exec "$container" sh -lc '
    endpoint="$1"
    if command -v curl >/dev/null 2>&1; then
      curl -fsS --max-time 2 -o /dev/null "$endpoint"
    elif command -v wget >/dev/null 2>&1; then
      wget -q -T 2 -O /dev/null "$endpoint"
    else
      exit 2
    fi
  ' sh "$endpoint" >/dev/null 2>&1
}

metadata_endpoint_reachable() {
  local container="$1"
  local endpoint="$2"
  docker exec "$container" sh -lc '
    endpoint="$1"
    if command -v curl >/dev/null 2>&1; then
      curl -fsS --max-time 2 -o /dev/null "$endpoint"
    elif command -v wget >/dev/null 2>&1; then
      wget -q -T 2 -O /dev/null "$endpoint"
    else
      exit 2
    fi
  ' sh "$endpoint" >/dev/null 2>&1
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
MANAGER_USER_WEB_CONTAINER_NAME="${MANAGER_USER_WEB_CONTAINER_NAME:-openclaw-manager-user-web}"
MANAGER_ADMIN_WEB_CONTAINER_NAME="${MANAGER_ADMIN_WEB_CONTAINER_NAME:-openclaw-manager-admin-web}"
MODEL_PROXY_CONTAINER_NAME="${MODEL_PROXY_CONTAINER_NAME:-openclaw-model-proxy}"
INSTANCE_AUTH_CONTAINER_NAME="${INSTANCE_AUTH_CONTAINER_NAME:-openclaw-instance-auth-proxy}"
MANAGER_CONTROL_CONTAINER_NAME="${MANAGER_CONTROL_CONTAINER_NAME:-openclaw-manager-control}"
MANAGER_EXECUTOR_CONTAINER_NAME="${MANAGER_EXECUTOR_CONTAINER_NAME:-openclaw-manager-executor}"
MODEL_PROXY_TOKEN_DIR="${MODEL_PROXY_TOKEN_DIR:-$OPENCLAW_PUBLIC_DIR/model-proxy-tokens}"
OPENCLAW_TENANT_NETWORK_PREFIX="${OPENCLAW_TENANT_NETWORK_PREFIX:-openclaw-user}"
USER_CONTAINER_PREFIX="${USER_CONTAINER_PREFIX:-openclaw_}"
HERMES_AUTH_BRIDGE_CA_HOST_FILE="${HERMES_AUTH_BRIDGE_CA_HOST_FILE:-}"

mount_is_readonly() {
  local container="$1" destination="$2"
  docker inspect "$container" --format '{{range .Mounts}}{{println .Destination .Source .RW}}{{end}}' 2>/dev/null \
    | awk -v destination="$destination" '$1 == destination && $2 != "/dev/null" && $3 == "false" { found=1 } END { exit !found }'
}
for hermes_root in "$OPENCLAW_PUBLIC_DIR/hermes" "$OPENCLAW_PUBLIC_DIR/instances/hermes"; do
  [ -d "$hermes_root" ] || continue
  while IFS= read -r -d '' instance_dir; do
    echo "[INFO] Hermes data permissions are managed by the container: $instance_dir"
  done < <(find "$hermes_root" -mindepth 1 -maxdepth 1 -type d -print0)
done

if [ -n "${OPENCLAW_INTERNAL_TOKEN:-}" ]; then
  ok "OPENCLAW_INTERNAL_TOKEN is configured"
else
  error "OPENCLAW_INTERNAL_TOKEN is empty; manager-web internal token checks will be disabled"
fi

if [ -d "$NGINX_CONF_DIR" ]; then
  ok "Nginx conf dir exists: $NGINX_CONF_DIR"

  manager_proxy_files="$(find "$NGINX_CONF_DIR" -maxdepth 1 -type f -name '*.conf' -exec grep -l -e "openclaw-manager-user-web:8080" -e "openclaw-manager-admin-web:8080" -e "openclaw-manager-web:8080" {} + 2>/dev/null | sort || true)"
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
    warn "no Nginx conf proxies to split manager Web services"
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
  if mount_is_readonly "$MANAGER_CONTROL_CONTAINER_NAME" "${HERMES_AUTH_BRIDGE_SIGNING_KEY_FILE:-/run/secrets/hermes-auth-bridge-ed25519.pem}"; then
    ok "Manager control Hermes signing key mount is read-only and not /dev/null"
  else
    error "Manager control Hermes signing key mount is missing, writable, or /dev/null"
  fi
  if mount_is_readonly "$MANAGER_EXECUTOR_CONTAINER_NAME" "${HERMES_AUTH_BRIDGE_CA_FILE:-/run/secrets/hermes-auth-bridge-ca.crt}"; then
    ok "Manager executor Hermes CA mount is read-only and not /dev/null"
  else
    error "Manager executor Hermes CA mount is missing, writable, or /dev/null"
  fi
  if [ -n "${HERMES_AUTH_BRIDGE_ISSUER:-}" ] && [ -n "$HERMES_AUTH_BRIDGE_CA_HOST_FILE" ]; then
    if curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
      --cacert "$HERMES_AUTH_BRIDGE_CA_HOST_FILE" \
      "${HERMES_AUTH_BRIDGE_ISSUER%/}/jwks.json" >/dev/null; then
      ok "Hermes bridge JWKS is reachable over configured TLS"
    else
      error "Hermes bridge JWKS or TLS validation failed"
    fi
    token_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
      --connect-timeout 3 --max-time 10 \
      --cacert "$HERMES_AUTH_BRIDGE_CA_HOST_FILE" --data 'grant_type=readiness_probe' \
      "${HERMES_AUTH_BRIDGE_ISSUER%/}/token" || true)"
    if [ "$token_status" = 400 ]; then
      ok "Hermes bridge token endpoint rejected readiness probe as expected"
    else
      error "Hermes bridge token readiness probe returned HTTP ${token_status:-unavailable}; expected 400"
    fi
  else
    error "Hermes bridge issuer or CA host file is empty"
  fi
  if [ -n "${MANAGER_CONTROL_INSTANCE_AUTH_TOKEN:-}" ]; then
    if docker inspect "$INSTANCE_AUTH_CONTAINER_NAME" >/dev/null 2>&1; then
      ok "container exists: $INSTANCE_AUTH_CONTAINER_NAME"
      for network in manager-net instance-auth-net; do
        if container_has_network "$INSTANCE_AUTH_CONTAINER_NAME" "$network"; then
          ok "$INSTANCE_AUTH_CONTAINER_NAME is attached to $network"
        else
          error "$INSTANCE_AUTH_CONTAINER_NAME is not attached to $network"
        fi
      done
    else
      error "instance auth container not found: $INSTANCE_AUTH_CONTAINER_NAME"
    fi
    if docker inspect "$NGINX_CONTAINER_NAME" >/dev/null 2>&1; then
      if container_has_network "$NGINX_CONTAINER_NAME" instance-auth-net; then
        ok "$NGINX_CONTAINER_NAME is attached to instance-auth-net"
      else
        error "$NGINX_CONTAINER_NAME is not attached to instance-auth-net"
      fi
    fi
    evo_ingresses="$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep '^evoscientist_.*-ingress$' || true)"
    if [ -n "$evo_ingresses" ]; then
      while IFS= read -r container; do
        if container_has_network "$container" instance-auth-net; then
          ok "$container is attached to instance-auth-net"
        else
          error "$container is not attached to instance-auth-net"
        fi
        if container_has_network "$container" manager-net; then
          error "user ingress is attached to manager-net: $container"
        else
          ok "$container is not attached to manager-net"
        fi
      done <<EOF
$evo_ingresses
EOF
    fi
  fi
  for manager_container in "$MANAGER_USER_WEB_CONTAINER_NAME" "$MANAGER_ADMIN_WEB_CONTAINER_NAME"; do
    if docker inspect "$manager_container" >/dev/null 2>&1; then
      ok "container exists: $manager_container"
      if container_has_network "$manager_container" manager-net; then
        ok "$manager_container is attached to manager-net"
      else
        error "$manager_container is not attached to manager-net"
      fi
      if container_has_network "$manager_container" agent-net; then
        error "$manager_container is attached to agent-net"
      else
        ok "$manager_container is not attached to agent-net"
      fi
    else
      warn "container not found: $manager_container"
    fi
  done

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
      tenant_network="$(tenant_network_name "$user_id")"
      container_state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
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
      if [ "$container_state" = running ]; then
        if container_has_network "$NGINX_CONTAINER_NAME" "$tenant_network"; then
          ok "$NGINX_CONTAINER_NAME is attached to tenant network: $tenant_network"
          trusted_proxy_label="$(docker network inspect "$tenant_network" --format '{{index .Labels "com.openclaw.trusted-proxy"}}' 2>/dev/null || true)"
          if [ "$trusted_proxy_label" = true ]; then
            expected_proxy_ip="$(tenant_proxy_ip "$tenant_network" 2>/dev/null || true)"
            actual_proxy_ip="$(docker inspect "$NGINX_CONTAINER_NAME" --format "{{with index .NetworkSettings.Networks \"$tenant_network\"}}{{.IPAddress}}{{end}}" 2>/dev/null || true)"
            if [ -n "$expected_proxy_ip" ] && [ "$actual_proxy_ip" = "$expected_proxy_ip" ]; then
              ok "$NGINX_CONTAINER_NAME trusted proxy address matches: $tenant_network -> $actual_proxy_ip"
            else
              error "$NGINX_CONTAINER_NAME trusted proxy address mismatch: $tenant_network expected=$expected_proxy_ip actual=$actual_proxy_ip"
            fi
          fi
        else
          error "$NGINX_CONTAINER_NAME is missing tenant network: $tenant_network"
        fi
        if container_has_network "$MODEL_PROXY_CONTAINER_NAME" "$tenant_network"; then
          ok "$MODEL_PROXY_CONTAINER_NAME is attached to tenant network: $tenant_network"
        else
          error "$MODEL_PROXY_CONTAINER_NAME is missing tenant network: $tenant_network"
        fi
      else
        ok "skip shared-service tenant network checks for $container (state=$container_state)"
      fi
    done <<EOF
$user_containers
EOF
  else
    warn "no user containers found with prefix: $USER_CONTAINER_PREFIX"
  fi

  # Discover containers from OpenClaw-owned tenant networks, independent of adapter naming.
  metadata_containers="$({
    docker network ls -q --filter "label=com.openclaw.tenant-network" 2>/dev/null \
      | while IFS= read -r network; do
          docker network inspect "$network" \
            --format '{{range .Containers}}{{println .Name}}{{end}}' 2>/dev/null || true
        done
  } | sort -u)"
  if [ -n "$metadata_containers" ]; then
    while IFS= read -r container; do
      for metadata_endpoint in \
        "http://100.100.100.200/latest/meta-data/" \
        "http://169.254.169.254/latest/meta-data/"; do
        if metadata_endpoint_reachable "$container" "$metadata_endpoint"; then
          error "$container can reach cloud metadata endpoint: $metadata_endpoint"
        else
          ok "$container cannot reach cloud metadata endpoint: $metadata_endpoint"
        fi
      done
    done <<EOF
$metadata_containers
EOF
  fi

  if docker inspect "$NGINX_CONTAINER_NAME" >/dev/null 2>&1; then
    if docker exec "$NGINX_CONTAINER_NAME" sh -lc 'wget -S -O- -T 3 http://openclaw-manager-admin-web:8080/admin/instances 2>&1 | grep -q "403 FORBIDDEN"' >/dev/null 2>&1; then
      ok "manager-admin-web rejects direct internal request without token"
    else
      warn "could not verify 403 for direct internal admin request without token"
    fi
  fi
else
  error "docker command not found"
fi

echo "[SUMMARY] errors=$ERRORS warnings=$WARNINGS"
if [ "${#ERROR_TYPES[@]}" -gt 0 ]; then
  for type in "${!ERROR_TYPES[@]}"; do
    printf '[SUMMARY] error_type=%s count=%s\n' "$type" "${ERROR_TYPES[$type]}"
  done | sort
fi

if [ "$ERRORS" -gt 0 ]; then
  exit 1
fi

exit 0
