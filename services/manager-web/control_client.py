import json
import os
import urllib.error
import urllib.parse
import urllib.request
import secrets


BASE_URL = os.environ.get(
    "MANAGER_CONTROL_BASE_URL",
    "http://manager-control:8082",
).rstrip("/")
SERVICE_TOKEN = os.environ.get(
    "MANAGER_CONTROL_ADMIN_WEB_TOKEN"
    if os.environ.get("MANAGER_WEB_ROLE") == "admin"
    else "MANAGER_CONTROL_USER_WEB_TOKEN",
    "",
).strip()
TIMEOUT = int(os.environ.get("MANAGER_CONTROL_TIMEOUT", "5"))


class ControlError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def request_json(method, path, *, actor_public_id=None, payload=None):
    if not SERVICE_TOKEN:
        raise ControlError(503, "manager-control user service token is not configured")
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    if actor_public_id:
        headers["X-Actor-User-Public-Id"] = actor_public_id
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", str(exc))
        except (ValueError, AttributeError):
            message = str(exc)
        raise ControlError(exc.code, message) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ControlError(503, "manager-control is unavailable") from exc
    return json.loads(body) if body else {}


def list_instances(user_public_id):
    user_id = urllib.parse.quote(user_public_id, safe="")
    response = request_json(
        "GET",
        f"/internal/v1/users/{user_id}/instances",
        actor_public_id=user_public_id,
    )
    return response["instances"]


def get_instance(user_public_id, instance_public_id):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    response = request_json(
        "GET",
        f"/internal/v1/instances/{instance_id}",
        actor_public_id=user_public_id,
    )
    return response["instance"]


def list_members(user_public_id, instance_public_id):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    response = request_json(
        "GET",
        f"/internal/v1/instances/{instance_id}/members",
        actor_public_id=user_public_id,
    )
    return response["members"]


def add_member(user_public_id, instance_public_id, username, role):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    response = request_json(
        "POST",
        f"/internal/v1/instances/{instance_id}/members",
        actor_public_id=user_public_id,
        payload={"username": username, "role": role},
    )
    return response["member"]


def remove_member(user_public_id, instance_public_id, member_public_id):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    member_id = urllib.parse.quote(member_public_id, safe="")
    request_json(
        "DELETE",
        f"/internal/v1/instances/{instance_id}/members/{member_id}",
        actor_public_id=user_public_id,
    )


def resolve_session(token_hash, provider):
    query = urllib.parse.urlencode({"token_hash": token_hash, "provider": provider})
    return request_json("GET", f"/internal/v1/auth/session?{query}")["user"]


def resolve_identity(provider, subject):
    query = urllib.parse.urlencode({"provider": provider, "subject": subject})
    return request_json("GET", f"/internal/v1/auth/identity?{query}")["user"]


def local_login(payload):
    return request_json("POST", "/internal/v1/auth/local-login", payload=payload)["user"]


def external_login(payload):
    return request_json("POST", "/internal/v1/auth/external-login", payload=payload)["user"]


def emergency_login(payload):
    return request_json("POST", "/internal/v1/auth/emergency-login", payload=payload)["user"]


def delete_session(token_hash):
    request_json(
        "DELETE", "/internal/v1/auth/session", payload={"token_hash": token_hash}
    )


def list_admin_instances():
    return request_json("GET", "/internal/v1/admin/instances")["instances"]


def get_admin_metadata():
    return request_json("GET", "/internal/v1/admin/metadata")


def create_execution_job(payload):
    return request_json("POST", "/internal/v1/execution-jobs", payload=payload)["job"]


def create_device_batch(payload):
    return request_json(
        "POST", "/internal/v1/admin/device-batches", payload=payload
    )


def get_device_batch(request_id):
    batch_id = urllib.parse.quote(request_id, safe="")
    return request_json("GET", f"/internal/v1/admin/device-batches/{batch_id}")


def create_action_batch(payload):
    return request_json(
        "POST", "/internal/v1/admin/action-batches", payload=payload
    )


def get_action_batch(request_id):
    batch_id = urllib.parse.quote(request_id, safe="")
    return request_json("GET", f"/internal/v1/admin/action-batches/{batch_id}")


def create_wechat_bind(actor_public_id, instance_public_id):
    return request_json(
        "POST", "/internal/v1/execution-jobs",
        actor_public_id=actor_public_id,
        payload={
            "request_id": f"wechat-bind-{secrets.token_urlsafe(12)}",
            "actor_user_public_id": actor_public_id,
            "instance_public_id": instance_public_id,
            "action": "instance.wechat_bind",
            "params": {},
        },
    )["job"]


def get_execution_job(request_id, actor_public_id):
    job_id = urllib.parse.quote(request_id, safe="")
    return request_json(
        "GET", f"/internal/v1/execution-jobs/{job_id}",
        actor_public_id=actor_public_id,
    )["job"]


def get_wechat_bind_job(actor_public_id, instance_public_id):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    return request_json(
        "GET", f"/internal/v1/instances/{instance_id}/wechat-bind-job",
        actor_public_id=actor_public_id,
    )["job"]


def cancel_execution_job(request_id, actor_public_id):
    job_id = urllib.parse.quote(request_id, safe="")
    return request_json(
        "POST", f"/internal/v1/execution-jobs/{job_id}/cancel",
        actor_public_id=actor_public_id,
    )["job"]
