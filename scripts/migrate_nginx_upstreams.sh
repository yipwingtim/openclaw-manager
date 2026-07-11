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
  for user_id in "$@"; do
    if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
      echo "[ERROR] Invalid user_id: $user_id" >&2
      exit 1
    fi
    find_user_config "$user_id"
  done
else
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
        done < <(find "$config_dir" -maxdepth 1 -type f -name '*.conf' ! -name 'manager-web.conf' -print0)
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
  "${config_files[@]}" <<'PY'
import re
import shutil
import sys
from pathlib import Path

backup_dir = Path(sys.argv[1])
active_dir = Path(sys.argv[2])
disabled_dir = Path(sys.argv[3])
legacy_disabled_dir = Path(sys.argv[4])
config_files = [Path(value) for value in sys.argv[5:]]
user_id_pattern = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
root_location = re.compile(
    r"(?ms)(?P<open>^\s*location\s+/\s*\{\s*\n)(?P<body>.*?)(?P<close>^\s*\})"
)
static_proxy = re.compile(
    r"(?m)^(?P<indent>\s*)proxy_pass\s+http://"
    r"(?:openclaw_[A-Za-z0-9_.-]+|(?:[0-9]{1,3}\.){3}[0-9]{1,3}):18789;[ \t]*$"
)

updates = []
for path in config_files:
    user_id = path.stem
    if not user_id_pattern.fullmatch(user_id):
        raise SystemExit(f"Invalid user config filename: {path.name}")

    text = path.read_text(encoding="utf-8")
    match = root_location.search(text)
    if not match:
        raise SystemExit(f"Could not find root location in {path}")

    body = match.group("body")
    if (
        "resolver 127.0.0.11" in body
        and f'set $openclaw_upstream "openclaw_{user_id}:18789";' in body
        and "proxy_pass http://$openclaw_upstream;" in body
    ):
        continue

    proxy_match = static_proxy.search(body)
    if not proxy_match:
        raise SystemExit(f"Could not find a supported OpenClaw upstream in {path}")

    indent = proxy_match.group("indent")
    replacement = (
        f"{indent}resolver 127.0.0.11 valid=10s ipv6=off;\n"
        f'{indent}set $openclaw_upstream "openclaw_{user_id}:18789";\n'
        f"{indent}proxy_pass http://$openclaw_upstream;"
    )
    updated_body = static_proxy.sub(replacement, body, count=1)
    updated = text[: match.start("body")] + updated_body + text[match.end("body") :]

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
  echo "[INFO] All Nginx user upstreams already use Docker DNS"
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
echo "[INFO] Migrated $changed_count Nginx user config(s) to Docker DNS upstreams"
echo "[INFO] Backup: $backup_dir"
