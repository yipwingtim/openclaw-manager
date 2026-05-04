#!/bin/bash

set -e

USER_ID=$1
PUBLIC_HOST=$2

if [ -z "$USER_ID" ] || [ -z "$PUBLIC_HOST" ]; then
  echo "Usage: $0 <user_id> <server_ip_or_domain>"
  echo "Example: $0 zongheban 10.185.xxx.xxx"
  exit 1
fi

BASE_DIR="/data/docker/openclaw-public"
USER_DIR="$BASE_DIR/users/$USER_ID"
CONFIG_FILE="$USER_DIR/config/openclaw.json"
COMPOSE_FILE="$USER_DIR/docker-compose.yml"

if [ ! -d "$USER_DIR" ]; then
  echo "[ERROR] User not found: $USER_ID"
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] openclaw.json not found: $CONFIG_FILE"
  exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[ERROR] docker-compose.yml not found: $COMPOSE_FILE"
  exit 1
fi

PORT=$(grep -E '^[[:space:]]*-[[:space:]]*"?[0-9]+:18789"?' "$COMPOSE_FILE" \
  | head -n1 \
  | sed -E 's/.*"?([0-9]+):18789"?.*/\1/')

if [ -z "$PORT" ]; then
  echo "[ERROR] Could not detect port from $COMPOSE_FILE"
  exit 1
fi

python3 - "$CONFIG_FILE" "$PUBLIC_HOST" "$PORT" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
public_host = sys.argv[2]
port = sys.argv[3]

with config_path.open("r", encoding="utf-8") as f:
    data = json.load(f)

gateway = data.setdefault("gateway", {})
gateway["mode"] = "local"
gateway["bind"] = "lan"

control_ui = gateway.setdefault("controlUi", {})
origins = control_ui.setdefault("allowedOrigins", [])

wanted = [
    f"http://localhost:{port}",
    f"http://127.0.0.1:{port}",
    f"http://{public_host}:{port}",
    f"https://{public_host}:{port}",
]

for item in wanted:
    if item not in origins:
        origins.append(item)

with config_path.open("w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

echo "[INFO] Updated allowedOrigins for user: $USER_ID"
echo "[INFO] Port: $PORT"
echo "[INFO] Added:"
echo "  http://$PUBLIC_HOST:$PORT"
echo "  https://$PUBLIC_HOST:$PORT"

CONTAINER_NAME="openclaw_$USER_ID"

if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  echo "[INFO] Restarting container: $CONTAINER_NAME"
  docker restart "$CONTAINER_NAME" >/dev/null
else
  echo "[WARN] Container is not running: $CONTAINER_NAME"
  echo "[WARN] Start it manually if needed:"
  echo "  cd $USER_DIR && docker compose up -d"
fi

echo "[INFO] Done."
