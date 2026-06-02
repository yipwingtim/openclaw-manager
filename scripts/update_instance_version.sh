#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE" >&2
  exit 1
fi

source "$CONFIG_FILE"

OPENCLAW_PUBLIC_DIR="${OPENCLAW_PUBLIC_DIR:?Missing OPENCLAW_PUBLIC_DIR in config}"

USER_ID="${1:-}"
TARGET_VERSION="${2:-}"

if [ -z "$USER_ID" ] || [ -z "$TARGET_VERSION" ]; then
  echo "Usage: $0 <user_id> <version>" >&2
  echo "Example: $0 batchtest004 2026.5.26" >&2
  exit 1
fi

if ! [[ "$USER_ID" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
  echo "[ERROR] Invalid user_id: $USER_ID" >&2
  exit 1
fi

if ! [[ "$TARGET_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "[ERROR] Invalid version: $TARGET_VERSION" >&2
  exit 1
fi

USER_DIR="$OPENCLAW_PUBLIC_DIR/users/$USER_ID"
COMPOSE_FILE="$USER_DIR/docker-compose.yml"
CONTAINER_NAME="openclaw_${USER_ID}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$USER_DIR/backups/version-upgrades/$TIMESTAMP"
BACKUP_FILE="$BACKUP_DIR/docker-compose.yml"
PERSISTENT_BACKUP_DIR="$BACKUP_DIR/persistent-data"
PRE_CHECK_FILE="$BACKUP_DIR/pre-check.txt"
POST_CHECK_FILE="$BACKUP_DIR/post-check.txt"
CHECK_SCRIPT="$SCRIPT_DIR/check_instance_upgrade.sh"

if [ ! -d "$USER_DIR" ]; then
  echo "[ERROR] User directory not found: $USER_DIR" >&2
  exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[ERROR] Compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

CURRENT_IMAGE="$(sed -nE 's/^[[:space:]]*image:[[:space:]]*(ghcr\.io\/openclaw\/openclaw:[^[:space:]]+)[[:space:]]*$/\1/p' "$COMPOSE_FILE" | head -n1)"
TARGET_IMAGE="ghcr.io/openclaw/openclaw:$TARGET_VERSION"

if [ -z "$CURRENT_IMAGE" ]; then
  echo "[ERROR] Could not detect OpenClaw image in: $COMPOSE_FILE" >&2
  exit 1
fi

if [ "$CURRENT_IMAGE" = "$TARGET_IMAGE" ]; then
  echo "[INFO] Instance already uses target image: $TARGET_IMAGE"
  exit 0
fi

mkdir -p "$BACKUP_DIR"
cp "$COMPOSE_FILE" "$BACKUP_FILE"

for relative_path in config skills extensions; do
  if [ -e "$USER_DIR/$relative_path" ]; then
    mkdir -p "$PERSISTENT_BACKUP_DIR"
    cp -a "$USER_DIR/$relative_path" "$PERSISTENT_BACKUP_DIR/"
  fi
done

print_rollback() {
  echo ""
  echo "Rollback commands:"
  echo "  cp '$BACKUP_FILE' '$COMPOSE_FILE'"
  echo "  cd '$USER_DIR'"
  echo "  docker compose pull"
  echo "  docker compose up -d"
  echo ""
  echo "Persistent backup:"
  echo "  $PERSISTENT_BACKUP_DIR"
  echo "Restore persistent data only after reviewing differences."
}

if [ -x "$CHECK_SCRIPT" ]; then
  echo "[INFO] Running pre-upgrade check: $PRE_CHECK_FILE"
  set +e
  "$CHECK_SCRIPT" "$USER_ID" > "$PRE_CHECK_FILE" 2>&1
  PRE_CHECK_STATUS=$?
  set -e

  if [ "$PRE_CHECK_STATUS" -eq 2 ]; then
    echo "[ERROR] Pre-upgrade check failed. Review: $PRE_CHECK_FILE" >&2
    cat "$PRE_CHECK_FILE"
    print_rollback
    exit 1
  elif [ "$PRE_CHECK_STATUS" -eq 1 ]; then
    echo "[WARN] Pre-upgrade check has warnings. Review: $PRE_CHECK_FILE"
  else
    echo "[INFO] Pre-upgrade check passed: $PRE_CHECK_FILE"
  fi
else
  echo "[WARN] Check script not found or not executable: $CHECK_SCRIPT"
fi

echo "[INFO] User: $USER_ID"
echo "[INFO] Current image: $CURRENT_IMAGE"
echo "[INFO] Target image:  $TARGET_IMAGE"
echo "[INFO] Backup: $BACKUP_FILE"
echo "[INFO] Persistent backup: $PERSISTENT_BACKUP_DIR"

python3 - "$COMPOSE_FILE" "$TARGET_IMAGE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
target_image = sys.argv[2]
text = path.read_text(encoding="utf-8")
updated, count = re.subn(
    r"^(\s*image:\s*)ghcr\.io/openclaw/openclaw:[^\s]+(\s*)$",
    rf"\g<1>{target_image}\g<2>",
    text,
    count=1,
    flags=re.MULTILINE,
)

if count != 1:
    raise SystemExit(f"Could not update OpenClaw image in {path}")

path.write_text(updated, encoding="utf-8")
PY

cd "$USER_DIR"

echo "[INFO] Pulling target image..."
if ! docker compose pull; then
  echo "[ERROR] Failed to pull target image." >&2
  print_rollback
  exit 1
fi

echo "[INFO] Recreating instance container..."
if ! docker compose up -d; then
  echo "[ERROR] Failed to recreate instance container." >&2
  print_rollback
  exit 1
fi

echo "[INFO] Waiting for container state..."
for _ in $(seq 1 30); do
  STATUS="$(docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  HEALTH="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)"

  if [ "$STATUS" = "running" ] && { [ "$HEALTH" = "healthy" ] || [ "$HEALTH" = "none" ]; }; then
    if [ -x "$CHECK_SCRIPT" ]; then
      echo "[INFO] Running post-upgrade check: $POST_CHECK_FILE"
      set +e
      "$CHECK_SCRIPT" "$USER_ID" > "$POST_CHECK_FILE" 2>&1
      POST_CHECK_STATUS=$?
      set -e

      if [ "$POST_CHECK_STATUS" -eq 2 ]; then
        echo "[ERROR] Post-upgrade check failed. Review: $POST_CHECK_FILE" >&2
      elif [ "$POST_CHECK_STATUS" -eq 1 ]; then
        echo "[WARN] Post-upgrade check has warnings. Review: $POST_CHECK_FILE"
      else
        echo "[INFO] Post-upgrade check passed: $POST_CHECK_FILE"
      fi
    fi

    echo ""
    echo "=============================="
    echo "UPDATE SUCCESS"
    echo "User: $USER_ID"
    echo "Image: $TARGET_IMAGE"
    echo "Container: $CONTAINER_NAME"
    echo "Status: $STATUS"
    echo "Health: $HEALTH"
    echo "Pre-check: $PRE_CHECK_FILE"
    echo "Post-check: $POST_CHECK_FILE"
    echo "=============================="
    echo ""
    echo "Post-update checks:"
    echo "  ./scripts/approve_device.sh '$USER_ID' --list-only"
    echo "  ./scripts/set_model_provider.sh '$USER_ID'  # run if model provider config is missing"
    print_rollback
    exit 0
  fi

  if [ "$STATUS" = "exited" ] || [ "$STATUS" = "dead" ] || [ "$HEALTH" = "unhealthy" ]; then
    break
  fi

  sleep 2
done

echo "[ERROR] Container did not become ready: $CONTAINER_NAME" >&2
docker ps -a --filter "name=^${CONTAINER_NAME}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' || true
print_rollback
exit 1
