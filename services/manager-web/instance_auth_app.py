import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, request


app = Flask(__name__)
COOKIE_NAME = "openclaw_manager_session"
CONTROL_URL = os.environ.get(
    "MANAGER_CONTROL_BASE_URL", "http://manager-control:8082"
).rstrip("/")
CONTROL_TOKEN = os.environ.get("MANAGER_CONTROL_INSTANCE_AUTH_TOKEN", "").strip()


@app.get("/health")
def health():
    return ({"ok": True}, 200) if CONTROL_TOKEN else ({"ok": False}, 503)


@app.get("/authorize/<instance_public_id>")
def authorize(instance_public_id):
    raw_token = request.cookies.get(COOKIE_NAME, "")
    if not raw_token:
        return "", 401
    query = urllib.parse.urlencode(
        {"token_hash": hashlib.sha256(raw_token.encode()).hexdigest()}
    )
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    upstream = urllib.request.Request(
        f"{CONTROL_URL}/internal/v1/instance-access/{instance_id}?{query}",
        headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(upstream, timeout=5) as response:
            return "", 204 if response.status == 200 else response.status
    except urllib.error.HTTPError as exc:
        return "", exc.code if exc.code in {401, 403} else 503
    except (urllib.error.URLError, TimeoutError):
        return "", 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8084)
