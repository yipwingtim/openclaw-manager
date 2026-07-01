#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USER_ID=$1

if [ -z "$USER_ID" ]; then
  echo "Usage: $0 <user_id>"
  exit 1
fi

BASE_DIR="/data/docker/openclaw-public"
USERS_DIR="$BASE_DIR/users"
DELETED_DIR="$BASE_DIR/deleted"

# ===== 找最近的删除版本 =====
TARGET=$(ls -dt $DELETED_DIR/${USER_ID}_* 2>/dev/null | head -n 1)

if [ -z "$TARGET" ]; then
  echo "[ERROR] No deleted user found for: $USER_ID"
  exit 1
fi

echo "[INFO] Found backup: $TARGET"

# ===== 判断是否已有同名用户 =====
if [ -d "$USERS_DIR/$USER_ID" ]; then
  echo "[WARN] User already exists: $USER_ID"
  echo "👉 Please delete it first or use another name"
  exit 1
fi

# ===== 恢复 nginx 配置（从回收站的 nginx/ 子目录） =====
NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:-/data/docker/nginx/conf}"
NGINX_COMPOSE_DIR="${NGINX_COMPOSE_DIR:-/data/docker/nginx/compose}"
NGINX_COMPOSE_FILE="${NGINX_COMPOSE_FILE:-$NGINX_COMPOSE_DIR/docker-compose.yml}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"

RESTORED_CONF="$NGINX_USERS_CONF_DIR/${USER_ID}.conf"
if [ -f "$TARGET/nginx/${USER_ID}.conf" ]; then
  mv "$TARGET/nginx/${USER_ID}.conf" "$RESTORED_CONF"
  echo "[INFO] Restored nginx config: $RESTORED_CONF"
fi

# ===== 恢复用户目录（回收站结构: TARGET/user/ 是原来的用户目录） =====
echo "[INFO] Restoring user..."
mv "$TARGET/user" "$USERS_DIR/$USER_ID"
# 清理空的回收站目录
rmdir "$TARGET/nginx" 2>/dev/null || true
rmdir "$TARGET" 2>/dev/null || true

# ===== 检测端口并恢复 nginx 端口映射 =====
PORT=""
if [ -f "$RESTORED_CONF" ]; then
  PORT=$(grep -E '^[[:space:]]*listen[[:space:]]+[0-9]+[[:space:]]+ssl;' "$RESTORED_CONF" \
    | head -n1 \
    | sed -E 's/.*listen[[:space:]]+([0-9]+)[[:space:]]+ssl;.*/\1/')
fi

if [ -n "$PORT" ] && [ -f "$NGINX_COMPOSE_FILE" ]; then
  PORT_LINE="      - \"${PORT}:${PORT}\""
  if grep -qF "$PORT_LINE" "$NGINX_COMPOSE_FILE"; then
    echo "[INFO] Port mapping $PORT:$PORT already exists"
  else
    echo "[INFO] Restoring nginx port mapping: $PORT:$PORT"
    # 在 ports: 块的最后一行（volumes: 之前）插入
    sed -i "/^[[:space:]]*volumes:/i\\${PORT_LINE}" "$NGINX_COMPOSE_FILE"
  fi
else
  echo "[WARN] Could not detect port or compose file not found, skip port mapping restore"
fi

# ===== 启动容器 =====
echo "[INFO] Starting container..."
cd "$USERS_DIR/$USER_ID"
docker compose up -d

# ===== 重建 nginx 以应用端口映射并刷新 upstream DNS =====
if [ -n "$PORT" ] && [ -f "$NGINX_COMPOSE_FILE" ]; then
  echo "[INFO] Updating nginx container..."
  cd "$NGINX_COMPOSE_DIR"
  docker compose up -d
  # restart 而非 reload：容器重建后 IP 变化，reload 不会重新解析 upstream DNS
  docker restart "$NGINX_CONTAINER_NAME"
fi

python3 "$SCRIPT_DIR/metadata_cli.py" set-instance-status \
  --user-id "$USER_ID" \
  --status active \
  --action restore_instance \
  --message "restored from restore_user.sh backup=$TARGET" \
  || echo "[WARN] Metadata update failed for restored user: $USER_ID"

echo ""
echo "=============================="
echo "RESTORE SUCCESS"
echo "User: $USER_ID"
echo "=============================="
