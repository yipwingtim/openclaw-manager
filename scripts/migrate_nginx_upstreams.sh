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

if [ ! -d "$NGINX_USERS_CONF_DIR" ]; then
  echo "[ERROR] Nginx user config directory not found: $NGINX_USERS_CONF_DIR" >&2
  exit 1
fi

declare -a config_files=()

if [ "$#" -gt 0 ]; then
  for user_id in "$@"; do
    if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
      echo "[ERROR] Invalid user_id: $user_id" >&2
      exit 1
    fi
    config_file="$NGINX_USERS_CONF_DIR/${user_id}.conf"
    if [ ! -f "$config_file" ]; then
      echo "[ERROR] Nginx config not found: $config_file" >&2
      exit 1
    fi
    config_files+=("$config_file")
  done
else
  while IFS= read -r -d '' config_file; do
    config_files+=("$config_file")
  done < <(find "$NGINX_USERS_CONF_DIR" -maxdepth 1 -type f -name '*.conf' -print0)
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
  local backups=()
  shopt -s nullglob
  backups=("$backup_dir"/*.conf)
  shopt -u nullglob
  if [ "${#backups[@]}" -gt 0 ]; then
    cp "${backups[@]}" "$NGINX_USERS_CONF_DIR/"
  fi
}

rollback_on_exit() {
  local exit_code="$1"
  trap - EXIT INT TERM
  if [ "$migration_active" -eq 1 ]; then
    restore_backups
    if compgen -G "$backup_dir/*.conf" >/dev/null; then
      echo "[WARN] Migration interrupted or failed; restored original configs from $backup_dir" >&2
      docker exec "$NGINX_CONTAINER_NAME" nginx -t >/dev/null 2>&1 || true
      docker exec "$NGINX_CONTAINER_NAME" nginx -s reload >/dev/null 2>&1 || true
    fi
  fi
  exit "$exit_code"
}

trap 'rollback_on_exit $?' EXIT
trap 'exit 130' INT TERM

changed_count="$(python3 - "$backup_dir" "${config_files[@]}" <<'PY'
import re
import shutil
import sys
from pathlib import Path

backup_dir = Path(sys.argv[1])
config_files = [Path(value) for value in sys.argv[2:]]
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

for path, updated in updates:
    shutil.copy2(path, backup_dir / path.name)
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
