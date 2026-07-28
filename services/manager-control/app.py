import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request
from werkzeug.security import check_password_hash

import metadata_store
from product_capabilities import execution_action_capability, product_supports


DB_FILE = Path(
    os.environ.get(
        "METADATA_DB_FILE",
        "/data/docker/openclaw-public/manager.db",
    )
)
EXECUTOR_STALE_SECONDS = max(
    1, int(os.environ.get("MANAGER_EXECUTOR_STALE_SECONDS", "900"))
)
TOKEN_ENV = {
    "manager-user-web": "MANAGER_CONTROL_USER_WEB_TOKEN",
    "manager-admin-web": "MANAGER_CONTROL_ADMIN_WEB_TOKEN",
    "manager-executor": "MANAGER_CONTROL_EXECUTOR_TOKEN",
}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_.@/-]{1,128}$")
JOB_STATUSES = {
    "queued",
    "running",
    "succeeded",
    "failed",
    "partial_failure",
    "interrupted",
    "cancelled",
}
JOB_ACTION_PARAMS = {
    "instance.start": set(),
    "instance.stop": set(),
    "instance.restart": {"reason"},
    "instance.set_basic_auth": {"enabled"},
    "instance.update_version": {"version", "restore_model_provider"},
    "instance.install_skill": {"skill_id"},
    "instance.refresh_devices": set(),
    "instance.approve_latest_device": set(),
    "instance.delete": set(),
    "instance.restore": set(),
    "instance.wechat_bind": set(),
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


def portal_instance(instance):
    return {
        "public_id": instance["public_id"],
        "legacy_user_id": instance.get("legacy_user_id"),
        "product": instance["product"],
        "instance_name": instance["instance_name"],
        "status": instance["status"],
        "version": instance.get("openclaw_version"),
        "port": instance.get("port"),
        "access_url": instance.get("access_url"),
        "access_role": instance["access_role"],
        "created_at": instance["created_at"],
        "updated_at": instance["updated_at"],
    }


def admin_metadata_instance(instance):
    return {
        "public_id": instance["public_id"],
        "legacy_user_id": instance.get("legacy_user_id"),
        "product": instance["product"],
        "instance_name": instance["instance_name"],
        "status": instance["status"],
        "port": instance.get("port"),
        "version": instance.get("openclaw_version"),
        "basic_auth_enabled": bool(instance.get("basic_auth_enabled")),
        "created_at": instance["created_at"],
        "updated_at": instance["updated_at"],
    }


def configured_tokens():
    return {
        service: token
        for service, env_name in TOKEN_ENV.items()
        if (token := os.environ.get(env_name, "").strip())
    }


def service_tokens_valid(tokens):
    return len(tokens) == len(TOKEN_ENV) and len(set(tokens.values())) == len(TOKEN_ENV)


def bearer_token():
    value = request.headers.get("Authorization", "")
    if not value.lower().startswith("bearer "):
        return ""
    return value.split(None, 1)[1].strip()


def actor_public_id():
    return request.headers.get("X-Actor-User-Public-Id", "").strip()


def member_payload(member):
    return {
        "user_public_id": member["user_public_id"],
        "username": member["username"],
        "display_name": member["display_name"],
        "role": member["role"],
    }


def execution_job_payload(
    job,
    actor_user_public_id=None,
    instance_public_id=None,
):
    return {
        "request_id": job["request_id"],
        "parent_request_id": job["parent_request_id"],
        "actor_user_public_id": actor_user_public_id
        or job.get("actor_user_public_id"),
        "instance_public_id": instance_public_id
        or job.get("instance_public_id"),
        "action": job["action"],
        "params": json.loads(job["params_json"]),
        "status": job["status"],
        "current_step": job["current_step"],
        "error_summary": job["error_summary"],
        "output": job["output"],
    }


def device_batch_child_payload(job):
    value = execution_job_payload(job)
    output = job.get("output") or ""
    if job["status"] in {"queued", "running"}:
        summary = job["status"]
    elif job["status"] != "succeeded":
        summary = job.get("error_summary") or "failed"
    elif "No pending device request found" in output:
        summary = "No pending request"
    elif job["action"] == "instance.refresh_devices" and re.search(
        r"^Pending(?:\s*\([1-9][0-9]*\)|\s*$)", output, re.MULTILINE
    ):
        summary = "Pending request found"
    elif job["action"] == "instance.approve_latest_device":
        summary = "Approved successfully"
    else:
        summary = "No pending request"
    value["summary"] = summary
    value["output"] = None
    return value


def authenticated_user_payload(user):
    return {
        "id": user["id"],
        "public_id": user["public_id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "email": user["email"],
        "role": user["role"],
        "status": user["status"],
        "provider": user["provider"],
        "session_kind": user["session_kind"],
        "csrf_token": user["csrf_token"],
    }


def executor_instance_payload(instance):
    return {
        "public_id": instance["public_id"],
        "legacy_user_id": instance.get("legacy_user_id"),
        "product": instance["product"],
        "runtime_identifier": instance["runtime_identifier"],
        "data_path": instance.get("data_path"),
        "status": instance["status"],
        "restore_state": instance.get("restore_state"),
        "access_role": instance.get("access_role"),
    }


def require_services(*allowed_services):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            tokens = configured_tokens()
            if not service_tokens_valid(tokens):
                return jsonify({"error": "manager-control service tokens are invalid"}), 503
            provided = bearer_token()
            service = next(
                (
                    name
                    for name, token in tokens.items()
                    if provided and hmac.compare_digest(provided, token)
                ),
                None,
            )
            if service is None:
                return jsonify({"error": "invalid service token"}), 401
            if service not in allowed_services:
                return jsonify({"error": "service is not allowed"}), 403
            g.source_service = service
            return view(*args, **kwargs)

        return wrapped

    return decorator


@app.get("/health")
def health():
    tokens_valid = service_tokens_valid(configured_tokens())
    try:
        database_uri = f"{DB_FILE.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as conn:
            version = conn.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
    except sqlite3.Error:
        return jsonify(
            {
                "ok": False,
                "schema_version": None,
                "service_tokens_configured": tokens_valid,
            }
        ), 503
    ready = version == 4 and tokens_valid
    return jsonify(
        {
            "ok": ready,
            "schema_version": version,
            "service_tokens_configured": tokens_valid,
        }
    ), (
        200 if ready else 503
    )


@app.get("/internal/v1/auth/session")
@require_services("manager-user-web", "manager-admin-web")
def resolve_auth_session():
    token_hash = request.args.get("token_hash", "")
    provider = request.args.get("provider", "")
    if not token_hash or not provider:
        return jsonify({"error": "token_hash and provider are required"}), 400
    metadata_store.activate_auth_provider(provider, db_file=DB_FILE)
    user = metadata_store.get_session(token_hash, db_file=DB_FILE)
    if user is None or user["provider"] != provider or user["status"] != "active":
        return jsonify({"error": "active session not found"}), 404
    if g.source_service == "manager-admin-web" and user["role"] != "admin":
        return jsonify({"error": "administrator role is required"}), 403
    return jsonify({"user": authenticated_user_payload(user)})


@app.delete("/internal/v1/auth/session")
@require_services("manager-user-web", "manager-admin-web")
def delete_auth_session():
    payload = request.get_json(silent=True) or {}
    token_hash = payload.get("token_hash")
    if not isinstance(token_hash, str) or not token_hash:
        return jsonify({"error": "token_hash is required"}), 400
    metadata_store.delete_session(token_hash, db_file=DB_FILE)
    return "", 204


@app.get("/internal/v1/auth/identity")
@require_services("manager-user-web", "manager-admin-web")
def resolve_auth_identity():
    provider = request.args.get("provider", "")
    subject = request.args.get("subject", "")
    if not provider or not subject:
        return jsonify({"error": "provider and subject are required"}), 400
    metadata_store.activate_auth_provider(provider, db_file=DB_FILE)
    user = metadata_store.get_user_by_identity(provider, subject, db_file=DB_FILE)
    if user is None or user["status"] != "active":
        return jsonify({"error": "active identity not found"}), 404
    if g.source_service == "manager-admin-web" and user["role"] != "admin":
        return jsonify({"error": "administrator role is required"}), 403
    user = {**user, "provider": provider, "session_kind": "proxy", "csrf_token": ""}
    return jsonify({"user": authenticated_user_payload(user)})


@app.post("/internal/v1/auth/external-login")
@require_services("manager-user-web", "manager-admin-web")
def external_auth_login():
    payload = request.get_json(silent=True) or {}
    required = {"provider", "subject", "token_hash", "csrf_token", "expires_at"}
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in required):
        return jsonify({"error": "invalid external login payload"}), 400
    metadata_store.activate_auth_provider(payload["provider"], db_file=DB_FILE)
    user = metadata_store.get_user_by_identity(
        payload["provider"], payload["subject"], db_file=DB_FILE
    )
    if user is None or user["status"] != "active":
        return jsonify({"error": "active identity not found"}), 403
    if g.source_service == "manager-admin-web" and user["role"] != "admin":
        return jsonify({"error": "administrator role is required"}), 403
    metadata_store.record_identity_login(
        payload["provider"], payload["subject"], payload.get("profile", {}), db_file=DB_FILE
    )
    metadata_store.create_session(
        payload["token_hash"], user["id"], payload["provider"],
        payload["csrf_token"], payload["expires_at"],
        session_kind="admin" if g.source_service == "manager-admin-web" else "user",
        db_file=DB_FILE,
    )
    session = metadata_store.get_session(payload["token_hash"], db_file=DB_FILE)
    return jsonify({"user": authenticated_user_payload(session)})


@app.post("/internal/v1/auth/emergency-login")
@require_services("manager-admin-web")
def emergency_auth_login():
    payload = request.get_json(silent=True) or {}
    required = {"username", "provider", "token_hash", "csrf_token", "expires_at"}
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in required):
        return jsonify({"error": "invalid emergency login payload"}), 400
    user = metadata_store.get_user_by_username(payload["username"], db_file=DB_FILE)
    if user is None or user["status"] != "active" or user["role"] != "admin":
        return jsonify({"error": "active administrator not found"}), 403
    metadata_store.create_session(
        payload["token_hash"], user["id"], payload["provider"],
        payload["csrf_token"], payload["expires_at"], session_kind="emergency",
        db_file=DB_FILE,
    )
    session = metadata_store.get_session(payload["token_hash"], db_file=DB_FILE)
    return jsonify({"user": authenticated_user_payload(session)})


@app.post("/internal/v1/auth/local-login")
@require_services("manager-user-web", "manager-admin-web")
def local_auth_login():
    payload = request.get_json(silent=True) or {}
    required = {"username", "password", "token_hash", "csrf_token", "expires_at"}
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in required):
        return jsonify({"error": "invalid local login payload"}), 400
    metadata_store.activate_auth_provider("local", db_file=DB_FILE)
    user = metadata_store.get_user_by_identity(
        "local",
        metadata_store.normalize_username(payload["username"]),
        db_file=DB_FILE,
    )
    credential = (
        metadata_store.get_local_credential(user["id"], db_file=DB_FILE)
        if user
        else None
    )
    locked = False
    if credential and credential.get("locked_until"):
        try:
            locked = datetime.fromisoformat(credential["locked_until"]) > datetime.now(
                timezone.utc
            )
        except ValueError:
            locked = True
    valid = (
        user is not None
        and user["status"] == "active"
        and credential is not None
        and not locked
        and check_password_hash(credential["password_hash"], payload["password"])
    )
    if not valid:
        if user and credential and not locked:
            metadata_store.record_login_failure(user["id"], db_file=DB_FILE)
        return jsonify({"error": "invalid username or password"}), 401
    if g.source_service == "manager-admin-web" and user["role"] != "admin":
        return jsonify({"error": "administrator role is required"}), 403
    metadata_store.reset_login_failures(user["id"], db_file=DB_FILE)
    metadata_store.create_session(
        payload["token_hash"],
        user["id"],
        "local",
        payload["csrf_token"],
        payload["expires_at"],
        session_kind="admin" if g.source_service == "manager-admin-web" else "user",
        db_file=DB_FILE,
    )
    session = metadata_store.get_session(payload["token_hash"], db_file=DB_FILE)
    return jsonify({"user": authenticated_user_payload(session)})


@app.get("/internal/v1/users/<user_public_id>/instances")
@require_services("manager-user-web", "manager-admin-web")
def user_instances(user_public_id):
    if g.source_service == "manager-user-web":
        if actor_public_id() != user_public_id:
            return jsonify({"error": "user service cannot impersonate another user"}), 403
        actor = metadata_store.get_user_by_public_id(
            user_public_id,
            db_file=DB_FILE,
        )
        if actor is None or actor["status"] != "active":
            return jsonify({"error": "active actor user is required"}), 403
    instances = metadata_store.list_instances_for_user(
        user_public_id,
        db_file=DB_FILE,
    )
    return jsonify({"instances": [portal_instance(instance) for instance in instances]})


@app.get("/internal/v1/admin/instances")
@require_services("manager-admin-web")
def admin_instances():
    instances = metadata_store.list_instances(db_file=DB_FILE)
    return jsonify(
        {
            "instances": [
                {
                    "public_id": instance["public_id"],
                    "legacy_user_id": instance.get("legacy_user_id"),
                    "product": instance["product"],
                    "instance_name": instance["instance_name"],
                    "status": instance["status"],
                    "restore_state": instance.get("restore_state"),
                    "access_url": instance.get("access_url"),
                    "version": instance.get("openclaw_version"),
                    "basic_auth_enabled": bool(instance.get("basic_auth_enabled")),
                    "capabilities": sorted(
                        capability
                        for capability in (
                            "basic_auth", "update_version", "skill_install", "device_pairing",
                            "delete", "restore",
                        )
                        if product_supports(instance["product"], capability)
                    ),
                }
                for instance in instances
            ]
        }
    )


@app.get("/internal/v1/admin/metadata")
@require_services("manager-admin-web")
def admin_metadata():
    with metadata_store.connect(DB_FILE) as conn:
        all_counts = metadata_store.table_counts(conn=conn)
        counts = {
            key: all_counts[key]
            for key in ("instances", "ports", "instance_credentials", "operation_records")
        }
        instances = metadata_store.list_instances(conn=conn)[:20]
        operations = metadata_store.list_operation_events(20, conn=conn)
    return jsonify(
        {
            "counts": counts,
            "instances": [admin_metadata_instance(item) for item in instances],
            "operations": operations,
        }
    )


@app.post("/internal/v1/admin/device-batches")
@require_services("manager-admin-web")
def create_device_batch():
    payload = request.get_json(silent=True) or {}
    allowed = {"request_id", "actor_user_public_id", "action", "instance_public_ids"}
    if set(payload) - allowed:
        return jsonify({"error": "unsupported batch fields"}), 400
    request_id = payload.get("request_id")
    actor_public_id = payload.get("actor_user_public_id")
    batch_action = payload.get("action")
    instance_public_ids = payload.get("instance_public_ids")
    if (
        not isinstance(request_id, str)
        or not REQUEST_ID_RE.fullmatch(request_id)
        or len(request_id) > 120
    ):
        return jsonify({"error": "invalid request_id"}), 400
    if batch_action not in {"preview", "approve"}:
        return jsonify({"error": "invalid batch action"}), 400
    if (
        not isinstance(instance_public_ids, list)
        or not 1 <= len(instance_public_ids) <= 100
        or any(not isinstance(value, str) or not value for value in instance_public_ids)
        or len(set(instance_public_ids)) != len(instance_public_ids)
    ):
        return jsonify({"error": "instance_public_ids must contain 1-100 unique IDs"}), 400
    actor = metadata_store.get_user_by_public_id(actor_public_id, db_file=DB_FILE)
    if actor is None or actor["status"] != "active" or actor["role"] != "admin":
        return jsonify({"error": "active admin actor is required"}), 403

    child_action = (
        "instance.refresh_devices"
        if batch_action == "preview"
        else "instance.approve_latest_device"
    )
    parent_action = f"batch.device_{batch_action}"
    params = {"instance_public_ids": instance_public_ids}
    try:
        with metadata_store.connect(DB_FILE) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = metadata_store.get_execution_job(request_id, conn=conn)
            if existing is not None:
                parent = metadata_store.create_execution_job(
                    request_id=request_id, actor_user_id=actor["id"],
                    action=parent_action, params=params, conn=conn,
                )
                children = metadata_store.list_execution_jobs(
                    parent_request_id=request_id, limit=100, conn=conn
                )
            else:
                instances = []
                for instance_public_id in instance_public_ids:
                    instance = metadata_store.get_instance_by_public_id(
                        instance_public_id, conn=conn
                    )
                    if instance is None:
                        raise ValueError(f"instance not found: {instance_public_id}")
                    if instance["status"] == "deleted" or not product_supports(
                        instance["product"], "device_pairing"
                    ):
                        raise ValueError(
                            f"device pairing is not available: {instance_public_id}"
                        )
                    active_jobs = metadata_store.list_execution_jobs(
                        statuses=("queued", "running"), limit=1,
                        instance_public_id=instance_public_id,
                        action=child_action, conn=conn,
                    )
                    if active_jobs:
                        raise ValueError(
                            f"device task is already active: {instance_public_id}"
                        )
                    for retention_action in ("instance.delete", "instance.restore"):
                        if metadata_store.list_execution_jobs(
                            statuses=("queued", "running"), limit=1,
                            instance_public_id=instance_public_id,
                            action=retention_action, conn=conn,
                        ):
                            raise ValueError(
                                f"retention task is already active: {instance_public_id}"
                            )
                    instances.append(instance)
                parent = metadata_store.create_execution_job(
                    request_id=request_id, actor_user_id=actor["id"],
                    action=parent_action, params=params, conn=conn,
                )
                metadata_store.update_execution_job(
                    request_id, "running", current_step="creating child jobs", conn=conn
                )
                children = []
                for index, instance in enumerate(instances, 1):
                    children.append(
                        metadata_store.create_execution_job(
                            request_id=f"{request_id}:{index}",
                            parent_request_id=request_id,
                            actor_user_id=actor["id"],
                            instance_public_id=instance["public_id"],
                            action=child_action, params={}, conn=conn,
                        )
                    )
                parent = metadata_store.update_execution_job(
                    request_id, "succeeded", output=f"queued {len(children)} child jobs",
                    conn=conn,
                )
                metadata_store.record_operation(
                    request_id=request_id, actor_user_id=actor["id"],
                    source_service="manager-control", action=parent_action,
                    status="success", message=f"queued {len(children)} child jobs",
                    conn=conn,
                )
                children = metadata_store.list_execution_jobs(
                    parent_request_id=request_id, limit=100, conn=conn
                )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except sqlite3.IntegrityError:
        return jsonify({"error": "could not create device batch"}), 409
    return jsonify(
        {
            "parent": execution_job_payload(parent, actor_public_id),
            "children": [device_batch_child_payload(job) for job in children],
        }
    )


@app.get("/internal/v1/admin/device-batches/<request_id>")
@require_services("manager-admin-web")
def get_device_batch(request_id):
    parent = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if parent is None or not parent["action"].startswith("batch.device_"):
        return jsonify({"error": "device batch not found"}), 404
    children = metadata_store.list_execution_jobs(
        parent_request_id=request_id, limit=100, db_file=DB_FILE
    )
    return jsonify(
        {
            "parent": execution_job_payload(parent),
            "children": [device_batch_child_payload(job) for job in children],
        }
    )


@app.get("/internal/v1/instances/<instance_public_id>")
@require_services("manager-user-web")
def get_instance(instance_public_id):
    actor_id = actor_public_id()
    if not actor_id:
        return jsonify({"error": "actor user public ID is required"}), 400
    instance = metadata_store.get_instance_for_user(
        instance_public_id,
        actor_id,
        db_file=DB_FILE,
    )
    if instance is None:
        return jsonify({"error": "instance not found"}), 404
    return jsonify({"instance": portal_instance(instance)})


@app.get("/internal/v1/executor/instances/<instance_public_id>")
@require_services("manager-executor")
def executor_instance(instance_public_id):
    actor_user_public_id = request.args.get("actor_user_public_id", "")
    if not actor_user_public_id:
        return jsonify({"error": "actor_user_public_id is required"}), 400
    admin = request.args.get("admin") == "true"
    actor = metadata_store.get_user_by_public_id(actor_user_public_id, db_file=DB_FILE)
    if admin:
        instance = (
            metadata_store.get_instance_by_public_id(instance_public_id, db_file=DB_FILE)
            if actor and actor["status"] == "active" and actor["role"] == "admin"
            else None
        )
        if instance is not None:
            instance["access_role"] = "admin"
    else:
        instance = metadata_store.get_instance_for_user(
            instance_public_id, actor_user_public_id, db_file=DB_FILE
        )
    if instance is None:
        return jsonify({"error": "instance not found"}), 404
    return jsonify({"instance": executor_instance_payload(instance)})


def manageable_instance(instance_public_id, conn):
    actor_id = actor_public_id()
    if not actor_id:
        return None, (jsonify({"error": "actor user public ID is required"}), 400)
    instance = metadata_store.get_instance_for_user(
        instance_public_id,
        actor_id,
        conn=conn,
    )
    if instance is None:
        return None, (jsonify({"error": "instance not found"}), 404)
    if instance["access_role"] not in {"owner", "manager"}:
        return None, (jsonify({"error": "member management is not allowed"}), 403)
    return instance, None


@app.get("/internal/v1/instances/<instance_public_id>/members")
@require_services("manager-user-web")
def instance_members(instance_public_id):
    with metadata_store.connect(DB_FILE) as conn:
        _, error = manageable_instance(instance_public_id, conn)
        if error:
            return error
        members = metadata_store.list_instance_members(
            instance_public_id,
            conn=conn,
        )
    return jsonify({"members": [member_payload(member) for member in members]})


@app.put(
    "/internal/v1/instances/<instance_public_id>/members/<member_public_id>"
)
@require_services("manager-user-web")
def set_instance_member(instance_public_id, member_public_id):
    payload = request.get_json(silent=True) or {}
    return set_member(instance_public_id, member_public_id, payload.get("role"))


@app.post("/internal/v1/instances/<instance_public_id>/members")
@require_services("manager-user-web")
def add_instance_member_by_username(instance_public_id):
    payload = request.get_json(silent=True) or {}
    username = payload.get("username")
    if not isinstance(username, str) or not username.strip() or len(username) > 128:
        return jsonify({"error": "valid username is required"}), 400
    user = metadata_store.get_user_by_username(
        username,
        db_file=DB_FILE,
    )
    if user is None or user["status"] != "active":
        return jsonify({"error": "active platform user not found"}), 404
    return set_member(instance_public_id, user["public_id"], payload.get("role"))


def set_member(instance_public_id, member_public_id, role):
    with metadata_store.connect(DB_FILE) as conn:
        instance, error = manageable_instance(instance_public_id, conn)
        if error:
            return error
        existing = next(
            (
                member
                for member in metadata_store.list_instance_members(
                    instance_public_id,
                    conn=conn,
                )
                if member["user_public_id"] == member_public_id
            ),
            None,
        )
        if instance["access_role"] == "manager" and (
            role == "manager" or (existing and existing["role"] == "manager")
        ):
            return jsonify({"error": "manager cannot manage manager members"}), 403
        try:
            metadata_store.add_instance_member(
                instance_public_id,
                member_public_id,
                role,
                created_by_user_id=instance["current_user_id"],
                conn=conn,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        metadata_store.record_operation(
            action="instance_member.set",
            status="success",
            actor_user_id=instance["current_user_id"],
            instance_id=instance["id"],
            source_service=g.source_service,
            message=f"member={member_public_id} role={role}",
            conn=conn,
        )
        member = next(
            item
            for item in metadata_store.list_instance_members(
                instance_public_id,
                conn=conn,
            )
            if item["user_public_id"] == member_public_id
        )
    return jsonify({"member": member_payload(member)})


@app.delete(
    "/internal/v1/instances/<instance_public_id>/members/<member_public_id>"
)
@require_services("manager-user-web")
def remove_instance_member(instance_public_id, member_public_id):
    with metadata_store.connect(DB_FILE) as conn:
        instance, error = manageable_instance(instance_public_id, conn)
        if error:
            return error
        member = next(
            (
                item
                for item in metadata_store.list_instance_members(
                    instance_public_id,
                    conn=conn,
                )
                if item["user_public_id"] == member_public_id
            ),
            None,
        )
        if member is None:
            return jsonify({"error": "instance member not found"}), 404
        if instance["access_role"] == "manager" and member["role"] == "manager":
            return jsonify({"error": "manager cannot manage manager members"}), 403
        metadata_store.remove_instance_member(
            instance_public_id,
            member_public_id,
            conn=conn,
        )
        metadata_store.record_operation(
            action="instance_member.remove",
            status="success",
            actor_user_id=instance["current_user_id"],
            instance_id=instance["id"],
            source_service=g.source_service,
            message=f"member={member_public_id} role={member['role']}",
            conn=conn,
        )
    return "", 204


@app.get("/internal/v1/operations")
@require_services("manager-admin-web")
def operation_events():
    try:
        limit = min(max(int(request.args.get("limit", "100")), 1), 1000)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify(
        {
            "operations": metadata_store.list_operation_events(
                limit,
                db_file=DB_FILE,
            )
        }
    )


@app.post("/internal/v1/execution-jobs")
@require_services("manager-admin-web", "manager-user-web")
def create_execution_job():
    payload = request.get_json(silent=True) or {}
    request_id = payload.get("request_id")
    parent_request_id = payload.get("parent_request_id")
    action = payload.get("action")
    actor_user_public_id = payload.get("actor_user_public_id")
    instance_public_id = payload.get("instance_public_id")
    params = payload.get("params", {})
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return jsonify({"error": "invalid request_id"}), 400
    if parent_request_id is not None and (
        not isinstance(parent_request_id, str)
        or not REQUEST_ID_RE.fullmatch(parent_request_id)
    ):
        return jsonify({"error": "invalid parent_request_id"}), 400
    if not isinstance(action, str) or not ACTION_RE.fullmatch(action):
        return jsonify({"error": "invalid action"}), 400
    if not isinstance(params, dict):
        return jsonify({"error": "params must be an object"}), 400
    allowed_params = JOB_ACTION_PARAMS.get(action)
    if allowed_params is None:
        return jsonify({"error": "unsupported execution action"}), 400
    if set(params) - allowed_params:
        return jsonify({"error": f"unsupported params for {action}"}), 400
    if "reason" in params and (
        not isinstance(params["reason"], str) or len(params["reason"]) > 500
    ):
        return jsonify({"error": "reason must be a string of at most 500 characters"}), 400
    if action == "instance.set_basic_auth" and not isinstance(
        params.get("enabled"), bool
    ):
        return jsonify({"error": "enabled must be a boolean"}), 400
    if action == "instance.update_version" and (
        not isinstance(params.get("version"), str)
        or not VERSION_RE.fullmatch(params["version"])
    ):
        return jsonify({"error": "invalid version"}), 400
    if action == "instance.update_version" and not isinstance(
        params.get("restore_model_provider"), bool
    ):
        return jsonify({"error": "restore_model_provider must be a boolean"}), 400
    if action == "instance.install_skill":
        skill_id = params.get("skill_id")
        presets = {
            value.strip()
            for value in re.split(r"[,\n]", os.environ.get("MANAGER_SKILL_PRESETS", ""))
            if value.strip() and SKILL_ID_RE.fullmatch(value.strip())
        }
        if not isinstance(skill_id, str) or skill_id not in presets:
            return jsonify({"error": "invalid or unconfigured skill preset"}), 400
    if not isinstance(instance_public_id, str) or not instance_public_id:
        return jsonify({"error": "instance_public_id is required"}), 400
    actor = metadata_store.get_user_by_public_id(
        actor_user_public_id,
        db_file=DB_FILE,
    )
    if actor is None or actor["status"] != "active":
        return jsonify({"error": "active actor is required"}), 403
    if (
        g.source_service == "manager-user-web"
        and actor_public_id() != actor_user_public_id
    ):
        return jsonify({"error": "user service cannot impersonate another user"}), 403
    if g.source_service == "manager-user-web" and action != "instance.wechat_bind":
        return jsonify({"error": "user web action is not allowed"}), 403
    if g.source_service == "manager-admin-web" and actor["role"] != "admin":
        return jsonify({"error": "active admin actor is required"}), 403
    if action == "instance.wechat_bind":
        if g.source_service != "manager-user-web":
            return jsonify({"error": "wechat bind is only available to user web"}), 403
        instance = metadata_store.get_instance_for_user(
            instance_public_id, actor_user_public_id, db_file=DB_FILE
        )
        if instance is None or instance.get("access_role") not in {"owner", "manager"}:
            return jsonify({"error": "device pairing is not allowed"}), 403
    else:
        instance = metadata_store.get_instance_by_public_id(
            instance_public_id,
            db_file=DB_FILE,
        )
        if instance is None:
            return jsonify({"error": "instance not found"}), 404
    capability = execution_action_capability(action)
    if capability is None or not product_supports(instance.get("product"), capability):
        return jsonify(
            {"error": f"instance product does not support {capability or action}"}
        ), 400
    existing_job = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if existing_job is not None:
        try:
            job = metadata_store.create_execution_job(
                request_id=request_id,
                parent_request_id=parent_request_id,
                actor_user_id=actor["id"],
                instance_public_id=instance_public_id,
                action=action,
                params=params,
                db_file=DB_FILE,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(
            {"job": execution_job_payload(job, actor_user_public_id, instance_public_id)}
        )
    if action == "instance.delete" and instance["status"] == "deleted":
        return jsonify({"error": "instance is already deleted"}), 409
    if instance["status"] == "deleted" and action != "instance.restore":
        return jsonify({"error": "deleted instance only supports restore"}), 409
    if action == "instance.restore" and (
        instance["status"] != "deleted"
        or instance.get("restore_state") != "restorable"
    ):
        return jsonify({"error": "instance is not restorable"}), 409
    try:
        with metadata_store.connect(DB_FILE) as conn:
            conn.execute("BEGIN IMMEDIATE")
            exclusive_actions = {"instance.delete", "instance.restore"}
            active_jobs = metadata_store.list_execution_jobs(
                limit=1,
                statuses=("queued", "running"),
                instance_public_id=instance_public_id,
                action=None if action in exclusive_actions else action,
                conn=conn,
            )
            active_retention_jobs = []
            if action not in exclusive_actions:
                for retention_action in exclusive_actions:
                    active_retention_jobs.extend(
                        metadata_store.list_execution_jobs(
                            limit=1,
                            statuses=("queued", "running"),
                            instance_public_id=instance_public_id,
                            action=retention_action,
                            conn=conn,
                        )
                    )
            if active_retention_jobs or (
                active_jobs
                and action in {
                    "instance.wechat_bind",
                    "instance.approve_latest_device",
                    *exclusive_actions,
                }
            ):
                message = (
                    "wechat bind is already running"
                    if action == "instance.wechat_bind" and not active_retention_jobs
                    else (
                        "approve_latest_device is already running"
                        if action == "instance.approve_latest_device" and not active_retention_jobs
                        else f"{action.removeprefix('instance.')} cannot run while another instance task is active"
                    )
                )
                return jsonify({"error": message}), 409
            job = metadata_store.create_execution_job(
                request_id=request_id,
                parent_request_id=parent_request_id,
                actor_user_id=actor["id"],
                instance_public_id=instance_public_id,
                action=action,
                params=params,
                conn=conn,
            )
    except ValueError as exc:
        status = 409 if "request_id already used" in str(exc) else 400
        return jsonify({"error": str(exc)}), status
    except sqlite3.IntegrityError:
        return jsonify({"error": "parent execution job not found"}), 400
    return jsonify(
        {
            "job": execution_job_payload(
                job,
                actor_user_public_id,
                instance_public_id,
            )
        }
    )


@app.get("/internal/v1/execution-jobs")
@require_services("manager-admin-web", "manager-executor")
def list_execution_jobs():
    status = request.args.get("status")
    if status is not None and status not in JOB_STATUSES:
        return jsonify({"error": "invalid execution job status"}), 400
    if g.source_service == "manager-executor":
        if status not in {None, "queued"}:
            return jsonify({"error": "executor may list only queued jobs"}), 403
        status = "queued"
    try:
        limit = min(max(int(request.args.get("limit", "100")), 1), 1000)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    jobs = metadata_store.list_execution_jobs(
        status,
        limit,
        db_file=DB_FILE,
    )
    return jsonify({"jobs": [execution_job_payload(job) for job in jobs]})


@app.post("/internal/v1/execution-jobs/claim")
@require_services("manager-executor")
def claim_execution_job():
    job, instance = metadata_store.claim_next_execution_job(
        stale_seconds=EXECUTOR_STALE_SECONDS,
        db_file=DB_FILE,
    )
    if job is None:
        return "", 204
    return jsonify(
        {
            "job": execution_job_payload(job),
            "instance": executor_instance_payload(instance),
        }
    )


@app.get("/internal/v1/execution-jobs/<request_id>")
@require_services("manager-admin-web", "manager-user-web", "manager-executor")
def get_execution_job(request_id):
    job = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if job is None:
        return jsonify({"error": "execution job not found"}), 404
    if g.source_service == "manager-user-web" and (
        job.get("actor_user_public_id") != actor_public_id()
        or job.get("action") != "instance.wechat_bind"
    ):
        return jsonify({"error": "execution job is not available"}), 403
    return jsonify({"job": execution_job_payload(job)})


@app.get("/internal/v1/instances/<instance_public_id>/wechat-bind-job")
@require_services("manager-user-web")
def current_wechat_bind_job(instance_public_id):
    actor_id = actor_public_id()
    if not actor_id:
        return jsonify({"error": "actor user public ID is required"}), 400
    jobs = metadata_store.list_execution_jobs(
        limit=1,
        actor_user_public_id=actor_id,
        instance_public_id=instance_public_id,
        action="instance.wechat_bind",
        newest_first=True,
        db_file=DB_FILE,
    )
    return jsonify({"job": execution_job_payload(jobs[0]) if jobs else None})


@app.post("/internal/v1/execution-jobs/<request_id>/cancel")
@require_services("manager-user-web")
def cancel_execution_job(request_id):
    job = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if job is None:
        return jsonify({"error": "execution job not found"}), 404
    if (
        job.get("actor_user_public_id") != actor_public_id()
        or job.get("action") != "instance.wechat_bind"
    ):
        return jsonify({"error": "execution job is not available"}), 403
    try:
        job = metadata_store.update_execution_job(
            request_id, "cancelled", db_file=DB_FILE
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"job": execution_job_payload(job)})


@app.patch("/internal/v1/execution-jobs/<request_id>")
@require_services("manager-executor")
def update_execution_job(request_id):
    payload = request.get_json(silent=True) or {}
    allowed = {"status", "current_step", "error_summary", "output"}
    if set(payload) - allowed:
        return jsonify({"error": "unsupported execution job fields"}), 400
    status = payload.get("status")
    if not isinstance(status, str):
        return jsonify({"error": "status is required"}), 400
    for field in ("current_step", "error_summary", "output"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return jsonify({"error": f"{field} must be a string"}), 400
    try:
        with metadata_store.connect(DB_FILE) as conn:
            job = metadata_store.get_execution_job(request_id, conn=conn)
            if job is None:
                raise ValueError("execution job not found")
            metadata_store.update_execution_job(
                request_id,
                status,
                current_step=payload.get("current_step"),
                error_summary=payload.get("error_summary"),
                output=payload.get("output"),
                conn=conn,
            )
            if status == "succeeded" and job["action"] == "instance.set_basic_auth":
                enabled = json.loads(job["params_json"])["enabled"]
                metadata_store.set_instance_basic_auth(
                    job["instance_public_id"],
                    enabled,
                    conn=conn,
                )
            if status == "succeeded" and job["action"] == "instance.update_version":
                metadata_store.set_instance_version(
                    job["instance_public_id"],
                    json.loads(job["params_json"])["version"],
                    conn=conn,
                )
            if (
                status in {"succeeded", "failed"}
                and job["action"]
                in {
                    "instance.set_basic_auth",
                    "instance.update_version",
                    "instance.install_skill",
                    "instance.refresh_devices",
                    "instance.approve_latest_device",
                    "instance.delete",
                    "instance.restore",
                }
            ):
                params = json.loads(job["params_json"])
                if job["action"] == "instance.set_basic_auth":
                    message = f"Basic Auth {'enabled' if params['enabled'] else 'disabled'}"
                elif job["action"] == "instance.update_version":
                    message = f"version={params['version']}"
                elif job["action"] == "instance.install_skill":
                    message = f"skill={params['skill_id']}"
                elif job["action"] in {"instance.delete", "instance.restore"}:
                    message = f"instance {job['action'].removeprefix('instance.')}d"
                else:
                    message = "device cache refreshed" if job["action"] == "instance.refresh_devices" else "latest device request approved"
                operation_status = "success" if status == "succeeded" else "failed"
                if (
                    job["action"] == "instance.approve_latest_device"
                    and "No pending device request found" in (payload.get("output") or "")
                ):
                    operation_status = "skipped"
                    message = "No pending device request found."
                metadata_store.record_operation(
                    request_id=request_id,
                    actor_user_id=job["actor_user_id"],
                    instance_id=job["instance_id"],
                    source_service="manager-executor",
                    action=job["action"],
                    status=operation_status,
                    message=message,
                    conn=conn,
                )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 409
        return jsonify({"error": str(exc)}), status_code
    job = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    return jsonify({"job": execution_job_payload(job)})


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_PORT", "8082")),
    )
