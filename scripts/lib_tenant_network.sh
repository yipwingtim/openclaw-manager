#!/usr/bin/env bash

TENANT_NETWORK_ALLOCATOR="${TENANT_NETWORK_ALLOCATOR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tenant_network_allocator.py}"

tenant_network_name() {
  local user_id="$1"
  local prefix="${OPENCLAW_TENANT_NETWORK_PREFIX:-openclaw-user}"
  local digest

  digest="$(printf '%s' "$user_id" | sha256sum | awk '{print $1}')" || return 1
  printf '%s-%s\n' "$prefix" "$digest"
}
run_tenant_network_allocator() {
  local mode="$1"
  shift
  local pool="${OPENCLAW_TENANT_SUBNET_POOL:-}"
  local subnet_prefix="${OPENCLAW_TENANT_SUBNET_PREFIX:-28}"
  local exclusions="${OPENCLAW_TENANT_NETWORK_EXCLUDE_CIDRS:-}"
  local lock_file="${OPENCLAW_TENANT_NETWORK_LOCK_FILE:-${OPENCLAW_PUBLIC_DIR:-/tmp}/tenant-network.lock}"
  local -a command
  local network
  local exclusion

  if [ -z "$pool" ]; then
    echo "[ERROR] OPENCLAW_TENANT_SUBNET_POOL must be configured before creating tenant networks" >&2
    return 1
  fi
  if [ "$#" -eq 0 ]; then
    echo "[ERROR] At least one tenant network is required" >&2
    return 1
  fi

  command=(python3 "$TENANT_NETWORK_ALLOCATOR" "$mode")
  for network in "$@"; do
    command+=(--network "$network")
  done
  command+=(--pool "$pool" --subnet-prefix "$subnet_prefix")
  if [ "$mode" != "plan" ]; then
    command+=(--lock-file "$lock_file")
  fi
  while IFS= read -r exclusion; do
    [ -n "$exclusion" ] && command+=(--exclude "$exclusion")
  done < <(printf '%s' "$exclusions" | tr ',' '\n' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')

  "${command[@]}"
}

plan_tenant_networks() {
  run_tenant_network_allocator plan "$@"
}

prepare_tenant_networks() {
  run_tenant_network_allocator prepare "$@"
}

ensure_tenant_network() {
  prepare_tenant_networks "$1"
}

remove_tenant_network_if_unused() {
  local network="$1"
  local attached
  local owner

  docker network inspect "$network" >/dev/null 2>&1 || return 0
  owner="$(docker network inspect -f '{{index .Labels "com.openclaw.tenant-network"}}' "$network" 2>/dev/null || true)"
  [ "$owner" = "$network" ] || return 0
  attached="$(docker network inspect -f '{{len .Containers}}' "$network")"
  if [ "$attached" = "0" ]; then
    docker network rm "$network" >/dev/null
  fi
}


container_has_network() {
  local container="$1"
  local network="$2"
  docker inspect "$container" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s\n" $name}}{{end}}' 2>/dev/null \
    | grep -Fxq "$network"
}

connect_container_to_network() {
  local container="$1"
  local network="$2"

  if ! docker inspect "$container" >/dev/null 2>&1; then
    return 0
  fi
  if ! container_has_network "$container" "$network"; then
    docker network connect "$network" "$container"
  fi
}

disconnect_container_from_network() {
  local container="$1"
  local network="$2"

  if ! docker inspect "$container" >/dev/null 2>&1; then
    return 0
  fi
  if container_has_network "$container" "$network"; then
    docker network disconnect "$network" "$container"
  fi
}

configure_tenant_compose_network() {
  local compose_file="$1"
  local network="$2"
  local mode="$3"

  python3 - "$compose_file" "$network" "$mode" <<'PY'
import re
import sys
from pathlib import Path

compose_file = Path(sys.argv[1])
network = sys.argv[2]
mode = sys.argv[3]
text = compose_file.read_text(encoding="utf-8")

if "      - agent-net\n" in text:
    text = text.replace("      - agent-net\n", "      - tenant-net\n")
    text, replacements = re.subn(
        r"(?m)^  agent-net:\n    external: true\s*$",
        f"  tenant-net:\n    name: {network}\n    external: true",
        text,
        count=1,
    )
    if replacements != 1:
        raise SystemExit("Could not replace legacy agent-net definition")

if "      - tenant-net\n" not in text:
    raise SystemExit("Compose does not attach the user service to tenant-net")

if mode == "write":
    text, replacements = re.subn(
        rf"(?m)^  tenant-net:\n    name: [^\n]+(?:\n    external: true)?\s*$",
        f"  tenant-net:\n    name: {network}\n    external: true",
        text,
        count=1,
    )
    if replacements != 1:
        raise SystemExit("Could not configure tenant-net as an external network")
    compose_file.write_text(text, encoding="utf-8")
elif mode != "check":
    raise SystemExit(f"Unsupported compose network mode: {mode}")
PY
}

validate_tenant_compose_network() {
  configure_tenant_compose_network "$1" "$2" check
}

ensure_tenant_compose_network() {
  configure_tenant_compose_network "$1" "$2" write
}

connect_shared_services_to_tenant_networks() {
  local nginx_container="$1"
  local model_proxy_container="$2"
  local user_container_prefix="${USER_CONTAINER_PREFIX:-openclaw_}"
  local network_prefix="${OPENCLAW_TENANT_NETWORK_PREFIX:-openclaw-user}"
  local containers
  local container
  local networks
  local network

  containers="$(docker ps -a --format '{{.Names}}' 2>/dev/null || true)"
  if [ -z "$containers" ]; then
    return 0
  fi

  while IFS= read -r container; do
    case "$container" in
      "$user_container_prefix"*) ;;
      *) continue ;;
    esac

    networks="$(
      docker inspect "$container" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s\n" $name}}{{end}}' 2>/dev/null || true
    )"
    while IFS= read -r network; do
      case "$network" in
        "$network_prefix"-*)
          connect_container_to_network "$nginx_container" "$network"
          connect_container_to_network "$model_proxy_container" "$network"
          ;;
      esac
    done <<EOF
$networks
EOF
  done <<EOF
$containers
EOF
}
