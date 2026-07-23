#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib_tenant_network.sh"

echo "==> Deploying services..."

cd "$MANAGER_DIR/services"
docker compose build

auth_backup_file="$(mktemp)"
trap 'rm -f "$auth_backup_file"' EXIT
MANAGER_AUTH_BACKUP_PATH_FILE="$auth_backup_file" bash "$SCRIPT_DIR/update_manager_auth.sh"
auth_backup_dir="$(cat "$auth_backup_file")"

if ! docker compose up -d --no-build; then
  actual_provider="$(docker inspect "${MANAGER_WEB_CONTAINER_NAME:-openclaw-manager-web}" \
    --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | awk -F= '$1 == "MANAGER_AUTH_PROVIDER" { print $2; exit }' || true)"
  actual_provider="${actual_provider:-nginx-basic}"
  if [ "$actual_provider" != "${MANAGER_AUTH_PROVIDER:-nginx-basic}" ]; then
    bash "$SCRIPT_DIR/update_manager_auth.sh" --restore "$auth_backup_dir" || true
    echo "[ERROR] Service deployment failed; previous Nginx authentication config restored" >&2
  else
    echo "[ERROR] Service deployment failed after manager-web switched provider; matching Nginx authentication config retained" >&2
  fi
  exit 1
fi

rm -f "$auth_backup_file"
trap - EXIT

connect_shared_services_to_tenant_networks \
  "${NGINX_CONTAINER_NAME:-openclaw-nginx}" \
  "${MODEL_PROXY_CONTAINER_NAME:-openclaw-model-proxy}"

bash "$SCRIPT_DIR/migrate_nginx_upstreams.sh"

echo "==> Services deployed successfully!"
