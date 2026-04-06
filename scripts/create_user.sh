#!/bin/bash

set -e

# ===== 参数 =====
USER_ID=$1

if [ -z "$USER_ID" ]; then
  echo "Usage: $0 <user_id>"
  exit 1
fi

# ===== 可配置参数 =====
BASE_DIR="/data/docker/openclaw-public"
VERSION=${VERSION:-2026.3.28}
TZ=${TZ:-Asia/Shanghai}

USER_DIR="$BASE_DIR/users/$USER_ID"
LOG_FILE="$BASE_DIR/logs/scripts/create_user.log"
PORT_FILE="$BASE_DIR/ports.txt"

# ===== 日志函数 =====
log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] $1" | tee -a "$LOG_FILE"
}

# ===== 检查用户 =====
if [ -d "$USER_DIR" ]; then
  log "User $USER_ID already exists"
  exit 1
fi

# ===== 分配端口 =====
if [ ! -f "$PORT_FILE" ]; then
  echo "30000" > "$PORT_FILE"
fi

PORT=$(cat "$PORT_FILE")
NEXT_PORT=$((PORT + 1))
echo "$NEXT_PORT" > "$PORT_FILE"

log "Alloc port $PORT for user $USER_ID"

# ===== 创建目录 =====
mkdir -p "$USER_DIR"/{config,workspaces,workspace,skills,extensions}

# ===== 复制模板 =====
TEMPLATE="$BASE_DIR/templates/docker-compose.tpl.yml"
TARGET_COMPOSE="$USER_DIR/docker-compose.yml"

cp "$TEMPLATE" "$TARGET_COMPOSE"

# ===== 替换变量 =====
sed -i "s#{{USER_ID}}#$USER_ID#g" "$TARGET_COMPOSE"
sed -i "s#{{PORT}}#$PORT#g" "$TARGET_COMPOSE"
sed -i "s#{{VERSION}}#$VERSION#g" "$TARGET_COMPOSE"
sed -i "s#{{BASE_DIR}}#$BASE_DIR#g" "$TARGET_COMPOSE"
sed -i "s#{{TZ}}#$TZ#g" "$TARGET_COMPOSE"

# ===== 启动容器 =====
cd "$USER_DIR"
docker compose up -d

# ===== 输出 =====
log "User $USER_ID created successfully"
log "Port: $PORT"

echo ""
echo "=============================="
echo "SUCCESS"
echo "User: $USER_ID"
echo "Port: $PORT"
echo "URL: http://<your-host>:$PORT"
echo "=============================="
