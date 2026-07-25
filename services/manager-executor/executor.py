import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from instance_adapters import EvoScientistDockerAdapter, OpenClawDockerAdapter


BASE_URL = os.environ.get(
    "MANAGER_CONTROL_BASE_URL", "http://manager-control:8082"
).rstrip("/")
TOKEN = os.environ.get("MANAGER_CONTROL_EXECUTOR_TOKEN", "").strip()
TIMEOUT = int(os.environ.get("MANAGER_CONTROL_TIMEOUT", "5"))
POLL_SECONDS = float(os.environ.get("MANAGER_EXECUTOR_POLL_SECONDS", "2"))
MAX_ATTEMPTS = max(1, int(os.environ.get("MANAGER_EXECUTOR_MAX_ATTEMPTS", "2")))
MAX_OUTPUT_LENGTH = 32 * 1024


class ControlClient:
    def request(self, method, path, payload=None):
        if not TOKEN:
            raise RuntimeError("MANAGER_CONTROL_EXECUTOR_TOKEN is not configured")
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            message = json.loads(body).get("error", str(exc)) if body else str(exc)
            raise RuntimeError(message) from exc
        return json.loads(body) if body else None

    def claim(self):
        return self.request("POST", "/internal/v1/execution-jobs/claim")

    def update(self, request_id, status, **fields):
        request_id = urllib.parse.quote(request_id, safe="")
        return self.request(
            "PATCH",
            f"/internal/v1/execution-jobs/{request_id}",
            {"status": status, **fields},
        )


def get_adapter(product):
    adapter_type = {
        "openclaw": OpenClawDockerAdapter,
        "evoscientist": EvoScientistDockerAdapter,
    }.get(product)
    if adapter_type is None:
        raise ValueError(f"unsupported instance product: {product}")
    return adapter_type(
        manager_dir=Path(
            os.environ.get("OPENCLAW_MANAGER_DIR", "/opt/openclaw-manager")
        ),
        public_dir=Path(
            os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public")
        ),
        nginx_users_conf_dir=Path(
            os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf")
        ),
        nginx_compose_dir=Path(
            os.environ.get("NGINX_COMPOSE_DIR", "/data/docker/nginx/compose")
        ),
        nginx_container_name=os.environ.get("NGINX_CONTAINER_NAME", "openclaw-nginx"),
    )


def run_once(control, adapter_factory=get_adapter, max_attempts=MAX_ATTEMPTS):
    claimed = control.claim()
    if not claimed:
        return False
    job, instance = claimed["job"], claimed["instance"]
    request_id = job["request_id"]
    action = job["action"].removeprefix("instance.")
    try:
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"unsupported execution action: {job['action']}")
        adapter = adapter_factory(instance["product"])
        status = adapter.status(instance)
        if action == "start" and status.startswith("Up"):
            control.update(request_id, "succeeded", output="instance already running")
            return True
        if action == "stop" and status == "STOPPED":
            control.update(request_id, "succeeded", output="instance already stopped")
            return True

        output = ""
        attempts = 1 if action == "restart" else max_attempts
        for attempt in range(1, attempts + 1):
            control.update(
                request_id,
                "running",
                current_step=f"{action} attempt {attempt}/{attempts}",
            )
            try:
                code, output = getattr(adapter, action)(instance)
            except Exception as exc:
                code, output = 1, str(exc)
            output = output[-MAX_OUTPUT_LENGTH:]
            if code == 0:
                control.update(request_id, "succeeded", output=output)
                return True
        control.update(
            request_id,
            "failed",
            error_summary=f"{action} failed after {attempts} attempt(s)",
            output=output,
        )
    except Exception as exc:
        control.update(request_id, "failed", error_summary=str(exc))
    return True


def main():
    control = ControlClient()
    while True:
        try:
            worked = run_once(control)
        except Exception as exc:
            print(f"manager-executor error: {exc}", flush=True)
            worked = False
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
