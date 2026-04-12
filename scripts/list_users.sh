#!/bin/bash

BASE_DIR="/data/docker/openclaw-public"
USERS_DIR="$BASE_DIR/users"
DELETED_DIR="$BASE_DIR/deleted"

echo ""
echo "=============================="
echo " OpenClaw User Manager"
echo "=============================="

# ===== 活跃用户 =====
echo ""
echo "[ACTIVE USERS]"
echo "------------------------------"

for dir in "$USERS_DIR"/*; do
  [ -d "$dir" ] || continue

  USER_ID=$(basename "$dir")

  # 解析端口
  PORT=$(grep "127.0.0.1" "$dir/docker-compose.yml" 2>/dev/null | sed -E 's/.*:(.*):18789/\1/')

  # 检查容器状态
  if docker ps | grep -q "openclaw_$USER_ID"; then
    STATUS="RUNNING"
  else
    STATUS="STOPPED"
  fi

  echo "User: $USER_ID"
  echo "Port: $PORT"
  echo "Status: $STATUS"
  echo "URL: http://<服务器IP>:$PORT"
  echo "------------------------------"
done

# ===== 已删除用户 =====
echo ""
echo "[RECYCLE BIN]"
echo "------------------------------"

for dir in "$DELETED_DIR"/*; do
  [ -d "$dir" ] || continue

  NAME=$(basename "$dir")

  echo "Deleted: $NAME"
done

echo "=============================="
