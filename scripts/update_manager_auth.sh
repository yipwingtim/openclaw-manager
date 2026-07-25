#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
TEMPLATE="$MANAGER_DIR/templates/nginx/manager-web.conf.tpl"
LIB_NGINX_AUTH="$SCRIPT_DIR/lib_nginx_auth.sh"
backup_path_file="${MANAGER_AUTH_BACKUP_PATH_FILE:-}"

[ -f "$CONFIG_FILE" ] || { echo "[ERROR] Config file not found: $CONFIG_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG_FILE"
# shellcheck disable=SC1090
source "$LIB_NGINX_AUTH"

MANAGER_AUTH_PROVIDER="${MANAGER_AUTH_PROVIDER:-nginx-basic}"
NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:?Missing NGINX_USERS_CONF_DIR}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"
target="$NGINX_USERS_CONF_DIR/manager-web.conf"
backup_root="$NGINX_USERS_CONF_DIR/.manager-auth-backups"
mkdir -p "$backup_root"

restore_instance_configs() {
  local restore_dir="$1"
  python3 - "$restore_dir/manifest.json" <<'PY'
import json
import shutil
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
if manifest.is_file():
    for item in json.loads(manifest.read_text(encoding="utf-8")):
        path = Path(item["path"])
        if item.get("existed", True):
            shutil.copy2(item["backup"], path)
        else:
            path.unlink(missing_ok=True)
PY
}

restore_all_configs() {
  local restore_dir="$1"
  if [ -f "$restore_dir/manager-web.conf" ]; then
    cp "$restore_dir/manager-web.conf" "$target"
  elif [ -f "$restore_dir/manager-web.missing" ]; then
    rm -f "$target"
  fi
  restore_instance_configs "$restore_dir"
}

if [ "${1:-}" = "--restore" ]; then
  restore_dir="${2:-}"
  case "$restore_dir" in
    "$backup_root"/*) ;;
    *) echo "[ERROR] Invalid manager auth backup directory" >&2; exit 1 ;;
  esac
  [ -d "$restore_dir" ] || { echo "[ERROR] Backup directory not found: $restore_dir" >&2; exit 1; }
  recovery_dir="$(mktemp -d "$backup_root/restore-$(date +%Y%m%d_%H%M%S).XXXXXX")"
  if [ -f "$target" ]; then
    cp "$target" "$recovery_dir/manager-web.conf"
  else
    touch "$recovery_dir/manager-web.missing"
  fi
  python3 - "$restore_dir/manifest.json" "$recovery_dir" <<'PY'
import json
import shutil
import sys
from pathlib import Path

source_manifest = Path(sys.argv[1])
recovery_dir = Path(sys.argv[2])
items = []
if source_manifest.is_file():
    for index, item in enumerate(json.loads(source_manifest.read_text(encoding="utf-8"))):
        path = Path(item["path"])
        backup = recovery_dir / "restore-current" / str(index)
        existed = path.is_file()
        if existed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        items.append({"path": str(path), "backup": str(backup), "existed": existed})
(recovery_dir / "manifest.json").write_text(json.dumps(items), encoding="utf-8")
PY
  if ! restore_all_configs "$restore_dir" \
    || ! docker exec "$NGINX_CONTAINER_NAME" nginx -t \
    || ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
    restore_all_configs "$recovery_dir"
    docker exec "$NGINX_CONTAINER_NAME" nginx -t >/dev/null 2>&1 \
      && docker exec "$NGINX_CONTAINER_NAME" nginx -s reload >/dev/null 2>&1 \
      || true
    echo "[ERROR] Could not restore manager authentication config; current config restored" >&2
    exit 1
  fi
  echo "[INFO] Restored manager authentication config: $restore_dir"
  exit 0
fi

NGINX_HTPASSWD_FILE_IN_CONTAINER="${NGINX_HTPASSWD_FILE_IN_CONTAINER:?Missing NGINX_HTPASSWD_FILE_IN_CONTAINER}"
NGINX_SSL_CERT="${NGINX_SSL_CERT:-/etc/nginx/certs/openclaw.crt}"
NGINX_SSL_KEY="${NGINX_SSL_KEY:-/etc/nginx/certs/openclaw.key}"
PUBLIC_HOST="${PUBLIC_HOST:?Missing PUBLIC_HOST}"
OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:?Missing OPENCLAW_PUBLIC_DIR}"
INSTANCE_ADMIN_GUARD="$(render_instance_admin_provider_guard "$MANAGER_AUTH_PROVIDER" "$PUBLIC_HOST" "${MANAGER_AUTH_TYPE:-}")" || {
  echo "[ERROR] Authentication provider is not implemented: $MANAGER_AUTH_PROVIDER" >&2
  exit 1
}
backup_dir="$(mktemp -d "$backup_root/$(date +%Y%m%d_%H%M%S).XXXXXX")"

MANAGER_EMERGENCY_LOCATION=""
MANAGER_INTERNAL_TOKEN_HEADER=""
if [ -n "${OPENCLAW_INTERNAL_TOKEN:-}" ]; then
  MANAGER_INTERNAL_TOKEN_HEADER="        proxy_set_header X-OpenClaw-Internal-Token \"$OPENCLAW_INTERNAL_TOKEN\";"
fi

case "$MANAGER_AUTH_PROVIDER" in
  nginx-basic)
    MANAGER_NGINX_AUTH_DIRECTIVES="    auth_basic \"OpenClaw Manager\";
    auth_basic_user_file $NGINX_HTPASSWD_FILE_IN_CONTAINER;"
    ;;
  local)
    MANAGER_NGINX_AUTH_DIRECTIVES="    auth_basic off;"
    MANAGER_EMERGENCY_LOCATION=""
    ;;
  *)
    case "${MANAGER_AUTH_TYPE:-}" in
      oidc|oauth2) ;;
      *) echo "[ERROR] MANAGER_AUTH_TYPE must be oidc or oauth2 for external providers" >&2; exit 1 ;;
    esac
    MANAGER_NGINX_AUTH_DIRECTIVES="    auth_basic off;"
    MANAGER_EMERGENCY_LOCATION="    location = /emergency/login {
        auth_basic \"OpenClaw Manager Emergency\";
        auth_basic_user_file $NGINX_HTPASSWD_FILE_IN_CONTAINER;
        proxy_pass http://manager_web_backend;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Remote-User \$remote_user;
${MANAGER_INTERNAL_TOKEN_HEADER:-}
    }"
    ;;
esac

export NGINX_SSL_CERT NGINX_SSL_KEY MANAGER_NGINX_AUTH_DIRECTIVES MANAGER_INTERNAL_TOKEN_HEADER MANAGER_EMERGENCY_LOCATION

tmp="$(mktemp "$NGINX_USERS_CONF_DIR/.manager-web.conf.XXXXXX")"
python3 - "$TEMPLATE" > "$tmp" <<'PY'
import os
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for key in ("NGINX_SSL_CERT", "NGINX_SSL_KEY", "MANAGER_NGINX_AUTH_DIRECTIVES", "MANAGER_INTERNAL_TOKEN_HEADER", "MANAGER_EMERGENCY_LOCATION"):
    text = text.replace("{{" + key + "}}", os.environ.get(key, ""))
print(text, end="")
PY

if [ -f "$target" ]; then
  cp "$target" "$target.bak.$(date +%Y%m%d_%H%M%S)"
  cp "$target" "$backup_dir/manager-web.conf"
else
  touch "$backup_dir/manager-web.missing"
fi

python3 - "$NGINX_USERS_CONF_DIR" "$OPENCLAW_PUBLIC_DIR" "$INSTANCE_ADMIN_GUARD" "$backup_dir" <<'PY'
import json
import shutil
import sys
from pathlib import Path

conf_dir = Path(sys.argv[1])
public_dir = Path(sys.argv[2])
guard = sys.argv[3]
backup_dir = Path(sys.argv[4])
locations = [
    ("active", conf_dir),
    ("disabled", conf_dir / "_disabled"),
    ("legacy-disabled", Path(f"{conf_dir}.disabled")),
]
updated = 0
modified = []
configs = []

for category, location in locations:
    if location.is_dir():
        configs.extend(
            (path, backup_dir / category / path.name)
            for path in location.glob("*.conf")
            if path.name != "manager-web.conf"
        )

deleted_dir = public_dir / "deleted"
if deleted_dir.is_dir():
    configs.extend(
        (path, backup_dir / "deleted" / path.relative_to(deleted_dir))
        for path in deleted_dir.rglob("*.conf")
        if path.parent.name == "nginx"
    )

try:
    for path, destination in configs:
        text = path.read_text(encoding="utf-8")
        if "location /admin/ {" not in text or "X-OpenClaw-User" not in text:
            continue
        lines = [
            line
            for line in text.splitlines()
            if "# managed-by-openclaw-manager-auth" not in line
        ]
        updated_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        if guard:
            updated_text = updated_text.replace(
                "    location /admin/ {\n",
                f"    location /admin/ {{\n{guard}\n",
                1,
            )
        if updated_text == text:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        modified.append((path, destination))
        path.write_text(updated_text, encoding="utf-8")
        updated += 1
    (backup_dir / "manifest.json").write_text(
        json.dumps(
            [{"path": str(path), "backup": str(source)} for path, source in modified]
        ),
        encoding="utf-8",
    )
except Exception:
    for path, source in modified:
        shutil.copy2(source, path)
    raise
print(f"[INFO] Updated legacy instance admin entry for {updated} config(s)")
PY

if ! mv "$tmp" "$target"; then
  restore_all_configs "$backup_dir"
  echo "[ERROR] Could not install manager-web Nginx config; instance configs restored" >&2
  exit 1
fi

if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
  restore_all_configs "$backup_dir"
  echo "[ERROR] Nginx validation failed; previous config restored" >&2
  exit 1
fi
if ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
  restore_all_configs "$backup_dir"
  docker exec "$NGINX_CONTAINER_NAME" nginx -t >/dev/null 2>&1 \
    && docker exec "$NGINX_CONTAINER_NAME" nginx -s reload >/dev/null 2>&1 \
    || true
  echo "[ERROR] Nginx reload failed; previous config restored" >&2
  exit 1
fi
[ -z "$backup_path_file" ] || printf '%s\n' "$backup_dir" > "$backup_path_file"
echo "[INFO] Backup: $backup_dir"
echo "[INFO] manager-web authentication provider configured: $MANAGER_AUTH_PROVIDER"
