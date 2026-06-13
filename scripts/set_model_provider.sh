#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTANCE_ID="${1:?请提供实例 ID，例如：xinxizhongxin}"
CONTAINER_NAME="openclaw_${INSTANCE_ID}"

MANAGER_ENV_FILE="$MANAGER_DIR/config/openclaw-manager.env"
PROVIDER_ENV_FILE="$MANAGER_DIR/config/model-providers.env"

if [[ ! -f "$PROVIDER_ENV_FILE" ]]; then
  echo "错误：未找到配置文件：$PROVIDER_ENV_FILE"
  exit 1
fi

set -a
if [[ -f "$MANAGER_ENV_FILE" ]]; then
  source "$MANAGER_ENV_FILE"
fi
source "$PROVIDER_ENV_FILE"
set +a

MODEL_PROVIDER_ID="${MODEL_PROVIDER_ID:?缺少 MODEL_PROVIDER_ID}"
MODEL_ID="${MODEL_ID:?缺少 MODEL_ID}"
MODEL_ALIAS="${MODEL_ALIAS:-$MODEL_ID}"
MODEL_PROXY_PUBLIC_BASE_URL="${MODEL_PROXY_PUBLIC_BASE_URL:-${MODEL_BASE_URL:-http://openclaw-model-proxy:8081/v1}}"
MODEL_PROXY_TOKEN_DIR="${MODEL_PROXY_TOKEN_DIR:-/data/docker/openclaw-public/model-proxy-tokens}"

MODEL_SHORT_ID="${MODEL_ID#${MODEL_PROVIDER_ID}/}"
PRIMARY_MODEL="$MODEL_ID"
MODEL_PROXY_TOKEN_FILE="$MODEL_PROXY_TOKEN_DIR/${INSTANCE_ID}.token"
MODEL_PROXY_MODELS_FILE="$MODEL_PROXY_TOKEN_DIR/${INSTANCE_ID}.models"

mkdir -p "$MODEL_PROXY_TOKEN_DIR"
if [[ -s "$MODEL_PROXY_TOKEN_FILE" ]]; then
  MODEL_PROXY_TOKEN="$(tr -d '\r\n' < "$MODEL_PROXY_TOKEN_FILE")"
else
  MODEL_PROXY_TOKEN="$(python3 - <<'PY'
import secrets

print("ocm_" + secrets.token_urlsafe(32))
PY
)"
  umask 077
  printf '%s\n' "$MODEL_PROXY_TOKEN" > "$MODEL_PROXY_TOKEN_FILE"
fi
chmod 600 "$MODEL_PROXY_TOKEN_FILE"
printf '%s\n' "$MODEL_SHORT_ID" > "$MODEL_PROXY_MODELS_FILE"
chmod 600 "$MODEL_PROXY_MODELS_FILE"

PROVIDER_JSON=$(cat <<EOF
{
  "baseUrl": "$MODEL_PROXY_PUBLIC_BASE_URL",
  "apiKey": "$MODEL_PROXY_TOKEN",
  "api": "openai-completions",
  "models": [
    {
      "id": "$MODEL_SHORT_ID",
      "name": "$MODEL_ALIAS",
      "api": "openai-completions",
      "reasoning": false,
      "input": ["text"],
      "cost": {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0
      },
      "contextWindow": 131072,
      "maxTokens": 16384
    }
  ]
}
EOF
)

echo "设置 models.providers.${MODEL_PROVIDER_ID} ..."
docker exec -i "$CONTAINER_NAME" openclaw config set "models.providers.${MODEL_PROVIDER_ID}" "$PROVIDER_JSON" --strict-json

echo "设置 agents.defaults.model.primary ..."
docker exec -i "$CONTAINER_NAME" openclaw config set agents.defaults.model.primary "\"$PRIMARY_MODEL\"" --strict-json

echo "校验配置 ..."
docker exec -i "$CONTAINER_NAME" openclaw config validate

echo "重启容器使配置生效 ..."
docker restart "$CONTAINER_NAME"

echo "完成。容器已重启：$CONTAINER_NAME"
echo "模型代理地址：$MODEL_PROXY_PUBLIC_BASE_URL"
echo "实例模型 token 文件：$MODEL_PROXY_TOKEN_FILE"
echo "实例模型白名单文件：$MODEL_PROXY_MODELS_FILE"
