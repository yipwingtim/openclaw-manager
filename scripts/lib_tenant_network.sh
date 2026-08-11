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
  if [ "${OPENCLAW_INSTANCE_AUTH_MODE:-token}" = "trusted-proxy" ]; then
    command+=(--trusted-proxy)
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

tenant_proxy_ip() {
  local network="$1"
  docker network inspect "$network" --format '{{json .IPAM.Config}}' | \
    python3 -c 'import ipaddress,json,sys; cfg=json.load(sys.stdin); nets=[ipaddress.ip_network(x["Subnet"]) for x in cfg if x.get("Subnet") and x["Subnet"].count(":") == 0]; assert len(nets)==1, "tenant network must have one IPv4 subnet"; print(nets[0].network_address + 2)'
}

connect_container_to_network_at_ip() {
  local container="$1"
  local network="$2"
  local ip_address="$3"
  if ! docker inspect "$container" >/dev/null 2>&1; then
    return 0
  fi
  local current
  current="$(docker inspect "$container" --format "{{with index .NetworkSettings.Networks \"$network\"}}{{.IPAddress}}{{end}}" 2>/dev/null || true)"
  if [ "$current" = "$ip_address" ]; then
    return 0
  fi
  if [ -n "$current" ]; then
    echo "[ERROR] $container already uses $current on $network; trusted proxy requires $ip_address" >&2
    return 1
  fi
  docker network connect --ip "$ip_address" "$network" "$container"
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
  python3 - "$nginx_container" "$model_proxy_container" <<'PY'
import concurrent.futures
import json
import os
import subprocess
import sys

nginx, model_proxy = sys.argv[1:]
network_prefix = os.environ.get("OPENCLAW_TENANT_NETWORK_PREFIX", "openclaw-user") + "-"


def inspect(names):
    if not names:
        return {}
    result = subprocess.run(
        ["docker", "inspect", *names], capture_output=True, text=True, check=False
    )
    if not result.stdout.strip():
        return {}
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return {
        item["Name"].lstrip("/"): set(item["NetworkSettings"]["Networks"])
        for item in items
    }


result = subprocess.run(
    ["docker", "ps", "--format", "{{.Names}}"],
    capture_output=True,
    text=True,
    check=False,
)
if result.returncode != 0:
    raise SystemExit(result.returncode)

containers = result.stdout.splitlines()
container_networks = inspect(containers)
desired = set()
for container, networks in container_networks.items():
    tenant_networks = {name for name in networks if name.startswith(network_prefix)}
    if container.startswith(os.environ.get("USER_CONTAINER_PREFIX", "openclaw_")):
        desired.update((service, network) for service in (nginx, model_proxy) for network in tenant_networks)
    elif container.startswith("hermes_"):
        desired.update((model_proxy, network) for network in tenant_networks)
    elif container.startswith("evoscientist_") and not container.endswith(("-proxy", "-ingress")):
        desired.update((model_proxy, network) for network in tenant_networks)

existing_services = inspect([nginx, model_proxy])
desired = {(service, network) for service, network in desired if service in existing_services}
missing = desired
for _ in range(3):
    actual = inspect(sorted({service for service, _ in missing}))
    missing = {
        (service, network)
        for service, network in missing
        if network not in actual.get(service, set())
    }
    if not missing:
        break
    def connect(pair):
        service, network = pair
        labels = subprocess.run(
            ["docker", "network", "inspect", network, "--format", "{{json .Labels}}"],
            capture_output=True, text=True, check=False,
        )
        try:
            trusted_proxy = json.loads(labels.stdout or "null") or {}
            trusted_proxy = trusted_proxy.get("com.openclaw.trusted-proxy") == "true"
        except json.JSONDecodeError:
            trusted_proxy = False
        if service == nginx and trusted_proxy:
            subnet = subprocess.run(
                ["docker", "network", "inspect", network, "--format", "{{json .IPAM.Config}}"],
                capture_output=True, text=True, check=False,
            )
            try:
                import ipaddress
                configs = json.loads(subnet.stdout)
                ipv4 = [ipaddress.ip_network(x["Subnet"]) for x in configs if x.get("Subnet") and ":" not in x["Subnet"]]
                if len(ipv4) != 1:
                    return subprocess.CompletedProcess([], 1, stderr="invalid tenant IPv4 subnet")
                return subprocess.run(
                    ["docker", "network", "connect", "--ip", str(ipv4[0].network_address + 2), network, service],
                    capture_output=True, text=True, check=False,
                )
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                return subprocess.CompletedProcess([], 1, stderr="invalid tenant IPv4 subnet")
        return subprocess.run(
            ["docker", "network", "connect", network, service],
            capture_output=True, text=True, check=False,
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(connect, missing))

actual = inspect(sorted({service for service, _ in missing}))
missing = {
    (service, network)
    for service, network in missing
    if network not in actual.get(service, set())
}
if missing:
    for service, network in sorted(missing):
        print(f"[ERROR] Failed to connect {service} to {network}", file=sys.stderr)
    raise SystemExit(1)
PY
}
