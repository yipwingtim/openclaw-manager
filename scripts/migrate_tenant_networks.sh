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
dry_run=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    --)
      shift
      user_ids+=("$@")
      break
      ;;
    -*)
      echo "[ERROR] Unknown option: $1" >&2
      echo "Usage: $0 [--dry-run] [user_id ...]" >&2
      exit 1
      ;;
    *)
      user_ids+=("$1")
      shift
      ;;
  esac
done

if [ "${#user_ids[@]}" -eq 0 ]; then
  while IFS= read -r container; do
    case "$container" in
      "$USER_CONTAINER_PREFIX"*)
        user_ids+=("${container#"$USER_CONTAINER_PREFIX"}")
        ;;
    esac
  done < <(docker ps -a --format '{{.Names}}')
fi

if [ "${#user_ids[@]}" -eq 0 ]; then
  echo "[INFO] No tenant containers require migration"
  exit 0
fi

declare -a user_dirs=()
declare -a compose_files=()
declare -a container_names=()
declare -a tenant_networks=()
declare -a state_labels=()
declare -a running_states=()
declare -a paused_states=()

for user_id in "${user_ids[@]}"; do
  if [[ ! "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "[ERROR] Invalid user id: $user_id" >&2
    exit 1
  fi

  user_dir="$OPENCLAW_PUBLIC_DIR/users/$user_id"
  compose_file="$user_dir/docker-compose.yml"
  container_name="${USER_CONTAINER_PREFIX}${user_id}"
  service_id="$(printf '%s' "$user_id" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
  tenant_network="$(tenant_network_name "$user_id")"
  was_running=false
  was_paused=false

  if [ ! -f "$compose_file" ]; then
    echo "[ERROR] Compose file missing for $user_id: $compose_file" >&2
    exit 1
  fi
  validate_tenant_compose_network "$compose_file" "$tenant_network"

  if docker inspect "$container_name" >/dev/null 2>&1; then
    was_running="$(docker inspect -f '{{.State.Running}}' "$container_name")"
    was_paused="$(docker inspect -f '{{.State.Paused}}' "$container_name")"
  fi

  if [ "$was_paused" = "true" ]; then
    state_label="paused"
  elif [ "$was_running" = "true" ]; then
    state_label="running"
  else
    state_label="stopped"
  fi

  user_dirs+=("$user_dir")
  compose_files+=("$compose_file")
  container_names+=("$container_name")
  tenant_networks+=("$tenant_network")
  state_labels+=("$state_label")
  running_states+=("$was_running")
  paused_states+=("$was_paused")
done

if [ "$dry_run" = "true" ]; then
  echo "[INFO] Running tenant network migration preflight (dry-run, no changes)"
  if ! network_plan_output="$(plan_tenant_networks "${tenant_networks[@]}")"; then
    echo "[ERROR] Tenant network migration preflight failed; no changes were made" >&2
    exit 1
  fi
else
  echo "[INFO] Running tenant network migration preflight for ${#user_ids[@]} tenant(s)"
  if ! network_plan_output="$(prepare_tenant_networks "${tenant_networks[@]}")"; then
    echo "[ERROR] Tenant network migration preflight failed; no Compose or container changes were made" >&2
    exit 1
  fi
fi

declare -a network_plan=()
mapfile -t network_plan <<< "$network_plan_output"
if [ "${#network_plan[@]}" -ne "${#user_ids[@]}" ]; then
  echo "[ERROR] Network planner returned ${#network_plan[@]} rows for ${#user_ids[@]} tenants" >&2
  exit 1
fi

for index in "${!user_ids[@]}"; do
  IFS=$'\t' read -r planned_network planned_subnet planned_action <<< "${network_plan[$index]}"
  if [ "$planned_network" != "${tenant_networks[$index]}" ]; then
    echo "[ERROR] Network planner order mismatch: expected ${tenant_networks[$index]}, got $planned_network" >&2
    exit 1
  fi
  printf '[PLAN] user=%s state=%s network=%s subnet=%s action=%s\n' \
    "${user_ids[$index]}" "${state_labels[$index]}" "$planned_network" "$planned_subnet" "$planned_action"
done

if [ "$dry_run" = "true" ]; then
  echo "[INFO] Dry-run completed; no Compose files, Docker networks, or containers were changed"
  exit 0
fi

for index in "${!user_ids[@]}"; do
  user_id="${user_ids[$index]}"
  user_dir="${user_dirs[$index]}"
  compose_file="${compose_files[$index]}"
  container_name="${container_names[$index]}"
  tenant_network="${tenant_networks[$index]}"
  was_running="${running_states[$index]}"
  was_paused="${paused_states[$index]}"

  echo "[INFO] Migrating $user_id to $tenant_network (state=${state_labels[$index]})"
  ensure_tenant_compose_network "$compose_file" "$tenant_network"

  if [ "$was_paused" = "true" ]; then
    docker unpause "$container_name"
  fi

  if [ "$was_running" = "true" ]; then
    if ! (
      cd "$user_dir"
      docker compose up -d --force-recreate
    ); then
      [ "$was_paused" = "true" ] && docker pause "$container_name" >/dev/null 2>&1 || true
      exit 1
    fi
  else
    (
      cd "$user_dir"
      docker compose create --force-recreate
    )
  fi

  if [ "$was_paused" = "true" ]; then
    docker pause "$container_name"
  fi

  connect_container_to_network "$NGINX_CONTAINER_NAME" "$tenant_network"
  connect_container_to_network "$MODEL_PROXY_CONTAINER_NAME" "$tenant_network"
done

connect_shared_services_to_tenant_networks \
  "$NGINX_CONTAINER_NAME" \
  "$MODEL_PROXY_CONTAINER_NAME"

legacy_users="$(
  docker ps -a --format '{{.Names}}' | while IFS= read -r container; do
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
