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

from instance_adapters import (
    EvoScientistDockerAdapter,
    HermesDockerAdapter,
    OpenClawDockerAdapter,
)
from product_capabilities import execution_action_capability


BASE_URL = os.environ.get(
    "MANAGER_CONTROL_BASE_URL", "http://manager-control:8082"
).rstrip("/")
TOKEN = os.environ.get("MANAGER_CONTROL_EXECUTOR_TOKEN", "").strip()
TIMEOUT = int(os.environ.get("MANAGER_CONTROL_TIMEOUT", "5"))
POLL_SECONDS = float(os.environ.get("MANAGER_EXECUTOR_POLL_SECONDS", "2"))
MAX_ATTEMPTS = max(1, int(os.environ.get("MANAGER_EXECUTOR_MAX_ATTEMPTS", "2")))
MAX_OUTPUT_LENGTH = 32 * 1024
CREATE_ERROR_OUTPUT_LENGTH = 4 * 1024
WECHAT_URL_RE = re.compile(r"https://liteapp\.weixin\.qq\.com/q/[^\s\"'<>]+")
FILE_ROOTS = {"workspace": "workspace", "workspaces": "workspaces", "uploads": "uploads"}
PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
PROVISIONING_SECRET_DIR = PUBLIC_DIR / ".manager-secrets"


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

    def record_activity(self, payload):
        return self.request("POST", "/internal/v1/activity-snapshots", payload)

    def create_hermes_auth_client(self, instance_public_id, payload):
        instance_id = urllib.parse.quote(instance_public_id, safe="")
        return self.request(
            "POST", f"/internal/v1/executor/hermes-auth/clients/{instance_id}", payload
        )

    def delete_hermes_auth_client(self, instance_public_id):
        instance_id = urllib.parse.quote(instance_public_id, safe="")
        return self.request("DELETE", f"/internal/v1/executor/hermes-auth/clients/{instance_id}")


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


def consume_provisioning_secret(secret_path):
    path = Path(secret_path).resolve()
    if path.parent != PROVISIONING_SECRET_DIR.resolve() or not path.is_file():
        raise ValueError("invalid provisioning secret")
    try:
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


def sanitize_creation_error(output, password=""):
    sanitized = output or ""
    if password:
        sanitized = sanitized.replace(password, "[REDACTED]")
    lines = sanitized.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"(?i)\b(password|token)\b", line):
            lines[index] = "[REDACTED]"
        if line.strip().lower() == "login token:" and index + 1 < len(lines):
            lines[index + 1] = "[REDACTED]"
    return "\n".join(lines)[-CREATE_ERROR_OUTPUT_LENGTH:]


def _read_openclaw_config(instance):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise RuntimeError("secure config read is not supported")
    data_path = instance.get("data_path")
    if not isinstance(data_path, str) or not data_path:
        user_id = instance.get("legacy_user_id")
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("instance data path is required")
        data_path = str(PUBLIC_DIR / "users" / user_id)
    data_root = Path(data_path).resolve()
    try:
        data_root.relative_to(PUBLIC_DIR.resolve())
    except ValueError as exc:
        raise ValueError("instance data path is outside public directory") from exc
    fds = [os.open(data_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for part in ("config",):
            fds.append(os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fds[-1],
            ))
        fds.append(os.open(
            "openclaw.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fds[-1]
        ))
        with os.fdopen(fds[-1], "r", encoding="utf-8") as config_file:
            fds[-1] = None
            return json.load(config_file)
    finally:
        for fd in reversed(fds):
            if fd is not None:
                os.close(fd)


def openclaw_creation_result(instance):
    user_id = instance["legacy_user_id"]
    config = _read_openclaw_config(instance)
    nginx_conf = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf")) / f"{user_id}.conf"
    match = re.search(r"^\s*listen\s+([0-9]+)\b", nginx_conf.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ValueError("created instance port not found")
    port = int(match.group(1))
    public_host = os.environ.get("PUBLIC_HOST", "").strip()
    version = instance.get("_creation_version") or os.environ.get("OPENCLAW_VERSION", "").strip()
    auth = config.get("gateway", {}).get("auth", {})
    auth_mode = auth.get("mode", "token")
    token = auth.get("token", "")
    htpasswd = os.environ.get("NGINX_HTPASSWD_FILE_IN_CONTAINER", "").strip()
    if (
        not public_host or not version or not htpasswd
        or auth_mode not in {"token", "trusted-proxy"}
        or (auth_mode == "token" and not token)
    ):
        raise ValueError("created instance metadata is incomplete")
    base_path = config.get("gateway", {}).get("controlUi", {}).get("basePath", "")
    base_path = f"/{str(base_path).strip('/')}" if base_path else ""
    access_url = f"https://{public_host}:{port}{base_path}"
    return {
        "port": port,
        "version": version,
        "access_url": access_url,
        "admin_url": f"{access_url}/admin/",
        "basic_auth_password_ref": f"nginx-auth:{Path(htpasswd).parent}/users/{user_id}/.htpasswd",
        "openclaw_token": token,
        "auth_mode": auth_mode,
    }


def hermes_creation_result(instance):
    port = instance.get("_created_port")
    public_host = os.environ.get("PUBLIC_HOST", "").strip()
    if not public_host or not isinstance(port, int):
        raise ValueError("created Hermes metadata is incomplete")
    access_url = f"https://{public_host}:{port}"
    return {
        "port": port,
        "version": instance.get("_creation_version") or os.environ.get("HERMES_VERSION", "v2026.7.20"),
        "access_url": access_url,
        "admin_url": access_url,
        "basic_auth_password_ref": None,
        "openclaw_token": "",
        "auth_mode": "session",
    }


def evoscientist_creation_result(instance):
    port = instance.get("_created_port")
    public_host = os.environ.get("PUBLIC_HOST", "").strip()
    if not public_host or not isinstance(port, int):
        raise ValueError("created EvoScientist metadata is incomplete")
    configured_version = instance.get("_creation_version")
    image = (
        f"ghcr.io/evoscientist/evoscientist:{configured_version}"
        if configured_version == "latest"
        else f"ghcr.io/evoscientist/evoscientist@{configured_version}"
        if configured_version
        else os.environ.get(
        "EVOSCIENTIST_IMAGE",
        "ghcr.io/evoscientist/evoscientist@sha256:ca1fd303d7ca2d1bfad97d9872b4ee910eea67c46047be1bf59463941fff3c47",
        ).strip()
    )
    return {
        "port": port, "version": configured_version or image.removeprefix("ghcr.io/evoscientist/evoscientist@"),
        "access_url": f"https://{public_host}:{port}",
        "admin_url": f"https://{public_host}:{port}",
        "basic_auth_password_ref": f"nginx-auth:/etc/nginx/auth/users/{instance['legacy_user_id']}/.htpasswd",
        "openclaw_token": "",
        "auth_mode": "none",
    }


def creation_result(instance):
    if instance["product"] == "hermes":
        return hermes_creation_result(instance)
    if instance["product"] == "evoscientist":
        return evoscientist_creation_result(instance)
    return openclaw_creation_result(instance)


def reload_nginx_after_create(adapter):
    code, output = adapter.run_command(
        ["docker", "compose", "up", "-d"], timeout=90,
        cwd=Path(os.environ.get("NGINX_COMPOSE_DIR", "/data/docker/nginx/compose")),
    )
    if code != 0:
        return code, output
    connect_code, connect_output = adapter.run_command(
        [
            "bash", "-lc",
            'source "$1"; connect_shared_services_to_tenant_networks "$2" "$3"',
            "bash",
            str(adapter.manager_dir / "scripts" / "lib_tenant_network.sh"),
            adapter.nginx_container_name,
            os.environ.get("MODEL_PROXY_CONTAINER_NAME", "openclaw-model-proxy"),
        ],
        timeout=300,
    )
    if connect_code != 0:
        return connect_code, connect_output
    reload_code, reload_output = adapter.reload_nginx()
    return reload_code, "\n".join(
        part for part in (output, connect_output, reload_output) if part
    )


def rollback_created_instance(adapter, instance):
    if instance["product"] in {"hermes", "evoscientist"}:
        return adapter.delete(instance)
    return adapter.run_command(
        [str(adapter.manager_dir / "scripts" / "delete_user.sh"), instance["legacy_user_id"]],
        timeout=180,
        env={**os.environ, "OPENCLAW_SKIP_METADATA_WRITE": "1"},
    )


def get_adapter(product):
    adapter_type = {
        "openclaw": OpenClawDockerAdapter,
        "evoscientist": EvoScientistDockerAdapter,
        "hermes": HermesDockerAdapter,
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
        adapter = adapter_factory(instance["product"])
        capability = execution_action_capability(job["action"])
        if capability is None or not adapter.supports(capability):
            raise ValueError(
                f"instance product does not support {capability or job['action']}"
            )
        if instance.get("status") == "deleted" and action not in {"restore", "purge_deleted"}:
            raise ValueError("deleted instance only supports restore or permanent deletion")
        if action == "cleanup_failed":
            control.update(request_id, "running", current_step="cleaning failed instance")
            code, output = adapter.cleanup_failed(instance)
            if code == 0:
                control.update(request_id, "succeeded", output="failed instance resources cleaned")
            else:
                control.update(request_id, "failed", error_summary="failed instance cleanup failed", output=output)
            return True
        if action == "create":
            control.update(request_id, "running", current_step="creating instance")
            password = (
                "" if instance["product"] == "hermes"
                else consume_provisioning_secret(job["params"]["secret_path"])
            )
            instance["_creation_version"] = job["params"].get("version")
            if instance["product"] == "openclaw":
                instance["_creation_auth_mode"] = "trusted-proxy"
            create_kwargs = {
                "skip_nginx_reload": True,
                "skip_metadata_write": True,
            }
            if job["params"].get("version"):
                create_kwargs["version"] = job["params"]["version"]
            if instance["product"] == "hermes":
                create_kwargs["hermes_auth_client_callback"] = lambda payload: (
                    control.create_hermes_auth_client(instance["public_id"], payload)
                )
            code, failure_output = adapter.create(
                instance,
                "true" if instance.get("basic_auth_enabled") else "false",
                password,
                **create_kwargs,
            )
            created = code == 0
            if created:
                if instance["product"] in {"hermes", "evoscientist"}:
                    code, failure_output = adapter.configure_ingress(instance)
                else:
                    code, failure_output = reload_nginx_after_create(adapter)
            if code == 0:
                try:
                    result = creation_result(instance)
                    control.update(
                        request_id, "succeeded", output="instance created", result=result
                    )
                    return True
                except Exception as exc:
                    code = 1
                    failure_output = str(exc)
            if instance.pop("_hermes_auth_client_created", False):
                control.delete_hermes_auth_client(instance["public_id"])
            rollback_code = rollback_created_instance(adapter, instance)[0] if created else 0
            rollback_note = (
                "resources removed"
                if instance["product"] in {"hermes", "evoscientist"} and created and rollback_code == 0
                else "resources moved to recycle bin"
                if created and rollback_code == 0
                else "create script rolled back resources"
                if not created
                else "manual cleanup required"
            )
            detail = sanitize_creation_error(failure_output, password)
            control.update(
                request_id,
                "failed",
                error_summary="instance creation failed",
                output=f"instance creation failed; {rollback_note}\n{detail}".rstrip(),
            )
            return True
        if action == "wechat_bind":
            instance = control.get_runtime_instance(
                job["instance_public_id"], job["actor_user_public_id"]
            )
            if (
                instance.get("product") != "openclaw"
                or instance.get("access_role") not in {"owner", "manager"}
            ):
                raise ValueError("device pairing is not supported")
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
        if action == "set_basic_auth":
            control.update(request_id, "running", current_step="updating Basic Auth")
            code, output = adapter.set_basic_auth(instance, job["params"]["enabled"])
            output = output[-MAX_OUTPUT_LENGTH:]
            if code == 0:
                control.update(request_id, "succeeded", output=output)
            else:
                control.update(
                    request_id,
                    "failed",
                    error_summary="Basic Auth update failed",
                    output=output,
                )
            return True
        if action == "update_version":
            params = job["params"]
            control.update(request_id, "running", current_step="updating version")
            code, output = adapter.update_version(
                instance,
                params["version"],
                restore_model_provider=params["restore_model_provider"],
            )
            output = output[-MAX_OUTPUT_LENGTH:]
            if code == 0:
                control.update(request_id, "succeeded", output=output)
            else:
                control.update(
                    request_id,
                    "failed",
                    error_summary="version update failed",
                    output=output,
                )
            return True
        if action == "install_skill":
            control.update(request_id, "running", current_step="installing skill")
            if not adapter.status(instance).startswith("Up"):
                control.update(
                    request_id,
                    "failed",
                    error_summary="instance is not running",
                    output="Skill installation requires a running instance.",
                )
                return True
            code, output = adapter.install_skill(
                instance, job["params"]["skill_id"], request_id=request_id
            )
            output = output[-MAX_OUTPUT_LENGTH:]
            if code == 0:
                control.update(request_id, "succeeded", output=output)
            else:
                control.update(
                    request_id,
                    "failed",
                    error_summary="skill installation failed",
                    output=output,
                )
            return True
        if action == "set_model_provider":
            control.update(request_id, "running", current_step="setting model provider")
            if not adapter.status(instance).startswith("Up"):
                control.update(
                    request_id, "failed", error_summary="instance is not running",
                    output="Model provider update requires a running instance.",
                )
                return True
            params = job["params"]
            code, output = adapter.set_model_provider(
                instance, params["model_provider_id"], params["model_id"],
                params["model_base_url"], params["model_alias"],
            )
            output = output[-MAX_OUTPUT_LENGTH:]
            if code == 0:
                control.update(request_id, "succeeded", output=output)
            else:
                control.update(
                    request_id, "failed", error_summary="model provider update failed",
                    output=output,
                )
            return True
        if action in {"refresh_devices", "approve_latest_device"}:
            control.update(request_id, "running", current_step=action.replace("_", " "))
            if not adapter.status(instance).startswith("Up"):
                control.update(
                    request_id,
                    "failed",
                    error_summary="instance is not running",
                    output="Device operation requires a running instance.",
                )
                return True
            if action == "approve_latest_device":
                code, output = adapter.approve_latest_device(
                    instance, request_id=request_id
                )
            else:
                code, output = adapter.refresh_devices(instance)
            output = output[-MAX_OUTPUT_LENGTH:]
            if code == 0:
                control.update(request_id, "succeeded", output=output)
            else:
                control.update(
                    request_id,
                    "failed",
                    error_summary=f"{action.replace('_', ' ')} failed",
                    output=output,
                )
            return True
        if action in {"delete", "restore", "purge_deleted"}:
            control.update(request_id, "running", current_step=f"{action} instance")
            if action == "delete" and instance.get("status") == "deleted":
                raise ValueError("instance is already deleted")
            if action == "restore" and (
                instance.get("status") != "deleted"
                or instance.get("restore_state") != "restorable"
            ):
                raise ValueError("instance is not restorable")
            if action == "purge_deleted" and (
                instance.get("status") != "deleted"
                or instance.get("restore_state") != "restorable"
            ):
                raise ValueError("instance is not eligible for permanent deletion")
            if action == "restore" and instance["product"] == "hermes":
                code, output = adapter.restore(
                    instance,
                    hermes_auth_client_callback=lambda payload: (
                        control.create_hermes_auth_client(instance["public_id"], payload)
                    ),
                )
            else:
                code, output = getattr(adapter, action)(instance)
            output = output[-MAX_OUTPUT_LENGTH:]
            if code == 0:
                if action == "delete" and instance["product"] == "hermes":
                    control.delete_hermes_auth_client(instance["public_id"])
                control.update(request_id, "succeeded", output=output)
            else:
                if instance.pop("_hermes_auth_client_created", False):
                    control.delete_hermes_auth_client(instance["public_id"])
                control.update(
                    request_id,
                    "failed",
                    error_summary=f"instance {action} failed",
                    output=output,
                )
            return True
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"unsupported execution action: {job['action']}")
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
