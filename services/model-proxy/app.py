import hmac
import json
import os
from pathlib import Path
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, request
from observability import ModelProxyObservabilityAdapter, ModelRequestObserver, initialize


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
REGENERATED_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
}
MODEL_GATED_PATHS = {
    "chat/completions",
    "completions",
    "embeddings",
    "responses",
}

app = Flask(__name__)
OBSERVER = ModelProxyObservabilityAdapter(ModelRequestObserver(initialize("openclaw-manager.model-proxy")))


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
        if key.lower() in HOP_BY_HOP_HEADERS | REGENERATED_RESPONSE_HEADERS:
            continue
        headers.append((key, value))
    return headers


def observed_content(upstream_response, observation):
    """Stream upstream content and finish the observation at stream end."""
    error = (None, None, None)
    output_chunks = []
    usage = None
    try:
        for chunk in upstream_response.iter_content(chunk_size=8192):
            if chunk:
                output_chunks.append(chunk)
            yield chunk
    except BaseException as exc:
        error = (type(exc), exc, exc.__traceback__)
        raise
    else:
        error = (None, None, None)
    finally:
        if output_chunks:
            body = b"".join(output_chunks)
            headers = getattr(upstream_response, "headers", {})
            parsed, usage = summarize_response(body, headers.get("Content-Type", ""))
            observation.set_output(parsed)
        if usage:
            observation.set_usage(normalize_usage(usage))
        observation.__exit__(*error)


def summarize_response(body, content_type):
    """Return JSON-safe output and usage without exposing raw SSE as metadata."""
    try:
        parsed = json.loads(body.decode("utf-8"))
        return parsed, parsed.get("usage") if isinstance(parsed, dict) else None
    except (UnicodeDecodeError, ValueError):
        pass
    if "text/event-stream" not in content_type.lower():
        return {"raw": body.decode("utf-8", errors="replace")}, None
    content = []
    tool_calls = {}
    finish_reason = None
    usage = None
    for line in body.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        value = line[5:].strip()
        if value == "[DONE]":
            continue
        try:
            chunk = json.loads(value)
        except ValueError:
            continue
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for call in delta.get("tool_calls", []):
                index = call.get("index", 0)
                current = tool_calls.setdefault(index, {"index": index})
                for key in ("id", "type"):
                    if call.get(key):
                        current[key] = call[key]
                function = call.get("function") or {}
                target = current.setdefault("function", {})
                for key in ("name", "arguments"):
                    if function.get(key):
                        target[key] = target.get(key, "") + function[key] if key == "arguments" else function[key]
            finish_reason = choice.get("finish_reason") or finish_reason
    result = {"content": "".join(content), "finish_reason": finish_reason}
    if tool_calls:
        result["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    return result, usage


def normalize_usage(usage):
    if not isinstance(usage, dict):
        return usage
    result = dict(usage)
    result.setdefault("input", result.get("prompt_tokens", result.get("input_tokens")))
    result.setdefault("output", result.get("completion_tokens", result.get("output_tokens")))
    result.setdefault("total", result.get("total_tokens"))
    return {key: value for key, value in result.items() if value is not None}


def request_context():
    try:
        from opentelemetry.propagate import extract
        return extract(dict(request.headers))
    except Exception:
        return None


def request_product():
    return request.headers.get("X-OpenClaw-Product", "model-proxy").strip() or "model-proxy"


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

    request_body = request.get_data()
    payload = request.get_json(silent=True) if request.is_json else None
    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    tool_results = [
        message for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    tool_result_bytes = sum(
        len(str(message.get("content", "")).encode("utf-8"))
        for message in tool_results
    )
    input_value = {
        "messages": messages,
        "tools": payload.get("tools", []) if isinstance(payload, dict) else [],
        "model": request_model(),
    }
    model_parameters = {
        key: payload[key]
        for key in ("temperature", "top_p", "max_tokens", "stream")
        if isinstance(payload, dict) and key in payload
    }
    observation = OBSERVER.observe(
        agent_id=user_id,
        model=request_model(),
        path=path.strip("/"),
        request_bytes=len(request_body),
        message_count=len(messages),
        tool_result_count=len(tool_results),
        tool_result_bytes=tool_result_bytes,
        session_id=request.headers.get("X-OpenClaw-Session-Id", ""),
        run_id=request.headers.get("X-OpenClaw-Run-Id", ""),
        product=request_product(),
        input_value=input_value,
        model_parameters=model_parameters,
        context=request_context(),
        instance_id=user_id,
        environment=os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split("deployment.environment=", 1)[-1].split(",", 1)[0]
        if "deployment.environment=" in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "") else "",
    )
    span = observation.__enter__()
    try:
        upstream_response = requests.request(
            method=request.method,
            url=upstream_url,
            headers=upstream_headers(user_id),
            data=request_body,
            stream=True,
            timeout=REQUEST_TIMEOUT,
        )
        if span is not None:
            span.set_attribute("http.response.status_code", upstream_response.status_code)
    except requests.RequestException as exc:
        observation.__exit__(type(exc), exc, exc.__traceback__)
        app.logger.warning("upstream request failed for user=%s path=/v1/%s: %s", user_id, path, exc)
        return jsonify({"error": "upstream request failed"}), 502

    if request.method == "GET" and path.strip("/") == "models":
        try:
            return filter_models_response(user_id, upstream_response)
        finally:
            observation.__exit__(None, None, None)

    return Response(
        observed_content(upstream_response, observation),
        status=upstream_response.status_code,
        headers=response_headers(upstream_response),
    )


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_PORT", "8081")),
    )
