#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"
source "$SCRIPT_DIR/lib_tenant_network.sh"

OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"
MODEL_PROXY_CONTAINER_NAME="${MODEL_PROXY_CONTAINER_NAME:-openclaw-model-proxy}"
USER_CONTAINER_PREFIX="${USER_CONTAINER_PREFIX:-openclaw_}"

declare -a user_ids=()

if [ "$#" -gt 0 ]; then
  user_ids=("$@")
else
  while IFS= read -r container; do
    case "$container" in
      "$USER_CONTAINER_PREFIX"*)
        user_ids+=("${container#"$USER_CONTAINER_PREFIX"}")
        ;;
    esac
  done < <(docker ps --format '{{.Names}}')
fi

if [ "${#user_ids[@]}" -eq 0 ]; then
  echo "[INFO] No running tenant containers require migration"
  exit 0
fi

for user_id in "${user_ids[@]}"; do
  if [[ ! "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "[ERROR] Invalid user id: $user_id" >&2
    exit 1
  fi

  user_dir="$OPENCLAW_PUBLIC_DIR/users/$user_id"
  compose_file="$user_dir/docker-compose.yml"
  service_id="$(printf '%s' "$user_id" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
  tenant_network="$(tenant_network_name "$service_id")"

  if [ ! -f "$compose_file" ]; then
    echo "[ERROR] Compose file missing for $user_id: $compose_file" >&2
    exit 1
  fi

  echo "[INFO] Migrating $user_id to $tenant_network"
  ensure_tenant_compose_network "$compose_file" "$tenant_network"
  (
    cd "$user_dir"
    docker compose up -d --force-recreate
  )
  connect_container_to_network "$NGINX_CONTAINER_NAME" "$tenant_network"
  connect_container_to_network "$MODEL_PROXY_CONTAINER_NAME" "$tenant_network"
done

connect_shared_services_to_tenant_networks \
  "$NGINX_CONTAINER_NAME" \
  "$MODEL_PROXY_CONTAINER_NAME"

legacy_users="$(
  docker ps --format '{{.Names}}' | while IFS= read -r container; do
    case "$container" in
      "$USER_CONTAINER_PREFIX"*)
        if container_has_network "$container" agent-net; then
          printf '%s\n' "$container"
        fi
        ;;
    esac
  done
)"

if [ -n "$legacy_users" ]; then
  echo "[ERROR] Tenant containers still attached to agent-net:" >&2
  echo "$legacy_users" >&2
  exit 1
fi

echo "[INFO] Tenant network migration completed"
