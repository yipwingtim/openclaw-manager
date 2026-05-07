#!/usr/bin/env bash
set -euo pipefail

INSTANCE_ID="${1:?请提供实例 ID，例如：xinxizhongxin}"
CONTAINER_NAME="openclaw_${INSTANCE_ID}"

ENV_FILE="config/model-providers.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "错误：未找到配置文件：$ENV_FILE"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

MODEL_PROVIDER_ID="${MODEL_PROVIDER_ID:?缺少 MODEL_PROVIDER_ID}"
MODEL_ID="${MODEL_ID:?缺少 MODEL_ID}"
MODEL_BASE_URL="${MODEL_BASE_URL:?缺少 MODEL_BASE_URL}"
MODEL_API_KEY="${MODEL_API_KEY:?缺少 MODEL_API_KEY}"
MODEL_ALIAS="${MODEL_ALIAS:-$MODEL_ID}"

MODEL_SHORT_ID="${MODEL_ID#${MODEL_PROVIDER_ID}/}"
PRIMARY_MODEL="$MODEL_ID"

PROVIDER_JSON=$(cat <<EOF
{
  "baseUrl": "$MODEL_BASE_URL",
  "apiKey": "$MODEL_API_KEY",
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
