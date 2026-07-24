import hmac
import json
import os
import re
import sqlite3
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request

import metadata_store


DB_FILE = Path(
    os.environ.get(
        "METADATA_DB_FILE",
        "/data/docker/openclaw-public/manager.db",
    )
)
TOKEN_ENV = {
    "manager-user-web": "MANAGER_CONTROL_USER_WEB_TOKEN",
    "manager-admin-web": "MANAGER_CONTROL_ADMIN_WEB_TOKEN",
    "manager-executor": "MANAGER_CONTROL_EXECUTOR_TOKEN",
}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
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
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


def portal_instance(instance):
    return {
        "public_id": instance["public_id"],
        "product": instance["product"],
        "instance_name": instance["instance_name"],
        "status": instance["status"],
        "version": instance.get("openclaw_version"),
        "access_url": instance.get("access_url"),
        "access_role": instance["access_role"],
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
    role = payload.get("role")
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
@require_services("manager-admin-web")
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
    if not isinstance(instance_public_id, str) or not instance_public_id:
        return jsonify({"error": "instance_public_id is required"}), 400
    actor = metadata_store.get_user_by_public_id(
        actor_user_public_id,
        db_file=DB_FILE,
    )
    if actor is None or actor["status"] != "active" or actor["role"] != "admin":
        return jsonify({"error": "active admin actor is required"}), 403
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


@app.get("/internal/v1/execution-jobs/<request_id>")
@require_services("manager-admin-web")
def get_execution_job(request_id):
    job = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if job is None:
        return jsonify({"error": "execution job not found"}), 404
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
        metadata_store.update_execution_job(
            request_id,
            status,
            current_step=payload.get("current_step"),
            error_summary=payload.get("error_summary"),
            output=payload.get("output"),
            db_file=DB_FILE,
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
