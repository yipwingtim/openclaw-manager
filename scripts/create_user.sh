#!/bin/bash

set -e

# ===== 参数 =====
# ===== 基础路径 =====
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$MANAGER_DIR/config/openclaw-manager.env"

# ===== 读取统一配置 =====
if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERROR] Config file not found: $CONFIG_FILE"
  exit 1
fi

source "$CONFIG_FILE"

# ===== 参数 =====
USER_ID=$1

if [ -z "$USER_ID" ]; then
  echo "Usage: $0 <user_id>"
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

USER_DIR="$BASE_DIR/users/$USER_ID"
LOG_FILE="$BASE_DIR/logs/scripts/create_user.log"
TEMPLATE="$OPENCLAW_MANAGER_DIR/templates/docker-compose.tpl.yml"

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

# ===== 检查用户 =====
if [ -d "$USER_DIR" ]; then
  fail "User $USER_ID already exists"
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

  if ss -tnl | awk '{print $4}' | grep -qE "(:|\.)${PORT}$"; then
    log "Port $PORT is already in use, skip"
    PORT=$((PORT + 1))
    continue
  fi

  break
done

NEXT_PORT=$((PORT + 1))

log "Alloc port $PORT for user $USER_ID"

# ===== 创建用户目录 =====
mkdir -p "$USER_DIR"/{config,workspaces,workspace,skills,extensions}

CONFIG_FILE="$USER_DIR/config/openclaw.json"

# ===== 写入 OpenClaw 基础配置 =====
cat > "$CONFIG_FILE" <<EOF
{
  "gateway": {
    "mode": "local",
    "bind": "lan",
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
sed -i "s#{{PORT}}#$PORT#g" "$TARGET_COMPOSE"
sed -i "s#{{VERSION}}#$VERSION#g" "$TARGET_COMPOSE"
sed -i "s#{{BASE_DIR}}#$BASE_DIR#g" "$TARGET_COMPOSE"
sed -i "s#{{TZ}}#$TZ#g" "$TARGET_COMPOSE"

# ===== 生成 nginx 用户配置 =====
mkdir -p "$NGINX_USERS_CONF_DIR"

NGINX_USER_CONF="$NGINX_USERS_CONF_DIR/${USER_ID}.conf"

cat > "$NGINX_USER_CONF" <<EOF
server {
    listen $PORT ssl;
    server_name _;

    ssl_certificate $NGINX_SSL_CERT;
    ssl_certificate_key $NGINX_SSL_KEY;

    client_max_body_size 10M;

    location / {
        auth_basic "OpenClaw Login";
        auth_basic_user_file $NGINX_HTPASSWD_FILE_IN_CONTAINER;

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
fi

# ===== 创建 / 更新 Basic Auth 用户 =====
if ! command -v htpasswd >/dev/null 2>&1; then
  fail "htpasswd command not found. Please install apache2-utils first."
  fail "Ubuntu/Debian: sudo apt update && sudo apt install -y apache2-utils"
  exit 1
fi

if ! command -v setfacl >/dev/null 2>&1; then
  fail "setfacl command not found. Please install acl first."
  fail "Ubuntu/Debian: sudo apt update && sudo apt install -y acl"
  exit 1
fi

mkdir -p "$(dirname "$NGINX_HTPASSWD_FILE")"

if [ ! -f "$NGINX_HTPASSWD_FILE" ]; then
  log "Creating Basic Auth user: $USER_ID"
  htpasswd -c "$NGINX_HTPASSWD_FILE" "$USER_ID"
else
  log "Creating / updating Basic Auth user: $USER_ID"
  htpasswd "$NGINX_HTPASSWD_FILE" "$USER_ID"
fi

# ===== 修复 nginx 容器读取 .htpasswd 的权限 =====
NGINX_UID=$(docker exec "$NGINX_CONTAINER_NAME" id -u nginx 2>/dev/null || true)

if [ -z "$NGINX_UID" ]; then
  fail "Could not detect nginx user UID in container: $NGINX_CONTAINER_NAME"
  exit 1
fi

log "Granting nginx container user UID $NGINX_UID read access to htpasswd file"

sudo setfacl -m "u:${NGINX_UID}:rx" "$(dirname "$NGINX_HTPASSWD_FILE")"
sudo setfacl -m "u:${NGINX_UID}:r" "$NGINX_HTPASSWD_FILE"

# ===== 启动用户容器 =====
cd "$USER_DIR"

if ! docker compose up -d; then
  fail "Failed to start container for user $USER_ID"
  fail "User directory is kept for troubleshooting: $USER_DIR"
  exit 1
fi

# ===== 更新 nginx 容器并检查配置 =====
log "Updating nginx container port mappings"

cd "$NGINX_COMPOSE_DIR"

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
echo "👉 username: $USER_ID"
echo "👉 password: 刚才创建 Basic Auth 用户时输入的密码"

echo "Login Token:"
if [ -z "$TOKEN" ]; then
  echo "👉 Token not generated yet. Check: $CONFIG_FILE"
else
  echo "👉 $TOKEN"
fi

echo "=============================="
