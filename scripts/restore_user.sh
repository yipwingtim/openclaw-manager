#!/bin/bash

set -e

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

# ===== 恢复目录 =====
echo "[INFO] Restoring user..."
mv "$TARGET" "$USERS_DIR/$USER_ID"

# ===== 启动容器 =====
echo "[INFO] Starting container..."
cd "$USERS_DIR/$USER_ID"
docker compose up -d

echo ""
echo "=============================="
echo "RESTORE SUCCESS"
echo "User: $USER_ID"
echo "=============================="
