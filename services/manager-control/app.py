import hmac
import json
import os
import re
import secrets
import sqlite3
import urllib.parse
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
MIXED_AUTH_ENABLED = (
    os.environ.get("MANAGER_LOCAL_AUTH_ENABLED", "false").lower()
    in {"1", "true", "yes", "on"}
    and os.environ.get("MANAGER_AUTH_PROVIDER", "nginx-basic")
    not in {"nginx-basic", "local"}
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
VERSION_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,63}|sha256:[0-9a-fA-F]{64})$")
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_.@/-]{1,128}$")
MODEL_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
LEGACY_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEFAULT_VERSION_KEYS = {
    "openclaw": "default_version.openclaw",
    "hermes": "default_version.hermes",
    "evoscientist": "default_version.evoscientist",
}
DEFAULT_EVOSCIENTIST_VERSION = "sha256:ca1fd303d7ca2d1bfad97d9872b4ee910eea67c46047be1bf59463941fff3c47"
PROVISIONING_SECRET_DIR = Path(
    os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public")
) / ".manager-secrets"
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
    "instance.set_model_provider": {
        "model_provider_id", "model_id", "model_base_url", "model_alias",
    },
    "instance.refresh_devices": set(),
    "instance.approve_latest_device": set(),
    "instance.delete": set(),
    "instance.restore": set(),
    "instance.purge_deleted": set(),
    "instance.cleanup_failed": set(),
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


def default_version(product, conn=None):
    env_defaults = {
        "openclaw": os.environ.get("OPENCLAW_VERSION", "").strip(),
        "hermes": os.environ.get("HERMES_VERSION", "v2026.7.20").strip(),
        "evoscientist": os.environ.get("EVOSCIENTIST_IMAGE", "").strip() or DEFAULT_EVOSCIENTIST_VERSION,
    }
    value = metadata_store.get_setting(DEFAULT_VERSION_KEYS[product], None, conn=conn)
    if product == "evoscientist" and value:
        value = value.rsplit("@", 1)[-1]
        if not value.startswith("sha256:") and ":" in value:
            value = value.rsplit(":", 1)[-1]
    if not value:
        value = env_defaults[product]
        if product == "evoscientist" and value:
            value = value.rsplit("@", 1)[-1]
            if not value.startswith("sha256:") and ":" in value:
                value = value.rsplit(":", 1)[-1]
    return value


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


def action_batch_child_payload(job):
    value = execution_job_payload(job)
    value["summary"] = (
        job["status"]
        if job["status"] in {"queued", "running", "succeeded"}
        else job.get("error_summary") or job["status"]
    )
    value["output"] = None
    return value


def validate_model_provider_params(params):
    provider_id = params.get("model_provider_id")
    model_id = params.get("model_id")
    base_url = params.get("model_base_url")
    alias = params.get("model_alias")
    if not isinstance(provider_id, str) or not MODEL_PROVIDER_ID_RE.fullmatch(provider_id):
        return "invalid model_provider_id"
    if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id):
        return "invalid model_id"
    if not isinstance(base_url, str) or len(base_url) > 2048:
        return "invalid model_base_url"
    if base_url:
        parsed_url = urllib.parse.urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return "invalid model_base_url"
    if not isinstance(alias, str) or not alias or len(alias) > 128 or any(
        ord(character) < 32 for character in alias
    ):
        return "invalid model_alias"
    return None


def create_batch_child_payload(job):
    value = execution_job_payload(job)
    value["params"] = {}
    value["summary"] = (
        job["status"]
        if job["status"] in {"queued", "running", "succeeded"}
        else job.get("error_summary") or job["status"]
    )
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
        "port": instance.get("port"),
        "basic_auth_enabled": bool(instance.get("basic_auth_enabled")),
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
    ready = version == 6 and tokens_valid
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
    if not MIXED_AUTH_ENABLED:
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


@app.delete("/internal/v1/auth/external-session")
@require_services("manager-user-web")
def delete_external_auth_session():
    payload = request.get_json(silent=True) or {}
    external_token_hash = payload.get("external_token_hash")
    if not isinstance(external_token_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", external_token_hash
    ):
        return jsonify({"error": "external_token_hash is required"}), 400
    metadata_store.delete_session_by_external_token(
        external_token_hash, db_file=DB_FILE
    )
    return "", 204


@app.get("/internal/v1/auth/identity")
@require_services("manager-user-web", "manager-admin-web")
def resolve_auth_identity():
    provider = request.args.get("provider", "")
    subject = request.args.get("subject", "")
    if not provider or not subject:
        return jsonify({"error": "provider and subject are required"}), 400
    if not MIXED_AUTH_ENABLED:
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
    external_token_hash = payload.get("external_token_hash")
    if external_token_hash is not None and (
        not isinstance(external_token_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", external_token_hash)
    ):
        return jsonify({"error": "invalid external login payload"}), 400
    if not MIXED_AUTH_ENABLED:
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
        external_token_hash=external_token_hash,
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
    if not MIXED_AUTH_ENABLED:
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
                            "delete", "restore", "purge_deleted", "batch_set_model_provider",
                        )
                        if product_supports(instance["product"], capability)
                    ),
                }
                for instance in instances
            ]
        }
    )


@app.get("/internal/v1/admin/users")
@require_services("manager-admin-web")
def admin_users():
    with metadata_store.connect(DB_FILE) as conn:
        users = conn.execute(
            """
            SELECT public_id, username, display_name, role, status
            FROM users
            ORDER BY normalized_username
            """
        ).fetchall()
    return jsonify({"users": [dict(user) for user in users]})


@app.post("/internal/v1/admin/instances")
@require_services("manager-admin-web")
def create_admin_instance():
    payload = request.get_json(silent=True) or {}
    allowed = {
        "request_id", "actor_user_public_id", "owner_user_public_id",
        "legacy_user_id", "instance_name", "product",
        "basic_auth_enabled", "basic_auth_password", "version", "confirm_latest",
    }
    if set(payload) - allowed:
        return jsonify({"error": "unsupported instance fields"}), 400
    request_id = payload.get("request_id")
    actor_public_id = payload.get("actor_user_public_id")
    owner_public_id = payload.get("owner_user_public_id")
    legacy_user_id = payload.get("legacy_user_id")
    instance_name = payload.get("instance_name")
    product = payload.get("product")
    basic_auth_enabled = payload.get("basic_auth_enabled")
    password = payload.get("basic_auth_password")
    version = payload.get("version")
    confirm_latest = payload.get("confirm_latest", False)
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return jsonify({"error": "invalid request_id"}), 400
    if not isinstance(legacy_user_id, str) or not LEGACY_USER_ID_RE.fullmatch(legacy_user_id):
        return jsonify({"error": "invalid legacy_user_id"}), 400
    if not isinstance(instance_name, str) or not instance_name.strip() or len(instance_name) > 128:
        return jsonify({"error": "invalid instance_name"}), 400
    if product not in {"openclaw", "hermes", "evoscientist"} or not product_supports(product, "create"):
        return jsonify({"error": "instance product does not support create"}), 400
    if not isinstance(basic_auth_enabled, bool):
        return jsonify({"error": "basic_auth_enabled must be a boolean"}), 400
    if product in {"hermes", "evoscientist"} and not basic_auth_enabled:
        return jsonify({"error": f"{product} requires Basic Auth"}), 400
    if not isinstance(password, str) or not password:
        return jsonify({"error": "basic_auth_password is required"}), 400
    if version is not None and (not isinstance(version, str) or not VERSION_RE.fullmatch(version)):
        return jsonify({"error": "version must be valid"}), 400
    if not isinstance(confirm_latest, bool):
        return jsonify({"error": "confirm_latest must be a boolean"}), 400
    if product == "evoscientist" and version == "latest" and not confirm_latest:
        return jsonify({"error": "latest requires explicit confirmation"}), 400
    if version is None:
        with metadata_store.connect(DB_FILE) as conn:
            version = default_version(product, conn=conn)
        if version and product == "evoscientist" and version == "latest" and not confirm_latest:
            return jsonify({"error": "latest requires explicit confirmation"}), 400

    existing_job = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if existing_job is not None:
        if (
            existing_job["action"] == "instance.create"
            and existing_job.get("actor_user_public_id") == actor_public_id
            and existing_job.get("instance_public_id")
        ):
            instance = metadata_store.get_instance_by_public_id(
                existing_job["instance_public_id"], db_file=DB_FILE
            )
            return jsonify(
                {"instance": admin_metadata_instance(instance), "job": execution_job_payload(existing_job)}
            ), 202
        return jsonify({"error": "request_id already used for another operation"}), 409

    secret_path = PROVISIONING_SECRET_DIR / secrets.token_urlsafe(32)
    try:
        actor = metadata_store.get_user_by_public_id(actor_public_id, db_file=DB_FILE)
        owner = metadata_store.get_user_by_public_id(owner_public_id, db_file=DB_FILE)
        if actor is None or actor["status"] != "active" or actor["role"] != "admin":
            raise ValueError("active administrator not found")
        if owner is None or owner["status"] != "active":
            raise ValueError("active owner user not found")
        PROVISIONING_SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        PROVISIONING_SECRET_DIR.chmod(0o700)
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(password)
        with metadata_store.connect(DB_FILE) as conn:
            instance = metadata_store.create_instance(
                owner_public_id=owner_public_id,
                product=product,
                instance_name=instance_name.strip(),
                legacy_user_id=legacy_user_id,
                runtime_identifier=f"{product}_{legacy_user_id}",
                data_path=str(
                    PROVISIONING_SECRET_DIR.parent
                    / ("hermes" if product == "hermes" else "users")
                    / legacy_user_id
                ),
                status="provisioning",
                basic_auth_enabled=basic_auth_enabled,
                conn=conn,
            )
            job = metadata_store.create_execution_job(
                request_id=request_id,
                actor_user_id=actor["id"],
                instance_public_id=instance["public_id"],
                action="instance.create",
                params={"secret_path": str(secret_path), **({"version": version} if version else {})},
                conn=conn,
            )
    except ValueError as exc:
        secret_path.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 409
    except Exception:
        secret_path.unlink(missing_ok=True)
        raise
    return jsonify({"instance": admin_metadata_instance(instance), "job": execution_job_payload(job)}), 202


@app.post("/internal/v1/admin/instance-batches")
@require_services("manager-admin-web")
def create_instance_batch():
    payload = request.get_json(silent=True) or {}
    if set(payload) - {"request_id", "actor_user_public_id", "instances"}:
        return jsonify({"error": "unsupported batch fields"}), 400
    request_id = payload.get("request_id")
    actor_public_id = payload.get("actor_user_public_id")
    rows = payload.get("instances")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return jsonify({"error": "invalid request_id"}), 400
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        return jsonify({"error": "instances must contain 1-100 rows"}), 400

    allowed = {
        "owner_user_public_id", "legacy_user_id", "instance_name", "product",
        "basic_auth_enabled", "basic_auth_password",
    }
    legacy_ids = []
    summaries = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - allowed:
            return jsonify({"error": f"invalid fields in row {index}"}), 400
        legacy_user_id = row.get("legacy_user_id")
        instance_name = row.get("instance_name")
        if not isinstance(legacy_user_id, str) or not LEGACY_USER_ID_RE.fullmatch(legacy_user_id):
            return jsonify({"error": f"invalid legacy_user_id in row {index}"}), 400
        if not isinstance(instance_name, str) or not instance_name.strip() or len(instance_name) > 128:
            return jsonify({"error": f"invalid instance_name in row {index}"}), 400
        if row.get("product") != "openclaw" or not product_supports("openclaw", "create"):
            return jsonify({"error": f"unsupported product in row {index}"}), 400
        if not isinstance(row.get("basic_auth_enabled"), bool):
            return jsonify({"error": f"invalid basic_auth_enabled in row {index}"}), 400
        if not isinstance(row.get("basic_auth_password"), str) or not row["basic_auth_password"]:
            return jsonify({"error": f"basic_auth_password is required in row {index}"}), 400
        if not isinstance(row.get("owner_user_public_id"), str) or not row["owner_user_public_id"]:
            return jsonify({"error": f"owner_user_public_id is required in row {index}"}), 400
        legacy_ids.append(legacy_user_id)
        summaries.append({
            "owner_user_public_id": row["owner_user_public_id"],
            "legacy_user_id": legacy_user_id,
            "instance_name": instance_name.strip(),
            "product": "openclaw",
            "basic_auth_enabled": row["basic_auth_enabled"],
        })
    if len(set(legacy_ids)) != len(legacy_ids):
        return jsonify({"error": "legacy_user_id values must be unique"}), 400

    actor = metadata_store.get_user_by_public_id(actor_public_id, db_file=DB_FILE)
    if actor is None or actor["status"] != "active" or actor["role"] != "admin":
        return jsonify({"error": "active admin actor is required"}), 403
    existing = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if existing is not None:
        try:
            parent = metadata_store.create_execution_job(
                request_id=request_id, actor_user_id=actor["id"],
                action="batch.create", params={"instances": summaries}, db_file=DB_FILE,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        children = metadata_store.list_execution_jobs(
            parent_request_id=request_id, limit=100, db_file=DB_FILE
        )
        return jsonify({
            "parent": execution_job_payload(parent),
            "children": [create_batch_child_payload(job) for job in children],
        }), 202

    secret_paths = []
    try:
        PROVISIONING_SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        PROVISIONING_SECRET_DIR.chmod(0o700)
        with metadata_store.connect(DB_FILE) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for owner_public_id in {row["owner_user_public_id"] for row in rows}:
                owner = metadata_store.get_user_by_public_id(owner_public_id, conn=conn)
                if owner is None or owner["status"] != "active":
                    raise ValueError(f"active owner user not found: {owner_public_id}")
            metadata_store.create_execution_job(
                request_id=request_id, actor_user_id=actor["id"],
                action="batch.create", params={"instances": summaries}, conn=conn,
            )
            metadata_store.update_execution_job(
                request_id, "running", current_step="creating instances", conn=conn
            )
            for index, row in enumerate(rows, 1):
                secret_path = PROVISIONING_SECRET_DIR / secrets.token_urlsafe(32)
                descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
                    secret_file.write(row["basic_auth_password"])
                secret_paths.append(secret_path)
                instance = metadata_store.create_instance(
                    owner_public_id=row["owner_user_public_id"], product="openclaw",
                    instance_name=row["instance_name"].strip(),
                    legacy_user_id=row["legacy_user_id"],
                    runtime_identifier=f"openclaw_{row['legacy_user_id']}",
                    data_path=str(PROVISIONING_SECRET_DIR.parent / "users" / row["legacy_user_id"]),
                    status="provisioning", basic_auth_enabled=row["basic_auth_enabled"],
                    conn=conn,
                )
                metadata_store.create_execution_job(
                    request_id=f"{request_id}:{index}", parent_request_id=request_id,
                    actor_user_id=actor["id"], instance_public_id=instance["public_id"],
                    action="instance.create", params={"secret_path": str(secret_path)},
                    conn=conn,
                )
            parent = metadata_store.update_execution_job(
                request_id, "succeeded", output=f"queued {len(rows)} child jobs", conn=conn
            )
            metadata_store.record_operation(
                request_id=request_id, actor_user_id=actor["id"],
                source_service="manager-control", action="batch.create",
                status="success", message=f"queued {len(rows)} child jobs", conn=conn,
            )
            children = metadata_store.list_execution_jobs(
                parent_request_id=request_id, limit=100, conn=conn
            )
    except (ValueError, sqlite3.IntegrityError) as exc:
        for secret_path in secret_paths:
            secret_path.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 409
    except Exception:
        for secret_path in secret_paths:
            secret_path.unlink(missing_ok=True)
        raise
    return jsonify({
        "parent": execution_job_payload(parent),
        "children": [create_batch_child_payload(job) for job in children],
    }), 202


@app.get("/internal/v1/admin/instance-batches/<request_id>")
@require_services("manager-admin-web")
def get_instance_batch(request_id):
    parent = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if parent is None or parent["action"] != "batch.create":
        return jsonify({"error": "instance batch not found"}), 404
    children = metadata_store.list_execution_jobs(
        parent_request_id=request_id, limit=100, db_file=DB_FILE
    )
    return jsonify({
        "parent": execution_job_payload(parent),
        "children": [create_batch_child_payload(job) for job in children],
    })


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


@app.get("/internal/v1/admin/default-versions")
@require_services("manager-admin-web")
def get_default_versions():
    with metadata_store.connect(DB_FILE) as conn:
        return jsonify({"versions": {product: default_version(product, conn=conn) for product in DEFAULT_VERSION_KEYS}})


@app.put("/internal/v1/admin/default-versions")
@require_services("manager-admin-web")
def update_default_versions():
    payload = request.get_json(silent=True) or {}
    if set(payload) - {*DEFAULT_VERSION_KEYS, "confirm_latest"}:
        return jsonify({"error": "unsupported default version fields"}), 400
    if not isinstance(payload.get("confirm_latest", False), bool):
        return jsonify({"error": "confirm_latest must be a boolean"}), 400
    for product, value in payload.items():
        if product not in DEFAULT_VERSION_KEYS:
            continue
        if not isinstance(value, str) or not VERSION_RE.fullmatch(value.strip()):
            return jsonify({"error": f"invalid {product} default version"}), 400
        if product == "evoscientist" and value.strip() == "latest" and not payload.get("confirm_latest"):
            return jsonify({"error": "latest requires explicit confirmation"}), 400
    with metadata_store.connect(DB_FILE) as conn:
        for product, value in payload.items():
            if product not in DEFAULT_VERSION_KEYS:
                continue
            metadata_store.set_setting(DEFAULT_VERSION_KEYS[product], value.strip(), conn=conn)
        metadata_store.record_operation(
            action="settings.default_versions.update", status="success",
            source_service="manager-admin-web", message="default agent versions updated", conn=conn,
        )
        versions = {
            product: default_version(product, conn=conn)
            for product in DEFAULT_VERSION_KEYS
        }
    return jsonify({"versions": versions})


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


@app.post("/internal/v1/admin/action-batches")
@require_services("manager-admin-web")
def create_action_batch():
    payload = request.get_json(silent=True) or {}
    allowed = {
        "request_id", "actor_user_public_id", "action",
        "instance_public_ids", "skill_id",
    }
    if set(payload) - allowed:
        return jsonify({"error": "unsupported batch fields"}), 400
    request_id = payload.get("request_id")
    actor_public_id = payload.get("actor_user_public_id")
    batch_action = payload.get("action")
    instance_public_ids = payload.get("instance_public_ids")
    actions = {
        "start": ("instance.start", {}),
        "stop": ("instance.stop", {}),
        "restart": ("instance.restart", {}),
        "install_skill": (
            "instance.install_skill", {"skill_id": payload.get("skill_id")}
        ),
    }
    if (
        not isinstance(request_id, str)
        or not REQUEST_ID_RE.fullmatch(request_id)
        or len(request_id) > 120
    ):
        return jsonify({"error": "invalid request_id"}), 400
    if batch_action not in actions:
        return jsonify({"error": "invalid batch action"}), 400
    if (
        not isinstance(instance_public_ids, list)
        or not 1 <= len(instance_public_ids) <= 100
        or any(not isinstance(value, str) or not value for value in instance_public_ids)
        or len(set(instance_public_ids)) != len(instance_public_ids)
    ):
        return jsonify({"error": "instance_public_ids must contain 1-100 unique IDs"}), 400
    child_action, child_params = actions[batch_action]
    if batch_action == "install_skill":
        presets = {
            value.strip()
            for value in re.split(r"[,\n]", os.environ.get("MANAGER_SKILL_PRESETS", ""))
            if value.strip() and SKILL_ID_RE.fullmatch(value.strip())
        }
        if child_params["skill_id"] not in presets:
            return jsonify({"error": "invalid or unconfigured skill preset"}), 400
    elif "skill_id" in payload:
        return jsonify({"error": "skill_id is only valid for install_skill"}), 400
    actor = metadata_store.get_user_by_public_id(actor_public_id, db_file=DB_FILE)
    if actor is None or actor["status"] != "active" or actor["role"] != "admin":
        return jsonify({"error": "active admin actor is required"}), 403

    parent_action = f"batch.{batch_action}"
    params = {"instance_public_ids": instance_public_ids, **child_params}
    try:
        with metadata_store.connect(DB_FILE) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = metadata_store.get_execution_job(request_id, conn=conn)
            if existing is None:
                instances = []
                capability = execution_action_capability(child_action)
                for instance_public_id in instance_public_ids:
                    instance = metadata_store.get_instance_by_public_id(
                        instance_public_id, conn=conn
                    )
                    if instance is None:
                        raise ValueError(f"instance not found: {instance_public_id}")
                    if instance["status"] == "deleted" or not product_supports(
                        instance["product"], capability
                    ):
                        raise ValueError(
                            f"action is not available: {instance_public_id}"
                        )
                    active_jobs = metadata_store.list_execution_jobs(
                        statuses=("queued", "running"), limit=1,
                        instance_public_id=instance_public_id,
                        action=child_action, conn=conn,
                    )
                    retention_jobs = any(
                        metadata_store.list_execution_jobs(
                            statuses=("queued", "running"), limit=1,
                            instance_public_id=instance_public_id,
                            action=action, conn=conn,
                        )
                        for action in ("instance.delete", "instance.restore")
                    )
                    if active_jobs or retention_jobs:
                        raise ValueError(f"instance task is already active: {instance_public_id}")
                    instances.append(instance)
                metadata_store.create_execution_job(
                    request_id=request_id, actor_user_id=actor["id"],
                    action=parent_action, params=params, conn=conn,
                )
                metadata_store.update_execution_job(
                    request_id, "running", current_step="creating child jobs", conn=conn
                )
                for index, instance in enumerate(instances, 1):
                    metadata_store.create_execution_job(
                        request_id=f"{request_id}:{index}", parent_request_id=request_id,
                        actor_user_id=actor["id"], instance_public_id=instance["public_id"],
                        action=child_action, params=child_params, conn=conn,
                    )
                metadata_store.update_execution_job(
                    request_id, "succeeded", output=f"queued {len(instances)} child jobs",
                    conn=conn,
                )
                metadata_store.record_operation(
                    request_id=request_id, actor_user_id=actor["id"],
                    source_service="manager-control", action=parent_action,
                    status="success", message=f"queued {len(instances)} child jobs",
                    conn=conn,
                )
            parent = metadata_store.create_execution_job(
                request_id=request_id, actor_user_id=actor["id"],
                action=parent_action, params=params, conn=conn,
            )
            parent = metadata_store.get_execution_job(request_id, conn=conn)
            children = metadata_store.list_execution_jobs(
                parent_request_id=request_id, limit=100, conn=conn
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except sqlite3.IntegrityError:
        return jsonify({"error": "could not create action batch"}), 409
    return jsonify({
        "parent": execution_job_payload(parent),
        "children": [action_batch_child_payload(job) for job in children],
    })


@app.get("/internal/v1/admin/action-batches/<request_id>")
@require_services("manager-admin-web")
def get_action_batch(request_id):
    parent = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if parent is None or parent["action"] not in {
        "batch.start", "batch.stop", "batch.restart", "batch.install_skill",
    }:
        return jsonify({"error": "action batch not found"}), 404
    children = metadata_store.list_execution_jobs(
        parent_request_id=request_id, limit=100, db_file=DB_FILE
    )
    return jsonify({
        "parent": execution_job_payload(parent),
        "children": [action_batch_child_payload(job) for job in children],
    })


@app.post("/internal/v1/admin/model-provider-batches")
@require_services("manager-admin-web")
def create_model_provider_batch():
    payload = request.get_json(silent=True) or {}
    if set(payload) - {"request_id", "actor_user_public_id", "instances"}:
        return jsonify({"error": "unsupported batch fields"}), 400
    request_id = payload.get("request_id")
    actor_public_id = payload.get("actor_user_public_id")
    rows = payload.get("instances")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return jsonify({"error": "invalid request_id"}), 400
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        return jsonify({"error": "instances must contain 1-100 rows"}), 400
    instance_ids = []
    normalized_rows = []
    required = {
        "instance_public_id", "model_provider_id", "model_id",
        "model_base_url", "model_alias",
    }
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != required:
            return jsonify({"error": f"invalid fields in row {index}"}), 400
        instance_public_id = row.get("instance_public_id")
        if not isinstance(instance_public_id, str) or not instance_public_id:
            return jsonify({"error": f"invalid instance_public_id in row {index}"}), 400
        params = {key: row[key] for key in required - {"instance_public_id"}}
        error = validate_model_provider_params(params)
        if error:
            return jsonify({"error": f"{error} in row {index}"}), 400
        instance_ids.append(instance_public_id)
        normalized_rows.append({"instance_public_id": instance_public_id, **params})
    if len(set(instance_ids)) != len(instance_ids):
        return jsonify({"error": "instance_public_id values must be unique"}), 400
    actor = metadata_store.get_user_by_public_id(actor_public_id, db_file=DB_FILE)
    if actor is None or actor["status"] != "active" or actor["role"] != "admin":
        return jsonify({"error": "active admin actor is required"}), 403

    parent_action = "batch.set_model_provider"
    parent_params = {"instances": normalized_rows}
    try:
        with metadata_store.connect(DB_FILE) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = metadata_store.get_execution_job(request_id, conn=conn)
            if existing is None:
                instances = []
                for row in normalized_rows:
                    instance = metadata_store.get_instance_by_public_id(
                        row["instance_public_id"], conn=conn
                    )
                    if instance is None:
                        raise ValueError(
                            f"instance not found: {row['instance_public_id']}"
                        )
                    if instance["status"] != "active" or not product_supports(
                        instance["product"], "batch_set_model_provider"
                    ):
                        raise ValueError(
                            f"model provider update is not available: {row['instance_public_id']}"
                        )
                    if metadata_store.list_execution_jobs(
                        statuses=("queued", "running"), limit=1,
                        instance_public_id=row["instance_public_id"], conn=conn,
                    ):
                        raise ValueError(
                            f"instance task is already active: {row['instance_public_id']}"
                        )
                    instances.append(instance)
                metadata_store.create_execution_job(
                    request_id=request_id, actor_user_id=actor["id"],
                    action=parent_action, params=parent_params, conn=conn,
                )
                metadata_store.update_execution_job(
                    request_id, "running", current_step="creating child jobs", conn=conn
                )
                for index, (instance, row) in enumerate(zip(instances, normalized_rows), 1):
                    metadata_store.create_execution_job(
                        request_id=f"{request_id}:{index}", parent_request_id=request_id,
                        actor_user_id=actor["id"], instance_public_id=instance["public_id"],
                        action="instance.set_model_provider",
                        params={key: row[key] for key in required - {"instance_public_id"}},
                        conn=conn,
                    )
                metadata_store.update_execution_job(
                    request_id, "succeeded", output=f"queued {len(instances)} child jobs",
                    conn=conn,
                )
                metadata_store.record_operation(
                    request_id=request_id, actor_user_id=actor["id"],
                    source_service="manager-control", action=parent_action,
                    status="success", message=f"queued {len(instances)} child jobs",
                    conn=conn,
                )
            parent = metadata_store.create_execution_job(
                request_id=request_id, actor_user_id=actor["id"],
                action=parent_action, params=parent_params, conn=conn,
            )
            children = metadata_store.list_execution_jobs(
                parent_request_id=request_id, limit=100, conn=conn
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except sqlite3.IntegrityError:
        return jsonify({"error": "could not create model provider batch"}), 409
    return jsonify({
        "parent": execution_job_payload(parent, actor_public_id),
        "children": [action_batch_child_payload(job) for job in children],
    })


@app.get("/internal/v1/admin/model-provider-batches/<request_id>")
@require_services("manager-admin-web")
def get_model_provider_batch(request_id):
    parent = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if parent is None or parent["action"] != "batch.set_model_provider":
        return jsonify({"error": "model provider batch not found"}), 404
    children = metadata_store.list_execution_jobs(
        parent_request_id=request_id, limit=100, db_file=DB_FILE
    )
    return jsonify({
        "parent": execution_job_payload(parent),
        "children": [action_batch_child_payload(job) for job in children],
    })


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
    if action == "instance.set_model_provider":
        error = validate_model_provider_params(params)
        if error:
            return jsonify({"error": error}), 400
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
    if action == "instance.cleanup_failed" and (
        instance["status"] != "failed" or instance.get("product") != "evoscientist"
    ):
        return jsonify({"error": "only failed EvoScientist instances can be cleaned up"}), 409
    if action == "instance.purge_deleted" and (
        instance["status"] != "deleted" or instance.get("restore_state") != "restorable"
    ):
        return jsonify({"error": "only restorable deleted instances can be permanently deleted"}), 409
    if instance["status"] == "deleted" and action not in {"instance.restore", "instance.purge_deleted"}:
        return jsonify({"error": "deleted instance only supports restore or permanent deletion"}), 409
    if action == "instance.restore" and (
        instance["status"] != "deleted"
        or instance.get("restore_state") != "restorable"
    ):
        return jsonify({"error": "instance is not restorable"}), 409
    try:
        with metadata_store.connect(DB_FILE) as conn:
            conn.execute("BEGIN IMMEDIATE")
            exclusive_actions = {"instance.delete", "instance.restore", "instance.purge_deleted", "instance.cleanup_failed"}
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
        status=status,
        limit=limit,
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
    allowed = {"status", "current_step", "error_summary", "output", "result"}
    if set(payload) - allowed:
        return jsonify({"error": "unsupported execution job fields"}), 400
    status = payload.get("status")
    if not isinstance(status, str):
        return jsonify({"error": "status is required"}), 400
    for field in ("current_step", "error_summary", "output"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return jsonify({"error": f"{field} must be a string"}), 400
    result = payload.get("result")
    if result is not None and not isinstance(result, dict):
        return jsonify({"error": "result must be an object"}), 400
    job = metadata_store.get_execution_job(request_id, db_file=DB_FILE)
    if job is None:
        return jsonify({"error": "execution job not found"}), 404
    if job["action"] == "instance.create" and status == "succeeded":
        created_instance = metadata_store.get_instance_by_public_id(
            job["instance_public_id"], db_file=DB_FILE
        )
        product = created_instance["product"]
        required = {
            "port", "version", "access_url", "admin_url",
            "basic_auth_password_ref", "openclaw_token",
        }
        if not isinstance(result, dict) or set(result) != required:
            return jsonify({"error": "invalid instance creation result"}), 400
        if (
            not isinstance(result["port"], int)
            or not 1 <= result["port"] <= 65535
            or any(
                not isinstance(result[field], str)
                or (field != "openclaw_token" and not result[field])
                or (field == "openclaw_token" and product == "openclaw" and not result[field])
                for field in required - {"port"}
            )
        ):
            return jsonify({"error": "invalid instance creation result"}), 400
    try:
        with metadata_store.connect(DB_FILE) as conn:
            job = metadata_store.get_execution_job(request_id, conn=conn)
            metadata_store.update_execution_job(
                request_id,
                status,
                current_step=payload.get("current_step"),
                error_summary=payload.get("error_summary"),
                output=payload.get("output"),
                conn=conn,
            )
            if job["action"] == "instance.create" and status in {"succeeded", "failed"}:
                if status == "succeeded":
                    metadata_store.finish_instance_provisioning(
                        job["instance_public_id"], "active",
                        port=result["port"], openclaw_version=result["version"],
                        access_url=result["access_url"], admin_url=result["admin_url"],
                        basic_auth_password_ref=result["basic_auth_password_ref"],
                        openclaw_token=result["openclaw_token"], conn=conn,
                    )
                else:
                    metadata_store.finish_instance_provisioning(
                        job["instance_public_id"], "failed", conn=conn
                    )
                metadata_store.record_operation(
                    request_id=request_id, actor_user_id=job["actor_user_id"],
                    instance_id=job["instance_id"], source_service="manager-executor",
                    action="instance.create",
                    status="success" if status == "succeeded" else "failed",
                    message="instance created" if status == "succeeded" else "instance creation failed",
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
            if status == "succeeded" and job["action"] in {
                "instance.start", "instance.restart", "instance.stop",
            }:
                metadata_store.set_instance_runtime_status(
                    job["instance_public_id"],
                    "stopped" if job["action"] == "instance.stop" else "active",
                    conn=conn,
                )
            if status == "succeeded" and job["action"] in {
                "instance.delete", "instance.restore",
            }:
                metadata_store.set_instance_retention_state(
                    job["instance_public_id"],
                    job["action"].removeprefix("instance."),
                    conn=conn,
                )
            if status == "succeeded" and job["action"] == "instance.cleanup_failed":
                metadata_store.purge_failed_instance(job["instance_public_id"], conn=conn)
            if status == "succeeded" and job["action"] == "instance.purge_deleted":
                metadata_store.purge_deleted_instance(job["instance_public_id"], conn=conn)
            if (
                status in {"succeeded", "failed"}
                and job["action"]
                in {
                    "instance.set_basic_auth",
                    "instance.update_version",
                    "instance.install_skill",
                    "instance.set_model_provider",
                    "instance.refresh_devices",
                    "instance.approve_latest_device",
                    "instance.delete",
                    "instance.restore",
                    "instance.cleanup_failed",
                    "instance.purge_deleted",
                }
            ):
                params = json.loads(job["params_json"])
                if job["action"] == "instance.set_basic_auth":
                    message = f"Basic Auth {'enabled' if params['enabled'] else 'disabled'}"
                elif job["action"] == "instance.update_version":
                    message = f"version={params['version']}"
                elif job["action"] == "instance.install_skill":
                    message = f"skill={params['skill_id']}"
                elif job["action"] == "instance.set_model_provider":
                    message = (
                        f"provider={params['model_provider_id']} "
                        f"model={params['model_id']}"
                    )
                elif job["action"] in {"instance.delete", "instance.restore", "instance.cleanup_failed", "instance.purge_deleted"}:
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
                    instance_id=None if job["action"] in {"instance.cleanup_failed", "instance.purge_deleted"} else job["instance_id"],
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
