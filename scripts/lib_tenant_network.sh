#!/usr/bin/env bash

tenant_network_name() {
  local service_id="$1"
  local prefix="${OPENCLAW_TENANT_NETWORK_PREFIX:-openclaw-user}"
  printf '%s-%s' "$prefix" "$service_id"
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

ensure_tenant_compose_network() {
  local compose_file="$1"
  local network="$2"

  python3 - "$compose_file" "$network" <<'PY'
import re
import sys
from pathlib import Path

compose_file = Path(sys.argv[1])
network = sys.argv[2]
text = compose_file.read_text(encoding="utf-8")

if "      - agent-net\n" in text:
    text = text.replace("      - agent-net\n", "      - tenant-net\n")
    text, replacements = re.subn(
        r"(?m)^  agent-net:\n    external: true\s*$",
        f"  tenant-net:\n    name: {network}",
        text,
        count=1,
    )
    if replacements != 1:
        raise SystemExit("Could not replace legacy agent-net definition")

if "      - tenant-net\n" not in text:
    raise SystemExit("Compose does not attach the user service to tenant-net")

compose_file.write_text(text, encoding="utf-8")
PY
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
