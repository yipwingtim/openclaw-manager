#!/bin/bash

set -e

# ===== 参数 =====
# ===== 基础路径 =====
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"
LIB_NGINX_AUTH="$SCRIPT_DIR/lib_nginx_auth.sh"

# ===== 读取统一配置 =====
if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE"
  exit 1
fi

source "$CONFIG_FILE"
source "$LIB_NGINX_AUTH"

# ===== 参数 =====
USER_ID="${1:-}"
BASIC_AUTH_PASSWORD=""
BASIC_AUTH_ENABLED="true"
SKIP_NGINX_RELOAD=0
SUCCESS=0
USER_DIR_CREATED=0
NGINX_CONF_CREATED=0
PORT_MAPPING_CREATED=0
USER_HTPASSWD_FILE_CREATED=0
USERS_CSV_ROW_CREATED=0
NGINX_COMPOSE_APPLIED=0

shift || true

while [ "$#" -gt 0 ]; do
  case "$1" in
    --password)
      BASIC_AUTH_PASSWORD="${2:-}"
      if [ -z "$BASIC_AUTH_PASSWORD" ]; then
        echo "[ERROR] --password requires a value"
        exit 1
      fi
      shift 2
      ;;
    --basic-auth-enabled)
      if [ "$#" -lt 2 ]; then
        echo "[ERROR] --basic-auth-enabled requires true or false"
        exit 1
      fi
      BASIC_AUTH_ENABLED="$(normalize_basic_auth_enabled "${2:-}")" || {
        echo "[ERROR] --basic-auth-enabled must be true or false"
        exit 1
      }
      shift 2
      ;;
    --skip-nginx-reload)
      SKIP_NGINX_RELOAD=1
      shift
      ;;
    *)
      echo "[ERROR] Unknown argument: $1"
      echo "Usage: $0 <user_id> [--password <basic_auth_password>] [--basic-auth-enabled true|false] [--skip-nginx-reload]"
      exit 1
      ;;
  esac
done

if [ -z "$USER_ID" ]; then
  echo "Usage: $0 <user_id> [--password <basic_auth_password>] [--basic-auth-enabled true|false] [--skip-nginx-reload]"
  exit 1
fi

# ===== 校验必要配置 =====
required_vars=(
  PUBLIC_HOST
  OPENCLAW_PUBLIC_DIR
  OPENCLAW_MANAGER_DIR
  OPENCLAW_VERSION
  TZ
  PORT_FILE
  USERS_CSV
  NGINX_COMPOSE_FILE
  NGINX_COMPOSE_DIR
  NGINX_CONF_DIR
  NGINX_USERS_CONF_DIR
  NGINX_SSL_CERT
  NGINX_SSL_KEY
  NGINX_HTPASSWD_FILE
  NGINX_HTPASSWD_FILE_IN_CONTAINER
  NGINX_CONTAINER_NAME
  DOCKER_NETWORK
  PORT_START
  PORT_END
)

for var in "${required_vars[@]}"; do
  if [ -z "${!var}" ]; then
    echo "[ERROR] Missing config variable: $var"
    exit 1
  fi
done

# ===== 派生路径 =====
BASE_DIR="$OPENCLAW_PUBLIC_DIR"
VERSION="$OPENCLAW_VERSION"
HOST_MANAGER_UID="${HOST_MANAGER_UID:-$(stat -c %u "$BASE_DIR")}"
HOST_MANAGER_GID="${HOST_MANAGER_GID:-$(stat -c %g "$BASE_DIR")}"

USER_DIR="$BASE_DIR/users/$USER_ID"
LOG_FILE="$BASE_DIR/logs/scripts/create_user.log"
TEMPLATE="$MANAGER_DIR/templates/docker-compose.tpl.yml"
SERVICE_ID="$(printf '%s' "$USER_ID" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"

# ===== 创建基础目录 =====
mkdir -p "$BASE_DIR/users"
mkdir -p "$BASE_DIR/deleted"
mkdir -p "$(dirname "$LOG_FILE")"

# ===== 日志函数 =====
log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] $1" | tee -a "$LOG_FILE"
}

fail() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] $1" | tee -a "$LOG_FILE" >&2
}

restore_host_owner() {
  if [ -n "${HOST_MANAGER_UID:-}" ] && [ -n "${HOST_MANAGER_GID:-}" ]; then
    if [ -d "$USER_DIR" ]; then
      chown -R "$HOST_MANAGER_UID:$HOST_MANAGER_GID" "$USER_DIR" 2>/dev/null || true
    fi
    if [ -n "${NGINX_USER_CONF:-}" ] && [ -f "$NGINX_USER_CONF" ]; then
      chown "$HOST_MANAGER_UID:$HOST_MANAGER_GID" "$NGINX_USER_CONF" 2>/dev/null || true
    fi
    if [ -n "${USERS_CSV:-}" ] && [ -f "$USERS_CSV" ]; then
      chown "$HOST_MANAGER_UID:$HOST_MANAGER_GID" "$USERS_CSV" 2>/dev/null || true
      chmod 660 "$USERS_CSV" 2>/dev/null || true
    fi
  fi
}

cleanup_on_exit() {
  local exit_code="$1"

  if [ "$exit_code" -eq 0 ] || [ "$SUCCESS" -eq 1 ]; then
    return
  fi

  restore_host_owner

  if [ -d "$USER_DIR" ]; then
    cd "$USER_DIR" 2>/dev/null || true
    docker compose down >/dev/null 2>&1 || true
  fi

  if [ "$USERS_CSV_ROW_CREATED" -eq 1 ] && [ -f "$USERS_CSV" ]; then
    python3 - "$USERS_CSV" "$USER_ID" "$PORT" <<'PY' >/dev/null 2>&1 || true
import sys
from pathlib import Path

csv_file = Path(sys.argv[1])
user_id = sys.argv[2]
port = sys.argv[3]

lines = csv_file.read_text(encoding="utf-8").splitlines(keepends=True)
filtered = [
    line for line in lines
    if not line.startswith(f"{user_id},{port},") or not line.rstrip("\n").endswith(",active")
]
csv_file.write_text("".join(filtered), encoding="utf-8")
PY
  fi

  if [ "$PORT_MAPPING_CREATED" -eq 1 ] && [ -f "$NGINX_COMPOSE_FILE" ]; then
    python3 - "$NGINX_COMPOSE_FILE" "$PORT" <<'PY' >/dev/null 2>&1 || true
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

compose_file.write_text("".join(line for line in lines if line not in patterns), encoding="utf-8")
PY
  fi

  if [ "$NGINX_CONF_CREATED" -eq 1 ] && [ -f "$NGINX_USER_CONF" ]; then
    rm -f "$NGINX_USER_CONF"
  fi

  if [ "$USER_HTPASSWD_FILE_CREATED" -eq 1 ] && [ -n "${NGINX_USER_HTPASSWD_FILE:-}" ]; then
    rm -f "$NGINX_USER_HTPASSWD_FILE"
    rmdir "$(dirname "$NGINX_USER_HTPASSWD_FILE")" >/dev/null 2>&1 || true
  fi

  if [ "$USER_DIR_CREATED" -eq 1 ] && [ -d "$USER_DIR" ]; then
    rm -rf "$USER_DIR"
  fi

  restore_host_owner

  if [ "$NGINX_COMPOSE_APPLIED" -eq 1 ] && [ -d "$NGINX_COMPOSE_DIR" ]; then
    (
      cd "$NGINX_COMPOSE_DIR" 2>/dev/null && docker compose up -d >/dev/null 2>&1
    ) || true

    docker exec "$NGINX_CONTAINER_NAME" nginx -t >/dev/null 2>&1 && \
      docker exec "$NGINX_CONTAINER_NAME" nginx -s reload >/dev/null 2>&1 || true
  fi
}

trap 'cleanup_on_exit $?' EXIT

# ===== 检查用户 =====
if [ -d "$USER_DIR" ]; then
  fail "User $USER_ID already exists"
  exit 1
fi

if [ -z "$SERVICE_ID" ]; then
  fail "Could not derive compose service id from user: $USER_ID"
  exit 1
fi

# ===== 检查模板 =====
if [ ! -f "$TEMPLATE" ]; then
  fail "Template not found: $TEMPLATE"
  exit 1
fi

# ===== 初始化端口文件 =====
if [ ! -f "$PORT_FILE" ]; then
  echo "$PORT_START" > "$PORT_FILE"
fi

# ===== 分配端口：自动跳过已占用端口，并限制在端口范围内 =====
PORT=$(cat "$PORT_FILE")

if [ "$PORT" -lt "$PORT_START" ]; then
  PORT="$PORT_START"
fi

while true; do
  if [ "$PORT" -gt "$PORT_END" ]; then
    fail "No available port in range $PORT_START-$PORT_END"
    exit 1
  fi

  if python3 - "$PORT" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

try:
    sock.bind(("0.0.0.0", port))
except OSError:
    raise SystemExit(0)
finally:
    sock.close()

raise SystemExit(1)
PY
  then
    log "Port $PORT is already in use, skip"
    PORT=$((PORT + 1))
    continue
  fi

  break
done

NEXT_PORT=$((PORT + 1))

log "Alloc port $PORT for user $USER_ID"

GATEWAY_TOKEN="$(python3 - <<'PY'
import secrets

print(secrets.token_hex(24))
PY
)"

# ===== 创建用户目录 =====
mkdir -p "$USER_DIR"/{config,workspaces,workspace,skills,extensions,uploads}
USER_DIR_CREATED=1

CONFIG_FILE="$USER_DIR/config/openclaw.json"

# ===== 写入 OpenClaw 基础配置 =====
cat > "$CONFIG_FILE" <<EOF
{
  "gateway": {
    "mode": "local",
    "bind": "lan",
    "auth": {
      "token": "$GATEWAY_TOKEN"
    },
    "controlUi": {
      "allowedOrigins": [
        "http://localhost:$PORT",
        "http://127.0.0.1:$PORT",
        "https://$PUBLIC_HOST:$PORT"
      ]
    }
  }
}
EOF

# ===== 注入默认 skills =====
if [ -d "$MANAGER_DIR/templates/skills" ]; then
  cp -r "$MANAGER_DIR/templates/skills/"* "$USER_DIR/skills/" 2>/dev/null || true
fi

# ===== 复制 docker-compose 模板 =====
TARGET_COMPOSE="$USER_DIR/docker-compose.yml"
cp "$TEMPLATE" "$TARGET_COMPOSE"

# ===== 替换变量 =====
sed -i "s#{{USER_ID}}#$USER_ID#g" "$TARGET_COMPOSE"
sed -i "s#{{SERVICE_ID}}#$SERVICE_ID#g" "$TARGET_COMPOSE"
sed -i "s#{{PORT}}#$PORT#g" "$TARGET_COMPOSE"
sed -i "s#{{VERSION}}#$VERSION#g" "$TARGET_COMPOSE"
sed -i "s#{{BASE_DIR}}#$BASE_DIR#g" "$TARGET_COMPOSE"
sed -i "s#{{TZ}}#$TZ#g" "$TARGET_COMPOSE"
sed -i "s#{{GATEWAY_TOKEN}}#$GATEWAY_TOKEN#g" "$TARGET_COMPOSE"

# ===== 生成 nginx 用户配置 =====
mkdir -p "$NGINX_USERS_CONF_DIR"

NGINX_USER_CONF="$NGINX_USERS_CONF_DIR/${USER_ID}.conf"
NGINX_USER_HTPASSWD_FILE="$(nginx_user_htpasswd_file "$USER_ID" "$NGINX_HTPASSWD_FILE")"
NGINX_USER_HTPASSWD_FILE_IN_CONTAINER="$(nginx_user_htpasswd_file_in_container "$USER_ID" "$NGINX_HTPASSWD_FILE_IN_CONTAINER")"
NGINX_USER_HTPASSWD_REF="$(nginx_user_htpasswd_ref "$USER_ID" "$NGINX_HTPASSWD_FILE_IN_CONTAINER")"
NGINX_AUTH_BLOCK="$(render_nginx_auth_lines "$BASIC_AUTH_ENABLED" "$NGINX_USER_HTPASSWD_FILE_IN_CONTAINER")"
NGINX_ADMIN_AUTH_BLOCK="$(render_nginx_auth_lines "true" "$NGINX_USER_HTPASSWD_FILE_IN_CONTAINER")"

cat > "$NGINX_USER_CONF" <<EOF
server {
    listen $PORT ssl;
    server_name _;

    ssl_certificate $NGINX_SSL_CERT;
    ssl_certificate_key $NGINX_SSL_KEY;

    client_max_body_size 10M;

    location = /admin {
        return 302 /admin/;
    }

    location /admin/ {
$NGINX_ADMIN_AUTH_BLOCK
        proxy_pass http://openclaw-manager-web:8080/instance-admin/;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-OpenClaw-User "$USER_ID";

        proxy_read_timeout 300;
        proxy_send_timeout 300;
    }

    location / {
$NGINX_AUTH_BLOCK
        proxy_pass http://openclaw_${USER_ID}:18789;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }
}
EOF
NGINX_CONF_CREATED=1

restore_host_owner

log "Generated nginx config: $NGINX_USER_CONF"

# ===== 更新 nginx docker-compose 端口映射 =====
if grep -q "\"$PORT:$PORT\"" "$NGINX_COMPOSE_FILE"; then
  log "Nginx port $PORT already exists in compose"
else
  python3 - "$NGINX_COMPOSE_FILE" "$PORT" <<'PY'
import sys
from pathlib import Path

compose_file = Path(sys.argv[1])
port = sys.argv[2]
line_to_add = f'      - "{port}:{port}"\n'

text = compose_file.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

out = []
in_nginx_service = False
in_ports = False
inserted = False

for i, line in enumerate(lines):
    stripped = line.strip()

    # 进入 nginx service
    if line.startswith("  nginx:"):
        in_nginx_service = True
        out.append(line)
        continue

    # 离开 nginx service：遇到下一个同级 service
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

    if in_ports:
        # ports 列表结束：遇到同级配置项
        if line.startswith("    ") and not line.startswith("      -"):
            if not inserted:
                out.append(line_to_add)
                inserted = True
            in_ports = False
            out.append(line)
            continue

    out.append(line)

# 文件结束时仍在 ports 段
if in_ports and not inserted:
    out.append(line_to_add)
    inserted = True

if not inserted:
    raise SystemExit("Could not insert nginx port mapping. Please check nginx compose structure.")

compose_file.write_text("".join(out), encoding="utf-8")
PY

  log "Added nginx port mapping: $PORT:$PORT"
  PORT_MAPPING_CREATED=1
fi

# ===== 创建 / 更新实例 Basic Auth 用户 =====
{
  if ! command -v htpasswd >/dev/null 2>&1; then
    fail "htpasswd command not found. Please install apache2-utils first."
    fail "Ubuntu/Debian: sudo apt update && sudo apt install -y apache2-utils"
    exit 1
  fi

  mkdir -p "$(dirname "$NGINX_USER_HTPASSWD_FILE")"

  if [ ! -f "$NGINX_USER_HTPASSWD_FILE" ]; then
    USER_HTPASSWD_FILE_CREATED=1
    log "Creating instance Basic Auth user: $USER_ID"
    if [ -n "$BASIC_AUTH_PASSWORD" ]; then
      printf '%s\n' "$BASIC_AUTH_PASSWORD" | htpasswd -ci "$NGINX_USER_HTPASSWD_FILE" "$USER_ID"
    else
      htpasswd -c "$NGINX_USER_HTPASSWD_FILE" "$USER_ID"
    fi
  else
    log "Creating / updating instance Basic Auth user: $USER_ID"
    if [ -n "$BASIC_AUTH_PASSWORD" ]; then
      printf '%s\n' "$BASIC_AUTH_PASSWORD" | htpasswd -i "$NGINX_USER_HTPASSWD_FILE" "$USER_ID"
    else
      htpasswd "$NGINX_USER_HTPASSWD_FILE" "$USER_ID"
    fi
  fi

  ensure_nginx_htpasswd_permissions "$NGINX_USER_HTPASSWD_FILE"
}

# ===== 启动用户容器 =====
cd "$USER_DIR"

if ! docker compose up -d; then
  fail "Failed to start container for user $USER_ID"
  fail "Rolling back generated files for user: $USER_ID"
  exit 1
fi

if [ "$SKIP_NGINX_RELOAD" -eq 1 ]; then
  log "Skip nginx update/reload; caller must reload nginx after batch operations"
else
  # ===== 更新 nginx 容器并检查配置 =====
  log "Updating nginx container port mappings"

  cd "$NGINX_COMPOSE_DIR"

  NGINX_COMPOSE_APPLIED=1
  if ! docker compose up -d; then
    fail "Failed to update nginx container"
    exit 1
  fi

  log "Testing nginx configuration"

  if ! docker exec "$NGINX_CONTAINER_NAME" nginx -t; then
    fail "Nginx configuration test failed"
    exit 1
  fi

  log "Reloading nginx"

  if ! docker exec "$NGINX_CONTAINER_NAME" nginx -s reload; then
    fail "Failed to reload nginx"
    exit 1
  fi
fi

# ===== 成功后再提交端口 =====
echo "$NEXT_PORT" > "$PORT_FILE"

log "User $USER_ID created successfully"
log "Port: $PORT"

# ===== 等待 OpenClaw 自动生成 token =====
TOKEN=""

for i in $(seq 1 20); do
  if [ -f "$CONFIG_FILE" ]; then
    TOKEN=$(python3 - "$CONFIG_FILE" <<'PY' 2>/dev/null || true
import json
import sys

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(data.get("gateway", {}).get("auth", {}).get("token", ""))
except Exception:
    print("")
PY
)
  fi

  if [ -n "$TOKEN" ]; then
    break
  fi

  sleep 1
done

# ===== 记录用户清单 =====
if [ ! -f "$USERS_CSV" ]; then
  echo "user_id,port,created_at,status" > "$USERS_CSV"
fi

echo "$USER_ID,$PORT,$(date '+%Y-%m-%d %H:%M:%S'),active" >> "$USERS_CSV"
USERS_CSV_ROW_CREATED=1

restore_host_owner

python3 "$SCRIPT_DIR/metadata_cli.py" create-instance \
  --user-id "$USER_ID" \
  --port "$PORT" \
  --openclaw-version "$VERSION" \
  --basic-auth-enabled "$BASIC_AUTH_ENABLED" \
  --basic-auth-password-ref "$NGINX_USER_HTPASSWD_REF" \
  --openclaw-token "$TOKEN" \
  --message "created from create_user.sh" \
  || echo "[WARN] Metadata update failed for created user: $USER_ID"

# ===== 输出 =====
echo ""
echo "=============================="
echo "SUCCESS"
echo "User: $USER_ID"
echo "Port: $PORT"
echo "Access URL:"
echo "👉 https://$PUBLIC_HOST:$PORT"
echo ""
echo "Basic Auth:"
if [ "$BASIC_AUTH_ENABLED" = "false" ]; then
  echo "👉 workspace: disabled"
else
  echo "👉 workspace: enabled"
fi
echo "👉 admin: enabled"
echo "👉 username: $USER_ID"
if [ -n "$BASIC_AUTH_PASSWORD" ]; then
  echo "👉 password: $BASIC_AUTH_PASSWORD"
else
  echo "👉 password: 刚才创建 Basic Auth 用户时输入的密码"
fi

echo "Login Token:"
if [ -z "$TOKEN" ]; then
  echo "👉 Token not generated yet. Check: $CONFIG_FILE"
else
  echo "👉 $TOKEN"
fi

echo "=============================="
SUCCESS=1
