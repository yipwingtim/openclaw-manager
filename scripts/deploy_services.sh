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

docker compose up -d --build

bash "$SCRIPT_DIR/update_manager_auth.sh"

connect_shared_services_to_tenant_networks \
  "${NGINX_CONTAINER_NAME:-openclaw-nginx}" \
  "${MODEL_PROXY_CONTAINER_NAME:-openclaw-model-proxy}"

bash "$SCRIPT_DIR/migrate_nginx_upstreams.sh"

echo "==> Services deployed successfully!"
