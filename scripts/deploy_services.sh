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

if ! docker network inspect instance-auth-net >/dev/null 2>&1; then
  docker network create instance-auth-net >/dev/null
fi

cd "$MANAGER_DIR/services"
services=("$@")
if [ "${#services[@]}" -gt 0 ]; then
  echo "==> Target services: ${services[*]}"
fi
docker compose build "${services[@]}"

if ! docker compose up -d --no-build --wait "${services[@]}"; then
  echo "[ERROR] New services did not become ready; Nginx configuration was not changed" >&2
  exit 1
fi

auth_backup_file="$(mktemp)"
trap 'rm -f "$auth_backup_file"' EXIT
MANAGER_AUTH_BACKUP_PATH_FILE="$auth_backup_file" bash "$SCRIPT_DIR/update_manager_auth.sh"

rm -f "$auth_backup_file"
trap - EXIT

connect_shared_services_to_tenant_networks \
  "${NGINX_CONTAINER_NAME:-openclaw-nginx}" \
  "${MODEL_PROXY_CONTAINER_NAME:-openclaw-model-proxy}"

bash "$SCRIPT_DIR/migrate_nginx_upstreams.sh"

echo "==> Services deployed successfully!"
