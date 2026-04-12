#!/bin/bash

set -e

USER_ID=$1

if [ -z "$USER_ID" ]; then
  echo "Usage: $0 <user_id>"
  exit 1
fi

BASE_DIR="/data/docker/openclaw-public"
USER_DIR="$BASE_DIR/users/$USER_ID"
DELETED_DIR="$BASE_DIR/deleted"

if [ ! -d "$USER_DIR" ]; then
  echo "[ERROR] User not found: $USER_ID"
  exit 1
fi

echo "[INFO] Stopping container..."
cd "$USER_DIR"
docker compose down || true

# 创建回收站目录
mkdir -p "$DELETED_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "[INFO] Moving user data to recycle bin..."
mv "$USER_DIR" "$DELETED_DIR/${USER_ID}_$TIMESTAMP"

echo "[INFO] User $USER_ID moved to recycle bin:"
echo "👉 $DELETED_DIR/${USER_ID}_$TIMESTAMP"

echo "[INFO] You can restore this user by moving it back:"
echo "👉 mv $DELETED_DIR/${USER_ID}_$TIMESTAMP $BASE_DIR/users/$USER_ID"
