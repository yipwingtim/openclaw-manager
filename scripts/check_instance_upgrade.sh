#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

USER_ID="${1:-}"

if [ -z "$USER_ID" ]; then
  echo "Usage: $0 <user_id>" >&2
  exit 1
fi

if ! [[ "$USER_ID" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
  echo "[ERROR] Invalid user_id: $USER_ID" >&2
  exit 1
fi

OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
NGINX_USERS_CONF_DIR="${NGINX_USERS_CONF_DIR:-/data/docker/nginx/conf}"
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"

USER_DIR="$OPENCLAW_PUBLIC_DIR/users/$USER_ID"
COMPOSE_FILE="$USER_DIR/docker-compose.yml"
CONFIG_JSON="$USER_DIR/config/openclaw.json"
NGINX_USER_CONF="$NGINX_USERS_CONF_DIR/${USER_ID}.conf"
CONTAINER_NAME="openclaw_${USER_ID}"

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

section() {
  echo
  echo "=============================="
  echo " $1"
  echo "=============================="
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  echo "[PASS] $*"
}

warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  echo "[WARN] $*"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "[FAIL] $*"
}

detect_port() {
  if [ ! -f "$NGINX_USER_CONF" ]; then
    return 0
  fi

  sed -nE 's/^[[:space:]]*listen[[:space:]]+([0-9]+)[[:space:]]+ssl;.*/\1/p' "$NGINX_USER_CONF" | head -n 1
}

json_value() {
  local path="$1"
  local expression="$2"

  python3 - "$path" "$expression" <<'PY' 2>/dev/null || true
import json
import sys

path, expression = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    current = data
    for key in expression.split("."):
        current = current[key]
    if isinstance(current, (dict, list)):
        print(json.dumps(current, ensure_ascii=False))
    else:
        print(current)
except Exception:
    print("")
PY
}

file_count() {
  local path="$1"
  if [ ! -d "$path" ]; then
    echo 0
    return
  fi
  find "$path" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' '
}

http_status() {
  local url="$1"
  curl -k -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "$url" 2>/dev/null || true
}

container_exec() {
  docker exec "$CONTAINER_NAME" "$@" 2>&1 || true
}

section "Instance"
echo "User: $USER_ID"
echo "User dir: $USER_DIR"
echo "Container: $CONTAINER_NAME"
echo "Generated at: $(date '+%Y-%m-%d %H:%M:%S')"

if [ -d "$USER_DIR" ]; then
  pass "User directory exists."
else
  fail "User directory missing: $USER_DIR"
fi

if [ -f "$COMPOSE_FILE" ]; then
  pass "Compose file exists."
  IMAGE="$(sed -nE 's/^[[:space:]]*image:[[:space:]]*(.+)[[:space:]]*$/\1/p' "$COMPOSE_FILE" | head -n 1)"
  if [ -n "$IMAGE" ]; then
    pass "Compose image: $IMAGE"
  else
    warn "Could not detect image from compose file."
  fi
else
  fail "Compose file missing: $COMPOSE_FILE"
fi

section "Container"
if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  STATUS="$(docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  HEALTH="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  IMAGE_NAME="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  echo "Status: $STATUS"
  echo "Health: $HEALTH"
  echo "Image:  $IMAGE_NAME"

  if [ "$STATUS" = "running" ]; then
    pass "Container is running."
  else
    fail "Container is not running."
  fi

  if [ "$HEALTH" = "healthy" ] || [ "$HEALTH" = "none" ]; then
    pass "Container health is acceptable."
  else
    fail "Container health is not acceptable: $HEALTH"
  fi

  VERSION_OUTPUT="$(container_exec openclaw --version | head -n 5)"
  if [ -n "$VERSION_OUTPUT" ]; then
    pass "OpenClaw version command returned output."
    echo "$VERSION_OUTPUT"
  else
    warn "OpenClaw version command returned no output."
  fi
else
  fail "Container not found."
fi

section "Persistent Data"
for relative_path in config skills extensions workspace workspaces uploads; do
  path="$USER_DIR/$relative_path"
  if [ -d "$path" ]; then
    pass "$relative_path exists ($(file_count "$path") top-level entries)."
  else
    warn "$relative_path missing."
  fi
done

CORE_FILES=(soul.md memory.md identity.md agents.md tools.md user.md heartbeat.md bootstrap.md)
FOUND_CORE=0
for filename in "${CORE_FILES[@]}"; do
  matches="$(find "$USER_DIR" -maxdepth 3 -type f -iname "$filename" 2>/dev/null | head -n 5 || true)"
  if [ -n "$matches" ]; then
    FOUND_CORE=$((FOUND_CORE + 1))
    pass "Core file found: $filename"
    echo "$matches"
  else
    warn "Core file not found: $filename"
  fi
done

if [ "$FOUND_CORE" -eq 0 ]; then
  warn "No core md files found under user directory."
fi

section "Config"
if [ -f "$CONFIG_JSON" ]; then
  pass "openclaw.json exists."
  TOKEN="$(json_value "$CONFIG_JSON" "gateway.auth.token")"
  if [ -n "$TOKEN" ]; then
    pass "Gateway token exists."
  else
    warn "Gateway token missing or not generated yet."
  fi
else
  fail "openclaw.json missing: $CONFIG_JSON"
fi

if docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  MODEL_PROVIDERS="$(container_exec openclaw config get models.providers | head -n 20)"
  if echo "$MODEL_PROVIDERS" | grep -Eq '[A-Za-z0-9_-]'; then
    pass "Model providers returned output."
    echo "$MODEL_PROVIDERS"
  else
    warn "Model providers missing or config command unsupported."
  fi

  PRIMARY_MODEL="$(container_exec openclaw config get agents.defaults.model.primary | head -n 5)"
  if echo "$PRIMARY_MODEL" | grep -Eq '[A-Za-z0-9_/.-]'; then
    pass "Primary model returned output: $PRIMARY_MODEL"
  else
    warn "Primary model missing or config command unsupported."
  fi
fi

section "Nginx"
PORT="$(detect_port)"
if [ -f "$NGINX_USER_CONF" ]; then
  pass "Nginx config exists."
else
  fail "Nginx config missing: $NGINX_USER_CONF"
fi

if [ -n "$PORT" ]; then
  pass "Nginx port detected: $PORT"
else
  fail "Could not detect nginx port."
fi

if [ -n "$PORT" ]; then
  ACCESS_URL="https://${PUBLIC_HOST}:${PORT}/"
  ADMIN_URL="https://${PUBLIC_HOST}:${PORT}/admin/"
  LOCAL_ACCESS_URL="https://127.0.0.1:${PORT}/"
  LOCAL_ADMIN_URL="https://127.0.0.1:${PORT}/admin/"
  echo "Access URL: $ACCESS_URL"
  echo "Admin URL:  $ADMIN_URL"

  ACCESS_STATUS="$(http_status "$LOCAL_ACCESS_URL")"
  ADMIN_STATUS="$(http_status "$LOCAL_ADMIN_URL")"

  if echo "$ACCESS_STATUS" | grep -Eq '^(200|302|401)$'; then
    pass "/ returned HTTP $ACCESS_STATUS"
  else
    warn "/ returned HTTP ${ACCESS_STATUS:-none}"
  fi

  if echo "$ADMIN_STATUS" | grep -Eq '^(200|302|401)$'; then
    pass "/admin/ returned HTTP $ADMIN_STATUS"
  else
    warn "/admin/ returned HTTP ${ADMIN_STATUS:-none}"
  fi
fi

section "Device CLI"
if docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  DEVICES_OUTPUT="$(container_exec openclaw devices list | head -n 40)"
  if [ -n "$DEVICES_OUTPUT" ]; then
    pass "Device list command returned output."
    echo "$DEVICES_OUTPUT"
  else
    warn "Device list command returned no output."
  fi
fi

section "Recent Logs"
if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  LOG_OUTPUT="$(docker logs --tail 80 "$CONTAINER_NAME" 2>&1 || true)"
  ERROR_LINES="$(echo "$LOG_OUTPUT" | grep -Ei 'error|fatal|panic|traceback|exception|unhandled' | tail -n 20 || true)"
  if [ -n "$ERROR_LINES" ]; then
    warn "Recent logs contain possible errors."
    echo "$ERROR_LINES"
  else
    pass "No obvious error keywords in recent logs."
  fi
fi

section "Summary"
echo "PASS: $PASS_COUNT"
echo "WARN: $WARN_COUNT"
echo "FAIL: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 2
fi

if [ "$WARN_COUNT" -gt 0 ]; then
  exit 1
fi

exit 0
