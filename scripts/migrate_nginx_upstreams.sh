#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

source "$CONFIG_FILE"

NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:?Missing NGINX_USERS_CONF_DIR in config}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:?Missing NGINX_CONTAINER_NAME in config}"
NGINX_DISABLED_USERS_CONF_DIR="$NGINX_USERS_CONF_DIR/_disabled"
LEGACY_NGINX_DISABLED_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR}.disabled"

if [ ! -d "$NGINX_USERS_CONF_DIR" ]; then
  echo "[ERROR] Nginx user config directory not found: $NGINX_USERS_CONF_DIR" >&2
  exit 1
fi

declare -a config_files=()

find_user_config() {
  local user_id="$1"
  local candidates=(
    "$NGINX_USERS_CONF_DIR/${user_id}.conf"
    "$NGINX_DISABLED_USERS_CONF_DIR/${user_id}.conf"
    "$LEGACY_NGINX_DISABLED_USERS_CONF_DIR/${user_id}.conf"
  )
  local matches=()
  local candidate
  for candidate in "${candidates[@]}"; do
    if [ -f "$candidate" ]; then
      matches+=("$candidate")
    fi
  done
  if [ "${#matches[@]}" -eq 0 ]; then
    echo "[ERROR] Nginx config not found for user: $user_id" >&2
    return 1
  fi
  if [ "${#matches[@]}" -gt 1 ]; then
    echo "[ERROR] Multiple Nginx configs found for user $user_id: ${matches[*]}" >&2
    return 1
  fi
  config_files+=("${matches[0]}")
}

if [ "$#" -gt 0 ]; then
  scan_mode="explicit"
  for user_id in "$@"; do
    if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
      echo "[ERROR] Invalid user_id: $user_id" >&2
      exit 1
    fi
    find_user_config "$user_id"
  done
else
  scan_mode="bulk"
  config_dirs=(
    "$NGINX_USERS_CONF_DIR"
    "$NGINX_DISABLED_USERS_CONF_DIR"
    "$LEGACY_NGINX_DISABLED_USERS_CONF_DIR"
  )
  for config_dir in "${config_dirs[@]}"; do
    if [ -d "$config_dir" ]; then
      if [ "$config_dir" = "$NGINX_USERS_CONF_DIR" ]; then
        while IFS= read -r -d '' config_file; do
          config_files+=("$config_file")
        done < <(find "$config_dir" -maxdepth 1 -type f -name '*.conf' -print0)
      else
        while IFS= read -r -d '' config_file; do
          config_files+=("$config_file")
        done < <(find "$config_dir" -maxdepth 1 -type f -name '*.conf' -print0)
      fi
    fi
  done
fi

if [ "${#config_files[@]}" -eq 0 ]; then
  echo "[INFO] No Nginx user configs found"
  exit 0
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_root="$NGINX_USERS_CONF_DIR/.dynamic-upstream-backups"
mkdir -p "$backup_root"
backup_dir="$(mktemp -d "$backup_root/${timestamp}_XXXXXX")"

migration_active=1

restore_backups() {
  local category source_dir target_dir
  for category in active disabled legacy-disabled; do
    source_dir="$backup_dir/$category"
    case "$category" in
      active) target_dir="$NGINX_USERS_CONF_DIR" ;;
      disabled) target_dir="$NGINX_DISABLED_USERS_CONF_DIR" ;;
      legacy-disabled) target_dir="$LEGACY_NGINX_DISABLED_USERS_CONF_DIR" ;;
    esac
    if [ -d "$source_dir" ]; then
      mkdir -p "$target_dir"
      cp "$source_dir"/*.conf "$target_dir/"
    fi
  done
}

rollback_on_exit() {
  local exit_code="$1"
  trap - EXIT INT TERM
  if [ "$migration_active" -eq 1 ]; then
    restore_backups
    if find "$backup_dir" -type f -name '*.conf' -print -quit | grep -q .; then
      echo "[WARN] Migration interrupted or failed; restored original configs from $backup_dir" >&2
      docker exec "$NGINX_CONTAINER_NAME" nginx -t >/dev/null 2>&1 || true
      docker exec "$NGINX_CONTAINER_NAME" nginx -s reload >/dev/null 2>&1 || true
    fi
  fi
  exit "$exit_code"
}

trap 'rollback_on_exit $?' EXIT
trap 'exit 130' INT TERM

changed_count="$(python3 - \
  "$backup_dir" \
  "$NGINX_USERS_CONF_DIR" \
  "$NGINX_DISABLED_USERS_CONF_DIR" \
  "$LEGACY_NGINX_DISABLED_USERS_CONF_DIR" \
  "$scan_mode" \
  "${config_files[@]}" <<'PY'
import re
import shutil
import sys
from pathlib import Path

backup_dir = Path(sys.argv[1])
active_dir = Path(sys.argv[2])
disabled_dir = Path(sys.argv[3])
legacy_disabled_dir = Path(sys.argv[4])
scan_mode = sys.argv[5]
config_files = [Path(value) for value in sys.argv[6:]]
user_id_pattern = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
root_location = re.compile(
    r"(?ms)(?P<open>^\s*location\s+/\s*\{\s*\n)(?P<body>.*?)(?P<close>^\s*\})"
)
static_proxy = re.compile(
    r"(?P<prefix>proxy_pass\s+http://)"
    r"(?P<host>[A-Za-z0-9_.-]+):(?P<port>[0-9]+)(?P<uri>/[^;\s]*)?;"
)
ipv4_address = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
manager_static_proxy = re.compile(
    r"(?P<prefix>proxy_pass\s+http://)openclaw-manager-web:8080(?P<uri>/[^;\s]*)?;"
)
manager_upstream = (
    "upstream manager_web_backend {\n"
    "    zone manager_web_backend 64k;\n"
    "    resolver 127.0.0.11 valid=10s ipv6=off;\n"
    "    resolver_timeout 5s;\n"
    "    server openclaw-manager-web:8080 resolve;\n"
    "}\n\n"
)

updates = []
for path in config_files:
    text = path.read_text(encoding="utf-8")
    if path.name == "manager-web.conf":
        if (
            "server openclaw-manager-web:8080 resolve;" in text
            and "proxy_pass http://manager_web_backend" in text
        ):
            continue
        if not manager_static_proxy.search(text):
            if scan_mode == "bulk":
                continue
            raise SystemExit(f"Could not find manager-web upstream in {path}")
        updated = manager_upstream + manager_static_proxy.sub(
            lambda match: (
                f'{match.group("prefix")}manager_web_backend'
                f'{match.group("uri") or ""};'
            ),
            text,
        )
        updates.append((path, updated))
        continue

    user_id = path.stem
    if not user_id_pattern.fullmatch(user_id):
        raise SystemExit(f"Invalid user config filename: {path.name}")

    root_match = root_location.search(text)
    root_body = root_match.group("body") if root_match else ""
    if (
        "resolver 127.0.0.11" in root_body
        and f'set $openclaw_upstream "openclaw_{user_id}:18789";' in root_body
        and "proxy_pass http://$openclaw_upstream;" in root_body
        and not static_proxy.search(text)
    ):
        continue

    endpoints = {}
    for proxy_match in static_proxy.finditer(text):
        original_host = proxy_match.group("host")
        port = proxy_match.group("port")
        resolved_host = original_host
        if ipv4_address.fullmatch(original_host):
            if port != "18789":
                continue
            resolved_host = f"openclaw_{user_id}"
        key = (original_host, port)
        if key not in endpoints:
            index = len(endpoints) + 1
            endpoints[key] = {
                "name": f"agent_{user_id.replace('-', '_').replace('.', '_')}_{index}",
                "host": resolved_host,
                "port": port,
            }

    if not endpoints:
        generic_dynamic = bool(
            "resolver 127.0.0.11" in text
            and re.search(r"server\s+[A-Za-z0-9_.-]+:[0-9]+\s+resolve;", text)
            and re.search(r"proxy_pass\s+http://[A-Za-z0-9_.-]+(?:[/;])", text)
        )
        if generic_dynamic:
            continue
        if scan_mode == "bulk":
            continue
        raise SystemExit(f"Could not find a supported container upstream in {path}")

    upstream_blocks = []
    for endpoint in endpoints.values():
        name = endpoint["name"]
        upstream_blocks.append(
            f"upstream {name} {{\n"
            f"    zone {name} 64k;\n"
            f"    resolver 127.0.0.11 valid=10s ipv6=off;\n"
            f"    resolver_timeout 5s;\n"
            f'    server {endpoint["host"]}:{endpoint["port"]} resolve;\n'
            f"}}\n"
        )

    def replace_proxy(match):
        endpoint = endpoints.get((match.group("host"), match.group("port")))
        if endpoint is None:
            return match.group(0)
        uri = match.group("uri") or ""
        return f'{match.group("prefix")}{endpoint["name"]}{uri};'

    updated = "\n".join(upstream_blocks) + "\n" + static_proxy.sub(replace_proxy, text)

    updates.append((path, updated))

backup_categories = {
    active_dir: "active",
    disabled_dir: "disabled",
    legacy_disabled_dir: "legacy-disabled",
}
for path, updated in updates:
    try:
        category = backup_categories[path.parent]
    except KeyError:
        raise SystemExit(f"Unsupported Nginx config location: {path}")
    category_dir = backup_dir / category
    category_dir.mkdir(parents=True, exist_ok=True)
    backup_path = category_dir / path.name
    if backup_path.exists():
        raise SystemExit(f"Duplicate Nginx config backup target: {backup_path}")
    shutil.copy2(path, backup_path)
    path.write_text(updated, encoding="utf-8")

print(len(updates))
PY
)"

if [ "$changed_count" -eq 0 ]; then
  migration_active=0
  trap - EXIT INT TERM
  rmdir "$backup_dir"
  echo "[INFO] All Nginx upstreams already use Docker DNS"
  exit 0
fi

if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
  echo "[ERROR] Nginx configuration test failed" >&2
  exit 1
fi

if ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
  echo "[ERROR] Nginx reload failed" >&2
  exit 1
fi

migration_active=0
trap - EXIT INT TERM
echo "[INFO] Migrated $changed_count Nginx config(s) to Docker DNS upstreams"
echo "[INFO] Backup: $backup_dir"
