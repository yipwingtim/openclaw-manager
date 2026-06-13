import hmac
import json
import os
from pathlib import Path
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, request


UPSTREAM_BASE_URL = os.environ.get("MODEL_PROXY_UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_API_KEY = os.environ.get("MODEL_PROXY_UPSTREAM_API_KEY", "")
TOKEN_DIR = Path(os.environ.get("MODEL_PROXY_TOKEN_DIR", "/data/docker/openclaw-public/model-proxy-tokens"))
REQUEST_TIMEOUT = int(os.environ.get("MODEL_PROXY_REQUEST_TIMEOUT", "300"))
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
MODEL_GATED_PATHS = {
    "chat/completions",
    "completions",
    "embeddings",
    "responses",
}

app = Flask(__name__)


if not UPSTREAM_BASE_URL:
    app.logger.warning("MODEL_PROXY_UPSTREAM_BASE_URL is not configured; proxy requests will fail.")
if not UPSTREAM_API_KEY:
    app.logger.warning("MODEL_PROXY_UPSTREAM_API_KEY is not configured; upstream authentication will fail.")


def load_tokens():
    tokens = {}
    if not TOKEN_DIR.is_dir():
        return tokens
    for token_file in TOKEN_DIR.glob("*.token"):
        token = token_file.read_text(encoding="utf-8", errors="ignore").strip()
        if token:
            tokens[token] = token_file.stem
    return tokens


def bearer_token():
    value = request.headers.get("Authorization", "")
    if not value.lower().startswith("bearer "):
        return ""
    return value.split(None, 1)[1].strip()


def authenticate():
    provided = bearer_token()
    if not provided:
        return None
    for token, user_id in load_tokens().items():
        if hmac.compare_digest(provided, token):
            return user_id
    return None


def allowed_models_for_user(user_id):
    model_file = TOKEN_DIR / f"{user_id}.models"
    if not model_file.is_file():
        return set()
    return {
        line.strip()
        for line in model_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def request_model():
    if not request.is_json:
        return ""
    payload = request.get_json(silent=True) or {}
    model = payload.get("model")
    return model.strip() if isinstance(model, str) else ""


def enforce_model_allowlist(user_id, path):
    allowed_models = allowed_models_for_user(user_id)
    if not allowed_models:
        return jsonify({"error": "no models are allowed for this model proxy token"}), 403

    normalized_path = path.strip("/")
    if request.method in {"POST", "PUT", "PATCH"} and normalized_path in MODEL_GATED_PATHS:
        model = request_model()
        if not model:
            return jsonify({"error": "request model is required"}), 400
        if model not in allowed_models:
            return jsonify({"error": "model is not allowed for this model proxy token", "model": model}), 403

    return None


def filter_models_response(user_id, upstream_response):
    allowed_models = allowed_models_for_user(user_id)
    if not allowed_models:
        return jsonify({"object": "list", "data": []}), upstream_response.status_code

    try:
        payload = upstream_response.json()
    except ValueError:
        return Response(
            upstream_response.iter_content(chunk_size=8192),
            status=upstream_response.status_code,
            headers=response_headers(upstream_response),
        )

    data = payload.get("data")
    if isinstance(data, list):
        payload["data"] = [
            item
            for item in data
            if isinstance(item, dict) and item.get("id") in allowed_models
        ]
    return jsonify(payload), upstream_response.status_code


def upstream_headers(user_id):
    headers = {}
    for key, value in request.headers.items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS or lower_key == "host":
            continue
        if lower_key == "authorization":
            continue
        headers[key] = value
    headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    headers["X-OpenClaw-Model-Proxy-User"] = user_id
    return headers


def response_headers(upstream_response):
    headers = []
    for key, value in upstream_response.headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        headers.append((key, value))
    return headers


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": bool(UPSTREAM_BASE_URL and UPSTREAM_API_KEY),
            "upstream_configured": bool(UPSTREAM_BASE_URL),
            "token_dir": str(TOKEN_DIR),
        }
    )


@app.route("/v1/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.route("/v1", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy(path=""):
    if not UPSTREAM_BASE_URL or not UPSTREAM_API_KEY:
        return jsonify({"error": "model proxy upstream is not configured"}), 503

    user_id = authenticate()
    if not user_id:
        return jsonify({"error": "invalid model proxy token"}), 401

    allowlist_error = enforce_model_allowlist(user_id, path)
    if allowlist_error is not None:
        return allowlist_error

    upstream_url = urljoin(f"{UPSTREAM_BASE_URL}/", path)
    if request.query_string:
        upstream_url = f"{upstream_url}?{request.query_string.decode('utf-8', errors='ignore')}"

    try:
        upstream_response = requests.request(
            method=request.method,
            url=upstream_url,
            headers=upstream_headers(user_id),
            data=request.get_data(),
            stream=True,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        app.logger.warning("upstream request failed for user=%s path=/v1/%s: %s", user_id, path, exc)
        return jsonify({"error": "upstream request failed"}), 502

    if request.method == "GET" and path.strip("/") == "models":
        return filter_models_response(user_id, upstream_response)

    return Response(
        upstream_response.iter_content(chunk_size=8192),
        status=upstream_response.status_code,
        headers=response_headers(upstream_response),
    )


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_PORT", "8081")),
    )
