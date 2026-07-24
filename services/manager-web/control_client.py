import json
import os
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.environ.get(
    "MANAGER_CONTROL_BASE_URL",
    "http://manager-control:8082",
).rstrip("/")
SERVICE_TOKEN = os.environ.get("MANAGER_CONTROL_USER_WEB_TOKEN", "").strip()
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
