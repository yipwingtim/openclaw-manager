#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

usage() {
  echo "Usage: $0 <user_id>"
}

log() {
  echo "[INFO] $*"
}

warn() {
  echo "[WARN] $*"
}

fail() {
  echo "[ERROR] $*"
  exit 1
}

if [ ! -f "$CONFIG_FILE" ]; then
  fail "Config file not found: $CONFIG_FILE"
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

USER_ID=$1
if [ -z "$USER_ID" ]; then
  usage
  exit 1
fi

if [[ ! "$USER_ID" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
  fail "Invalid user id: $USER_ID"
fi

required_vars=(
  OPENCLAW_PUBLIC_DIR
  NGINX_COMPOSE_FILE
  NGINX_COMPOSE_DIR
  NGINX_USERS_CONF_DIR
  NGINX_CONTAINER_NAME
)

for var in "${required_vars[@]}"; do
  if [ -z "${!var}" ]; then
    fail "Missing config variable: $var"
  fi
done

BASE_DIR="$OPENCLAW_PUBLIC_DIR"
USERS_DIR="$BASE_DIR/users"
DELETED_DIR="$BASE_DIR/deleted"
USERS_CSV="$BASE_DIR/users.csv"
USER_DIR="$USERS_DIR/$USER_ID"
NGINX_TARGET_CONF="$NGINX_USERS_CONF_DIR/${USER_ID}.conf"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

find_latest_recycle_dir() {
  local latest=""
  local candidate
  shopt -s nullglob
  for candidate in "$DELETED_DIR/${USER_ID}_"*; do
    if [ -d "$candidate" ] && { [ -z "$latest" ] || [ "$candidate" -nt "$latest" ]; }; then
      latest="$candidate"
    fi
  done
  shopt -u nullglob
  printf '%s' "$latest"
}

detect_port_from_nginx_conf() {
  local conf=$1
  if [ ! -f "$conf" ]; then
    return 0
  fi
  grep -E '^[[:space:]]*listen[[:space:]]+[0-9]+([[:space:]]+ssl)?;' "$conf" \
    | head -n1 \
    | sed -E 's/.*listen[[:space:]]+([0-9]+).*/\1/'
}

detect_port_from_users_csv() {
  if [ ! -f "$USERS_CSV" ]; then
    return 0
  fi
  awk -F',' -v user="$USER_ID" '
    NR==1 && $1=="user_id" { next }
    $1==user { port=$2 }
    END { if (port != "") print port }
  ' "$USERS_CSV"
}

compose_has_port_mapping() {
  local compose_file=$1
  local port=$2
  [ -f "$compose_file" ] || return 1
  grep -Eq "^[[:space:]]*-[[:space:]]*['\"]?${port}:${port}['\"]?[[:space:]]*$" "$compose_file"
}

add_nginx_port_mapping() {
  local compose_file=$1
  local port=$2
  python3 - "$compose_file" "$port" <<'PY'
import sys
from pathlib import Path

compose_file = Path(sys.argv[1])
port = sys.argv[2]
line_to_add = f'      - "{port}:{port}"\n'

text = compose_file.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

if any(line.strip().strip('"').strip("'") == f"- {port}:{port}" for line in lines):
    raise SystemExit(0)

out = []
in_nginx_service = False
in_ports = False
inserted = False

for line in lines:
    stripped = line.strip()

    if line.startswith("  nginx:"):
        in_nginx_service = True
        out.append(line)
        continue

    if in_nginx_service and line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":") and not line.startswith("  nginx:"):
        if in_ports and not inserted:
            out.append(line_to_add)
            inserted = True
        in_nginx_service = False
        in_ports = False
        out.append(line)
        continue

    if in_nginx_service and stripped == "ports:":
        in_ports = True
        out.append(line)
        continue

    if in_ports and line.startswith("    ") and not line.startswith("      -"):
        if not inserted:
            out.append(line_to_add)
            inserted = True
        in_ports = False
        out.append(line)
        continue

    out.append(line)

if in_ports and not inserted:
    out.append(line_to_add)
    inserted = True

if not inserted:
    raise SystemExit("Could not insert nginx port mapping. Please check nginx compose structure.")

compose_file.write_text("".join(out), encoding="utf-8")
PY
}

update_users_csv_status() {
  if [ ! -f "$USERS_CSV" ]; then
    warn "users.csv not found: $USERS_CSV"
    return 0
  fi

  local tmp_file
  tmp_file="$(mktemp)"
  local found_file
  found_file="$(mktemp)"

  awk -F',' -v OFS=',' -v user="$USER_ID" -v found_file="$found_file" '
    NR==1 { print; next }
    $1==user {
      found=1
      if ($4=="deleted") {
        $4="active"
      }
    }
    { print }
    END {
      if (found) {
        print "found" > found_file
      }
    }
  ' "$USERS_CSV" > "$tmp_file"

  mv "$tmp_file" "$USERS_CSV"
  if [ ! -s "$found_file" ]; then
    warn "users.csv has no row for restored user: $USER_ID"
  fi
  rm -f "$found_file"
}

rollback_nginx_config() {
  local backup_compose=$1
  local moved_nginx_conf=$2
  local source_nginx_conf=$3
  warn "Rolling back nginx compose and user config"
  if [ -f "$backup_compose" ]; then
    cp "$backup_compose" "$NGINX_COMPOSE_FILE"
  fi
  if [ -n "$moved_nginx_conf" ] && [ -f "$moved_nginx_conf" ]; then
    mkdir -p "$(dirname "$source_nginx_conf")"
    mv "$moved_nginx_conf" "$source_nginx_conf"
  fi
  cd "$NGINX_COMPOSE_DIR"
  docker compose up -d || warn "Failed to re-apply nginx compose rollback"
}

RECYCLE_DIR="$(find_latest_recycle_dir)"
if [ -z "$RECYCLE_DIR" ]; then
  fail "No deleted user found for: $USER_ID"
fi

log "Found backup: $RECYCLE_DIR"

if [ -d "$RECYCLE_DIR/user" ]; then
  RECYCLE_USER_DIR="$RECYCLE_DIR/user"
  RECYCLE_NGINX_CONF="$RECYCLE_DIR/nginx/${USER_ID}.conf"
  RECYCLE_LAYOUT="current"
elif [ -f "$RECYCLE_DIR/docker-compose.yml" ]; then
  RECYCLE_USER_DIR="$RECYCLE_DIR"
  RECYCLE_NGINX_CONF=""
  RECYCLE_LAYOUT="legacy"
  warn "Using legacy recycle layout without embedded user/ directory: $RECYCLE_DIR"
else
  fail "Invalid recycle layout: missing user/docker-compose.yml or legacy docker-compose.yml in $RECYCLE_DIR"
fi

if [ -d "$USER_DIR" ]; then
  fail "User already exists: $USER_ID"
fi
if [ -e "$NGINX_TARGET_CONF" ]; then
  fail "Nginx config already exists: $NGINX_TARGET_CONF"
fi
if [ ! -f "$RECYCLE_USER_DIR/docker-compose.yml" ]; then
  fail "Recycle user compose missing: $RECYCLE_USER_DIR/docker-compose.yml"
fi
if [ "$RECYCLE_LAYOUT" = "current" ] && [ ! -f "$RECYCLE_NGINX_CONF" ]; then
  fail "Recycle nginx config missing: $RECYCLE_NGINX_CONF"
fi
if [ ! -f "$NGINX_COMPOSE_FILE" ]; then
  fail "Nginx compose file not found: $NGINX_COMPOSE_FILE"
fi
if [ ! -d "$NGINX_COMPOSE_DIR" ]; then
  fail "Nginx compose dir not found: $NGINX_COMPOSE_DIR"
fi

PORT=""
if [ -n "$RECYCLE_NGINX_CONF" ] && [ -f "$RECYCLE_NGINX_CONF" ]; then
  PORT="$(detect_port_from_nginx_conf "$RECYCLE_NGINX_CONF")"
fi
if [ -z "$PORT" ]; then
  PORT="$(detect_port_from_users_csv)"
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  fail "Could not detect restore port for user: $USER_ID"
fi

if compose_has_port_mapping "$NGINX_COMPOSE_FILE" "$PORT"; then
  fail "Nginx compose already contains port mapping: $PORT:$PORT"
fi

BACKUP_NAME="restore-backup-$TIMESTAMP"
BACKUP_DIR="$RECYCLE_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_DIR"
cp "$NGINX_COMPOSE_FILE" "$BACKUP_DIR/$(basename "$NGINX_COMPOSE_FILE")"
BACKUP_COMPOSE_FILE="$BACKUP_DIR/$(basename "$NGINX_COMPOSE_FILE")"
log "Backed up nginx compose: $BACKUP_COMPOSE_FILE"

log "Restoring user directory..."
mkdir -p "$USERS_DIR"
if [ "$RECYCLE_LAYOUT" = "current" ]; then
  mv "$RECYCLE_USER_DIR" "$USER_DIR"
else
  mv "$RECYCLE_DIR" "$USER_DIR"
  BACKUP_DIR="$USER_DIR/$BACKUP_NAME"
  BACKUP_COMPOSE_FILE="$BACKUP_DIR/$(basename "$NGINX_COMPOSE_FILE")"
fi

MOVED_NGINX_CONF=""
if [ -n "$RECYCLE_NGINX_CONF" ] && [ -f "$RECYCLE_NGINX_CONF" ]; then
  log "Restoring nginx user config..."
  mkdir -p "$NGINX_USERS_CONF_DIR"
  mv "$RECYCLE_NGINX_CONF" "$NGINX_TARGET_CONF"
  MOVED_NGINX_CONF="$NGINX_TARGET_CONF"
else
  warn "Recycle nginx config not found; restore will start container but nginx access may require manual config restore"
fi

log "Restoring nginx port mapping: $PORT:$PORT"
add_nginx_port_mapping "$NGINX_COMPOSE_FILE" "$PORT"

log "Starting OpenClaw container..."
cd "$USER_DIR"
docker compose up -d

log "Updating nginx container..."
cd "$NGINX_COMPOSE_DIR"
docker compose up -d

log "Testing nginx configuration..."
if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
  rollback_nginx_config "$BACKUP_COMPOSE_FILE" "$MOVED_NGINX_CONF" "$RECYCLE_NGINX_CONF"
  fail "Nginx configuration test failed. Restored nginx compose/config backup. User directory may need manual review: $USER_DIR"
fi

log "Reloading nginx..."
if ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
  fail "Failed to reload nginx"
fi

update_users_csv_status

metadata_args=(
  "$SCRIPT_DIR/metadata_cli.py"
  set-instance-status
  --user-id "$USER_ID"
  --status active
  --action restore_instance
  --port "$PORT"
  --message "restored from restore_user.sh recycle=$RECYCLE_DIR"
)
python3 "${metadata_args[@]}" || warn "Metadata update failed for restored user: $USER_ID"

cat <<EOF

==============================
RESTORE SUCCESS
User: $USER_ID
Port: $PORT
User dir: $USER_DIR
Nginx conf: $NGINX_TARGET_CONF
Backup dir: $BACKUP_DIR
==============================
EOF
