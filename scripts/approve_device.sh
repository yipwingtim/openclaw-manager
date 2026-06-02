#!/bin/bash

set -euo pipefail

# ============================================================
# approve_device.sh
#
# 用途：
#   审批某个 OpenClaw 用户实例的 Control UI device pairing request。
#
# 用法：
#   ./scripts/approve_device.sh <user_id>
#   ./scripts/approve_device.sh <user_id> <requestId>
#   ./scripts/approve_device.sh <user_id> --latest
#
# 说明：
#   - 不传 requestId 时：只列出 pending requests 和最近访问日志，不自动审批多个请求。
#   - 只有 1 个 pending request 时：自动审批该 requestId。
#   - 传入 requestId 时：审批指定 requestId。
#   - 传入 --latest 时：显式审批最新 pending request。
# ============================================================

USER_ID="${1:-}"
TARGET_REQUEST_ID="${2:-}"

LIST_ONLY=false

if [ -z "$USER_ID" ]; then
  echo "Usage:"
  echo "  $0 <user_id>"
  echo "  $0 <user_id> <requestId>"
  echo "  $0 <user_id> --latest"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

BASE_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
NGINX_CONF_DIR="${NGINX_USERS_CONF_DIR:-/data/docker/nginx/conf}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-openclaw-nginx}"

USER_DIR="$BASE_DIR/users/$USER_ID"
CONTAINER_NAME="openclaw_${USER_ID}"
NGINX_USER_CONF="$NGINX_CONF_DIR/${USER_ID}.conf"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $*"
}

warn() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $*" >&2
}

fail() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*" >&2
  exit 1
}

approval_output_is_terminal() {
  echo "$1" | grep -Eiq 'approved|success|no pending|already approved|already paired|not found'
}

if [ ! -d "$USER_DIR" ]; then
  fail "User not found: $USER_ID ($USER_DIR)"
fi

if ! docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  fail "Container is not running: $CONTAINER_NAME"
fi

log "User: $USER_ID"
log "Container: $CONTAINER_NAME"

# 尝试从 nginx 用户配置中提取监听端口
PORT=""
if [ -f "$NGINX_USER_CONF" ]; then
  PORT="$(grep -E '^[[:space:]]*listen[[:space:]]+[0-9]+[[:space:]]+ssl' "$NGINX_USER_CONF" \
    | head -n 1 \
    | sed -E 's/^[[:space:]]*listen[[:space:]]+([0-9]+)[[:space:]]+ssl.*/\1/' || true)"
fi

if [ -n "$PORT" ]; then
  log "Nginx port: $PORT"
else
  warn "Could not detect nginx port from: $NGINX_USER_CONF"
fi

echo
echo "=============================="
echo " Recent Nginx access hints"
echo "=============================="

if [ -n "$PORT" ] && docker ps --format '{{.Names}}' | grep -Fxq "$NGINX_CONTAINER_NAME"; then
  # 尝试从 nginx access log 中查找最近该端口的访问记录
  # 不同 nginx log_format 可能字段不同，这里只做辅助展示。
  docker exec "$NGINX_CONTAINER_NAME" sh -lc "
    if [ -f /var/log/nginx/access.log ]; then
      grep -E '(:$PORT|\"Host:.*:$PORT| $PORT )' /var/log/nginx/access.log 2>/dev/null | tail -n 20 || true
    else
      echo 'No /var/log/nginx/access.log found in nginx container.'
    fi
  " || true
else
  warn "Skip nginx access hints: port or nginx container not available."
fi

echo
echo "=============================="
echo " Pending device requests"
echo "=============================="

LIST_OUTPUT="$(docker exec "$CONTAINER_NAME" openclaw devices list 2>&1 || true)"
echo "$LIST_OUTPUT"
CACHE_DIR="$BASE_DIR/users/$USER_ID"
CACHE_FILE="$CACHE_DIR/devices.txt"

mkdir -p "$CACHE_DIR"

{
  echo "Generated at: $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "$LIST_OUTPUT"
} > "$CACHE_FILE"

log "Device cache updated: $CACHE_FILE"



# 如果没有 Pending 段，说明当前没有待审批设备。
# openclaw devices list 可能只输出 Paired 表格，此时不要从已配对设备里误提取 id。
if ! echo "$LIST_OUTPUT" | grep -Eq '^Pending|Pending \('; then
  echo
  echo "=============================="
  echo " Decision"
  echo "=============================="
  log "No pending device request found."
  exit 0
fi

# 尝试提取 requestId。
# 兼容：
#   requestId: xxxxx
#   id: xxxxx
#   纯表格/文本里出现的 6 位以上字母数字短码
#
# 为避免误抓 deviceId / 时间戳，这里优先抓 requestId/id 字段。
REQUEST_IDS="$(
  echo "$LIST_OUTPUT" \
    | sed -nE 's/.*requestId[[:space:]:=]+([A-Za-z0-9._-]+).*/\1/p; s/.*request_id[[:space:]:=]+([A-Za-z0-9._-]+).*/\1/p' \
    | awk 'NF' \
    | sort -u
)"

if [ -z "$REQUEST_IDS" ]; then
  REQUEST_IDS="$(
    echo "$LIST_OUTPUT" \
      | sed -nE 's/.*(^|[[:space:]])id[[:space:]:=]+([A-Za-z0-9._-]+).*/\2/p' \
      | awk 'NF' \
      | sort -u
  )"
fi

REQUEST_COUNT="$(echo "$REQUEST_IDS" | awk 'NF' | wc -l | tr -d ' ')"

echo
echo "=============================="
echo " Decision"
echo "=============================="

if [ "$TARGET_REQUEST_ID" = "--list-only" ]; then
  LIST_ONLY=true
  TARGET_REQUEST_ID=""
fi

if [ "$TARGET_REQUEST_ID" = "--latest" ]; then
  log "Explicit --latest requested. Previewing latest pending request."
  LATEST_OUTPUT="$(docker exec "$CONTAINER_NAME" openclaw devices approve --latest 2>&1 || true)"
  echo "$LATEST_OUTPUT"

  LATEST_REQUEST_ID="$(
    echo "$LATEST_OUTPUT" \
      | sed -nE 's/.*Selected pending device request[[:space:]]+([A-Za-z0-9._-]+).*/\1/p' \
      | head -n 1
  )"

  if [ -z "$LATEST_REQUEST_ID" ]; then
    if approval_output_is_terminal "$LATEST_OUTPUT"; then
      log "Latest approval completed without explicit requestId."
      exit 0
    fi

    warn "Could not extract latest requestId from OpenClaw output. Falling back to legacy --latest approval."
    LEGACY_OUTPUT="$(docker exec "$CONTAINER_NAME" openclaw devices approve --latest 2>&1 || true)"
    echo "$LEGACY_OUTPUT"

    if approval_output_is_terminal "$LEGACY_OUTPUT"; then
      log "Latest approval completed via legacy --latest flow."
      exit 0
    fi

    fail "Could not approve latest device request."
  fi

  log "Approving latest requestId explicitly: $LATEST_REQUEST_ID"
  docker exec "$CONTAINER_NAME" openclaw devices approve "$LATEST_REQUEST_ID"
  log "Approved latest request for user: $USER_ID"
  exit 0
fi

if [ -n "$TARGET_REQUEST_ID" ]; then
  log "Approving specified requestId: $TARGET_REQUEST_ID"
  docker exec "$CONTAINER_NAME" openclaw devices approve "$TARGET_REQUEST_ID"
  log "Approved request for user: $USER_ID"
  exit 0
fi

if [ "$LIST_ONLY" = true ]; then
  log "List-only mode enabled. Skip approval."
  exit 0
fi

if [ "$REQUEST_COUNT" -eq 0 ]; then
  log "No pending device request found."
  exit 0
fi

if [ "$REQUEST_COUNT" -eq 1 ]; then
  ONLY_REQUEST_ID="$(echo "$REQUEST_IDS" | awk 'NF' | head -n 1)"
  log "Only one pending request found. Approving requestId: $ONLY_REQUEST_ID"
  docker exec "$CONTAINER_NAME" openclaw devices approve "$ONLY_REQUEST_ID"
  log "Approved request for user: $USER_ID"
  exit 0
fi

warn "Multiple pending requests found. Refuse to auto-approve."
echo
echo "Please approve one explicitly:"
echo "  $0 $USER_ID <requestId>"
echo
echo "Or explicitly approve latest:"
echo "  $0 $USER_ID --latest"
echo
exit 2
