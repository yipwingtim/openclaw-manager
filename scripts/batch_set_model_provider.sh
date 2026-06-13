#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MANAGER_ENV_FILE="$MANAGER_DIR/config/openclaw-manager.env"

if [ -f "$MANAGER_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$MANAGER_ENV_FILE"
  set +a
fi

INPUT_CSV="${1:-}"
OUTPUT_CSV="${2:-}"

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <input.csv> <output.csv>"
  echo
  echo "Input CSV columns:"
  echo "  user_id,model_provider_id,model_id,model_base_url,model_api_key,model_alias"
  echo
  echo "model_base_url and model_api_key are kept for backward-compatible CSV input."
  echo "Instances receive MODEL_PROXY_PUBLIC_BASE_URL and an instance-scoped model proxy token."
  exit 1
fi

INPUT_CSV="$1"
OUTPUT_CSV="$2"

if [ ! -f "$INPUT_CSV" ]; then
  echo "[ERROR] Input CSV not found: $INPUT_CSV" >&2
  exit 1
fi

HEADER="$(head -n 1 "$INPUT_CSV" | tr -d '\r')"
EXPECTED_HEADER="user_id,model_provider_id,model_id,model_base_url,model_api_key,model_alias"

if [ "$HEADER" != "$EXPECTED_HEADER" ]; then
  echo "[ERROR] Invalid input CSV header." >&2
  echo "[ERROR] Expected: $EXPECTED_HEADER" >&2
  echo "[ERROR] Actual: $HEADER" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_CSV")"

csv_escape() {
  local value="${1:-}"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

trim() {
  local value="${1:-}"
  value="${value//$'\r'/}"
  printf '%s' "$value" | xargs
}

write_output_row() {
  local user_id="$1"
  local container_name="$2"
  local model_provider_id="$3"
  local model_id="$4"
  local model_base_url="$5"
  local status="$6"
  local message="$7"

  {
    csv_escape "$user_id"; printf ","
    csv_escape "$container_name"; printf ","
    csv_escape "$model_provider_id"; printf ","
    csv_escape "$model_id"; printf ","
    csv_escape "$model_base_url"; printf ","
    csv_escape "$status"; printf ","
    csv_escape "$message"; printf "\n"
  } >> "$OUTPUT_CSV"
}

set_model_provider() {
  local user_id="$1"
  local model_provider_id="$2"
  local model_id="$3"
  local model_base_url="$4"
  local _model_api_key="$5"
  local model_alias="$6"
  local container_name="openclaw_${user_id}"
  local model_short_id="${model_id#${model_provider_id}/}"
  local primary_model="$model_id"
  local provider_json
  local proxy_base_url="${MODEL_PROXY_PUBLIC_BASE_URL:-${model_base_url:-http://openclaw-model-proxy:8081/v1}}"
  local token_dir="${MODEL_PROXY_TOKEN_DIR:-/data/docker/openclaw-public/model-proxy-tokens}"
  local token_file="$token_dir/${user_id}.token"
  local proxy_token

  mkdir -p "$token_dir"
  if [ -s "$token_file" ]; then
    proxy_token="$(tr -d '\r\n' < "$token_file")"
  else
    proxy_token="$(python3 - <<'PY'
import secrets

print("ocm_" + secrets.token_urlsafe(32))
PY
)"
    umask 077
    printf '%s\n' "$proxy_token" > "$token_file"
  fi
  chmod 600 "$token_file"

  provider_json=$(python3 - "$proxy_base_url" "$proxy_token" "$model_short_id" "$model_alias" <<'PY'
import json
import sys

base_url, api_key, model_short_id, model_alias = sys.argv[1:5]
payload = {
    "baseUrl": base_url,
    "apiKey": api_key,
    "api": "openai-completions",
    "models": [
        {
            "id": model_short_id,
            "name": model_alias,
            "api": "openai-completions",
            "reasoning": False,
            "input": ["text"],
            "cost": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
            },
            "contextWindow": 131072,
            "maxTokens": 16384,
        }
    ],
}
print(json.dumps(payload, ensure_ascii=False))
PY
)

  if ! docker ps --format '{{.Names}}' | grep -Fxq "$container_name"; then
    echo "container_not_running"
    return 1
  fi

  docker exec "$container_name" openclaw config set "models.providers.${model_provider_id}" "$provider_json" --strict-json >/dev/null
  docker exec "$container_name" openclaw config set agents.defaults.model.primary "\"$primary_model\"" --strict-json >/dev/null
  docker exec "$container_name" openclaw config validate >/dev/null
  docker restart "$container_name" >/dev/null
}

echo "user_id,container_name,model_provider_id,model_id,model_base_url,status,message" > "$OUTPUT_CSV"

line_no=0
while IFS=, read -r raw_user_id raw_provider_id raw_model_id raw_base_url raw_api_key raw_alias _rest; do
  line_no=$((line_no + 1))

  if [ "$line_no" -eq 1 ]; then
    continue
  fi

  user_id="$(trim "$raw_user_id")"
  model_provider_id="$(trim "$raw_provider_id")"
  model_id="$(trim "$raw_model_id")"
  model_base_url="$(trim "$raw_base_url")"
  model_api_key="$(trim "$raw_api_key")"
  model_alias="$(trim "${raw_alias:-}")"
  container_name="openclaw_${user_id}"

  if [ -z "$user_id" ]; then
    continue
  fi

  if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "[WARN] Skip invalid user_id at line $line_no: $user_id" >&2
    write_output_row "$user_id" "$container_name" "$model_provider_id" "$model_id" "$model_base_url" "invalid_user_id" "Invalid user_id"
    continue
  fi

  if [ -z "$model_provider_id" ] || [ -z "$model_id" ]; then
    echo "[WARN] Skip incomplete model config at line $line_no: $user_id" >&2
    write_output_row "$user_id" "$container_name" "$model_provider_id" "$model_id" "$model_base_url" "invalid_config" "Missing required model config"
    continue
  fi

  if [ -z "$model_alias" ]; then
    model_alias="$model_id"
  fi

  echo "[INFO] Setting model provider for user: $user_id"
  if message="$(set_model_provider "$user_id" "$model_provider_id" "$model_id" "$model_base_url" "$model_api_key" "$model_alias" 2>&1)"; then
    write_output_row "$user_id" "$container_name" "$model_provider_id" "$model_id" "$model_base_url" "updated" "Container restarted"
  else
    echo "[ERROR] Failed to set model provider for user: $user_id" >&2
    write_output_row "$user_id" "$container_name" "$model_provider_id" "$model_id" "$model_base_url" "failed" "$message"
  fi
done < "$INPUT_CSV"

echo "[INFO] Batch model provider update completed: $OUTPUT_CSV"
