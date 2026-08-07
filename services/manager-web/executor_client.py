import os
import urllib.parse

import requests


BASE_URL = os.environ.get(
    "MANAGER_EXECUTOR_BASE_URL", "http://manager-executor-api:8083"
).rstrip("/")
TOKEN = os.environ.get(
    "MANAGER_CONTROL_ADMIN_WEB_TOKEN"
    if os.environ.get("MANAGER_WEB_ROLE") == "admin"
    else "MANAGER_CONTROL_USER_WEB_TOKEN",
    "",
).strip()
TIMEOUT = int(os.environ.get("MANAGER_EXECUTOR_HTTP_TIMEOUT", "120"))


class ExecutorError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def request(method, path, actor_public_id, **kwargs):
    try:
        response = requests.request(
            method,
            f"{BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-Actor-User-Public-Id": actor_public_id,
            },
            timeout=TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise ExecutorError(f"executor unavailable: {exc}") from exc
    if response.status_code >= 400:
        try:
            message = response.json().get("error", response.text)
        except ValueError:
            message = response.text
        raise ExecutorError(
            message or f"executor returned {response.status_code}",
            status_code=response.status_code,
        )
    return response


def snapshot(actor_public_id, instance_public_id):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    return request(
        "GET", f"/internal/v1/instances/{instance_id}/snapshot", actor_public_id
    ).json()


def admin_instance_statuses(actor_public_id, instance_public_ids):
    return request(
        "POST", "/internal/v1/admin/instance-statuses", actor_public_id,
        json={"instance_public_ids": instance_public_ids},
    ).json()["statuses"]


def collect_activity_snapshots(actor_public_id, instance_public_ids):
    return request(
        "POST", "/internal/v1/admin/activity-snapshots/collect", actor_public_id,
        json={"instance_public_ids": instance_public_ids},
    ).json()["results"]


def device_action(actor_public_id, instance_public_id, action):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    return request(
        "POST", f"/internal/v1/instances/{instance_id}/devices/{action}", actor_public_id
    ).json()


def upload(actor_public_id, instance_public_id, uploaded):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    return request(
        "POST", f"/internal/v1/instances/{instance_id}/files", actor_public_id,
        files={"file": (uploaded.filename, uploaded.stream, uploaded.mimetype)},
    ).json()


def download(actor_public_id, instance_public_id, root_key, relative_path):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    root = urllib.parse.quote(root_key, safe="")
    relative = urllib.parse.quote(relative_path, safe="")
    return request(
        "GET", f"/internal/v1/instances/{instance_id}/files/{root}/{relative}",
        actor_public_id, stream=True,
    )


def delete(actor_public_id, instance_public_id, root_key, relative_path):
    instance_id = urllib.parse.quote(instance_public_id, safe="")
    root = urllib.parse.quote(root_key, safe="")
    relative = urllib.parse.quote(relative_path, safe="")
    request(
        "DELETE", f"/internal/v1/instances/{instance_id}/files/{root}/{relative}",
        actor_public_id,
    )
