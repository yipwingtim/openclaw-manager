import json
import os
import re
import selectors
import subprocess
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
WECHAT_URL_RE = re.compile(r"https://liteapp\.weixin\.qq\.com/q/[^\s\"'<>]+")
FILE_ROOTS = {"workspace": "workspace", "workspaces": "workspaces", "uploads": "uploads"}


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

    def get_runtime_instance(self, instance_public_id, actor_user_public_id, admin=False):
        query = urllib.parse.urlencode(
            {
                "actor_user_public_id": actor_user_public_id,
                "admin": "true" if admin else "false",
            }
        )
        instance_id = urllib.parse.quote(instance_public_id, safe="")
        return self.request(
            "GET", f"/internal/v1/executor/instances/{instance_id}?{query}"
        )["instance"]

    def get_job(self, request_id):
        request_id = urllib.parse.quote(request_id, safe="")
        return self.request("GET", f"/internal/v1/execution-jobs/{request_id}")["job"]


def resolve_instance_file(instance, root_key, relative_path):
    relative_root = FILE_ROOTS.get(root_key)
    data_path = instance.get("data_path")
    if relative_root is None or not isinstance(data_path, str) or not data_path:
        return None
    data_root = Path(data_path).resolve()
    root = (data_root / relative_root).resolve()
    target = (root / relative_path).resolve()
    try:
        root.relative_to(data_root)
        target.relative_to(root)
    except ValueError:
        return None
    return target


def job_cancelled(control, request_id):
    try:
        return control.get_job(request_id).get("status") == "cancelled"
    except Exception:
        return False


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
        if action == "wechat_bind":
            instance = control.get_runtime_instance(
                job["instance_public_id"], job["actor_user_public_id"]
            )
            if (
                instance.get("product") != "openclaw"
                or instance.get("access_role") not in {"owner", "manager"}
            ):
                raise ValueError("device pairing is not supported")
            adapter = adapter_factory(instance["product"])
            if not adapter.status(instance).startswith("Up"):
                raise ValueError("instance is not running")
            runtime_target = adapter.get_runtime_target(instance)
            control.update(request_id, "running", current_step="starting wechat bind")
            process = None
            selector = None
            cancelled = False
            process_reaped = False
            try:
                process = subprocess.Popen(
                    [
                        "docker", "exec", runtime_target, "timeout", "300s", "npx", "-y",
                        "@tencent-weixin/openclaw-weixin-cli", "install",
                    ],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                output_tail = ""
                assert process.stdout is not None
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                while process.poll() is None:
                    cancelled = job_cancelled(control, request_id)
                    if cancelled:
                        return True
                    events = selector.select(timeout=1)
                    if not events:
                        continue
                    line = process.stdout.readline()
                    if not line:
                        continue
                    output_tail = (output_tail + line)[-MAX_OUTPUT_LENGTH:]
                    match = WECHAT_URL_RE.search(output_tail)
                    output = match.group(0)[:2048] if match else "微信绑定任务正在运行。"
                    control.update(request_id, "running", output=output)
                return_code = process.wait(timeout=5)
                process_reaped = True
                cancelled = job_cancelled(control, request_id)
                if cancelled:
                    return True
                output_tail = (output_tail + process.stdout.read())[-MAX_OUTPUT_LENGTH:]
                match = WECHAT_URL_RE.search(output_tail)
                output = match.group(0)[:2048] if match else ""
                if return_code == 0:
                    control.update(request_id, "succeeded", output=output)
                else:
                    control.update(
                        request_id,
                        "failed",
                        error_summary="微信绑定命令失败或超时",
                        output=output,
                    )
                return True
            finally:
                cleanup_command = process is not None and not process_reaped
                if selector is not None:
                    selector.close()
                if cleanup_command:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    process_reaped = True
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                if not cancelled:
                    cancelled = job_cancelled(control, request_id)
                if cancelled or cleanup_command:
                    subprocess.run(
                        [
                            "docker", "exec", runtime_target, "pkill", "-f",
                            "@tencent-weixin/openclaw-weixin-cli",
                        ],
                        capture_output=True, timeout=10, check=False,
                    )
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
        if not job_cancelled(control, request_id):
            try:
                control.update(request_id, "failed", error_summary=str(exc))
            except Exception:
                pass
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
