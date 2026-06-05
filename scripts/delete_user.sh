#!/bin/bash

set -e

# ===== 基础路径 =====
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE"
  exit 1
fi

source "$CONFIG_FILE"

# ===== 参数 =====
USER_ID=$1
SKIP_NGINX_RELOAD=0
USER_DIR_EXISTS=0

shift || true

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-nginx-reload)
      SKIP_NGINX_RELOAD=1
      shift
      ;;
    *)
      echo "Usage: $0 <user_id> [--skip-nginx-reload]"
      exit 1
      ;;
  esac
done

if [ -z "$USER_ID" ]; then
  echo "Usage: $0 <user_id> [--skip-nginx-reload]"
  exit 1
fi

# ===== 校验必要配置 =====
required_vars=(
  OPENCLAW_PUBLIC_DIR
  NGINX_COMPOSE_FILE
  NGINX_COMPOSE_DIR
  NGINX_USERS_CONF_DIR
  NGINX_CONTAINER_NAME
)

for var in "${required_vars[@]}"; do
  if [ -z "${!var}" ]; then
    echo "[ERROR] Missing config variable: $var"
    exit 1
  fi
done

BASE_DIR="$OPENCLAW_PUBLIC_DIR"
USER_DIR="$BASE_DIR/users/$USER_ID"
DELETED_DIR="$BASE_DIR/deleted"
NGINX_USER_CONF="$NGINX_USERS_CONF_DIR/${USER_ID}.conf"

if [ ! -d "$USER_DIR" ]; then
  echo "[WARN] User not found: $USER_ID"
else
  USER_DIR_EXISTS=1
fi

# ===== 识别 nginx 端口 =====
PORT=""

if [ -f "$NGINX_USER_CONF" ]; then
  PORT=$(grep -E '^[[:space:]]*listen[[:space:]]+[0-9]+[[:space:]]+ssl;' "$NGINX_USER_CONF" \
    | head -n1 \
    | sed -E 's/.*listen[[:space:]]+([0-9]+)[[:space:]]+ssl;.*/\1/')
fi

if [ -z "$PORT" ] && [ -f "$BASE_DIR/users.csv" ]; then
  if [ -r "$BASE_DIR/users.csv" ]; then
    PORT=$(awk -F',' -v user="$USER_ID" '$1==user && $4=="active" {print $2}' "$BASE_DIR/users.csv" | tail -n1)
  else
    PORT=$(sudo awk -F',' -v user="$USER_ID" '$1==user && $4=="active" {print $2}' "$BASE_DIR/users.csv" | tail -n1)
  fi
fi

if [ -z "$PORT" ]; then
  echo "[WARN] Could not detect port for user: $USER_ID"
  echo "[WARN] Will continue deleting user data, but nginx port mapping may need manual cleanup."
else
  echo "[INFO] Detected port: $PORT"
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RECYCLE_DIR="$DELETED_DIR/${USER_ID}_$TIMESTAMP"

echo "[INFO] Stopping OpenClaw container..."
if [ "$USER_DIR_EXISTS" -eq 1 ]; then
  cd "$USER_DIR"
  docker compose down || true
else
  echo "[INFO] Skip container stop: user directory not found"
fi

# ===== 创建回收站目录 =====
mkdir -p "$RECYCLE_DIR"

if [ "$USER_DIR_EXISTS" -eq 1 ]; then
  echo "[INFO] Moving user data to recycle bin..."
  mv "$USER_DIR" "$RECYCLE_DIR/user"
else
  echo "[INFO] Skip moving user data: directory already missing"
fi

# ===== 逻辑删除 nginx 用户配置 =====
if [ -f "$NGINX_USER_CONF" ]; then
  echo "[INFO] Moving nginx config to recycle bin..."
  mkdir -p "$RECYCLE_DIR/nginx"
  mv "$NGINX_USER_CONF" "$RECYCLE_DIR/nginx/${USER_ID}.conf"
else
  echo "[WARN] Nginx user config not found: $NGINX_USER_CONF"
fi

# ===== 从 nginx docker-compose.yml 移除端口映射 =====
if [ -n "$PORT" ] && [ -f "$NGINX_COMPOSE_FILE" ]; then
  echo "[INFO] Removing nginx port mapping: $PORT:$PORT"

  python3 - "$NGINX_COMPOSE_FILE" "$PORT" <<'PY'
import sys
from pathlib import Path

compose_file = Path(sys.argv[1])
port = sys.argv[2]

text = compose_file.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

patterns = {
    f'      - "{port}:{port}"\n',
    f"      - '{port}:{port}'\n",
    f"      - {port}:{port}\n",
}

new_lines = []
removed = False

for line in lines:
    if line in patterns:
        removed = True
        continue
    new_lines.append(line)

compose_file.write_text("".join(new_lines), encoding="utf-8")

if not removed:
    print(f"[WARN] Port mapping {port}:{port} not found in compose file")
PY

else
  echo "[WARN] Skip nginx compose port cleanup"
fi

if [ "$SKIP_NGINX_RELOAD" -eq 1 ]; then
  echo "[INFO] Skip nginx update/reload; caller must reload nginx after batch operations"
else
  # ===== 更新 nginx 容器并 reload =====
  echo "[INFO] Updating nginx container..."
  cd "$NGINX_COMPOSE_DIR"

  if ! docker compose up -d; then
    echo "[ERROR] Failed to update nginx container"
    exit 1
  fi

  echo "[INFO] Testing nginx configuration..."
  if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
    echo "[ERROR] Nginx configuration test failed"
    exit 1
  fi

  echo "[INFO] Reloading nginx..."
  if ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
    echo "[ERROR] Failed to reload nginx"
    exit 1
  fi
fi

# ===== 更新 users.csv 状态 =====
if [ -f "$BASE_DIR/users.csv" ]; then
  TMP_FILE="$(mktemp)"
  if [ -r "$BASE_DIR/users.csv" ] && [ -w "$BASE_DIR/users.csv" ]; then
    awk -F',' -v OFS=',' -v user="$USER_ID" '
      NR==1 { print; next }
      $1==user && $4=="active" { $4="deleted" }
      { print }
    ' "$BASE_DIR/users.csv" > "$TMP_FILE"
    mv "$TMP_FILE" "$BASE_DIR/users.csv"
  else
    sudo awk -F',' -v OFS=',' -v user="$USER_ID" '
      NR==1 { print; next }
      $1==user && $4=="active" { $4="deleted" }
      { print }
    ' "$BASE_DIR/users.csv" > "$TMP_FILE"
    sudo mv "$TMP_FILE" "$BASE_DIR/users.csv"
  fi
fi

metadata_args=(
  "$SCRIPT_DIR/metadata_cli.py"
  set-instance-status
  --user-id "$USER_ID"
  --status deleted
  --action delete_instance
  --message "deleted from delete_user.sh recycle=$RECYCLE_DIR"
)
if [ -n "$PORT" ]; then
  metadata_args+=(--port "$PORT")
fi
python3 "${metadata_args[@]}" || echo "[WARN] Metadata update failed for deleted user: $USER_ID"

echo ""
echo "=============================="
echo "DELETED"
echo "User: $USER_ID"
if [ -n "$PORT" ]; then
  echo "Released nginx port: $PORT"
fi
echo "Recycle path:"
echo "👉 $RECYCLE_DIR"
echo ""
echo "Restore notes:"
echo "1. Move user data back:"
echo "   mv $RECYCLE_DIR/user $BASE_DIR/users/$USER_ID"
echo "2. Move nginx config back:"
echo "   mv $RECYCLE_DIR/nginx/${USER_ID}.conf $NGINX_USERS_CONF_DIR/${USER_ID}.conf"
echo "3. Add nginx port mapping back to:"
echo "   $NGINX_COMPOSE_FILE"
echo "4. Run:"
echo "   cd $NGINX_COMPOSE_DIR && docker compose up -d"
echo "   docker exec $NGINX_CONTAINER_NAME nginx -t"
echo "   docker exec $NGINX_CONTAINER_NAME nginx -s reload"
echo ""
echo "Note: Basic Auth user is kept in .htpasswd and was not deleted."
echo "=============================="
