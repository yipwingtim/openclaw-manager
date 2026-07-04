import os
import re
import shutil
import subprocess
import tempfile
import csv
import io
import threading
import hmac
import time
import uuid
from urllib.parse import urlencode
from pathlib import Path

from flask import Flask, Response, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

import metadata_store


APP_DIR = Path(__file__).resolve().parent
MANAGER_DIR = Path(os.environ.get("OPENCLAW_MANAGER_DIR", "/opt/openclaw-manager"))
PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
NGINX_USERS_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
NGINX_COMPOSE_DIR = Path(os.environ.get("NGINX_COMPOSE_DIR", "/data/docker/nginx/compose"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
NGINX_CONTAINER_NAME = os.environ.get("NGINX_CONTAINER_NAME", "openclaw-nginx")
OPENCLAW_INTERNAL_TOKEN = os.environ.get("OPENCLAW_INTERNAL_TOKEN", "").strip()

USER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_.@/-]{1,128}$")
MAX_UPLOAD_BYTES = int(os.environ.get("MANAGER_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
SKILL_INSTALL_TIMEOUT = int(os.environ.get("MANAGER_SKILL_INSTALL_TIMEOUT", "180"))
WECHAT_BIND_TIMEOUT = int(os.environ.get("MANAGER_WECHAT_BIND_TIMEOUT", "300"))
BATCH_CREATE_TIMEOUT = int(os.environ.get("MANAGER_BATCH_CREATE_TIMEOUT", "3600"))
CONTAINER_UPLOAD_DIR = "/workspaces/uploads"
WORKSPACE_FILE_ROOTS = {
    "workspace": ("OpenClaw Workspace", "workspace", "/home/node/.openclaw/workspace"),
    "workspaces": ("Shared Workspaces", "workspaces", "/workspaces"),
    "uploads": ("Uploaded Files", "uploads", CONTAINER_UPLOAD_DIR),
}
DEFAULT_DOWNLOAD_EXTENSIONS = ".md,.markdown,.txt,.pdf,.doc,.docx,.xls,.xlsx,.csv,.ppt,.pptx,.zip"
DOWNLOAD_EXTENSIONS = {
    value.strip().lower()
    for value in os.environ.get("MANAGER_DOWNLOAD_EXTENSIONS", DEFAULT_DOWNLOAD_EXTENSIONS).split(",")
    if value.strip()
}
DEFAULT_PROTECTED_FILENAMES = "agents.md,soul.md,tools.md,identity.md,user.md,heartbeat.md,bootstrap.md,memory.md"
PROTECTED_FILENAMES = {
    value.strip().lower()
    for value in os.environ.get("MANAGER_PROTECTED_FILENAMES", DEFAULT_PROTECTED_FILENAMES).split(",")
    if value.strip()
}
ADMIN_USERS = {user.strip() for user in os.environ.get("MANAGER_ADMIN_USERS", "openclaw").split(",") if user.strip()}
LAST_CREATED_ACCOUNTS = {}
CREATE_EVENTS = {}
CREATE_EVENTS_LOCK = threading.Lock()
WECHAT_BIND_JOBS = {}
WECHAT_BIND_JOBS_LOCK = threading.Lock()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
if not OPENCLAW_INTERNAL_TOKEN:
    app.logger.warning("OPENCLAW_INTERNAL_TOKEN is not configured; internal proxy token checks are disabled.")

INTERNAL_PROXY_PATHS = ("/admin", "/instance-admin", "/users", "/me", "/go")


@app.before_request
def require_internal_proxy_token():
    if not request.path.startswith(INTERNAL_PROXY_PATHS):
        return None
    if not OPENCLAW_INTERNAL_TOKEN:
        return None
    provided = request.headers.get("X-OpenClaw-Internal-Token", "")
    if not hmac.compare_digest(provided, OPENCLAW_INTERNAL_TOKEN):
        return forbidden("Forbidden: invalid internal proxy token.")
    return None


@app.context_processor
def inject_actor_context():
    actor = get_actor_user()
    actor_is_admin = is_admin_user(actor) if actor else False
    return {
        "current_user": actor,
        "is_admin": actor_is_admin,
        "show_global_admin_nav": actor_is_admin,
    }


def validate_user_id(user_id):
    user_id = (user_id or "").strip()
    if not USER_ID_RE.fullmatch(user_id):
        return None
    return user_id


def configured_skill_presets():
    raw = os.environ.get("MANAGER_SKILL_PRESETS", "")
    presets = []
    seen = set()
    for item in re.split(r"[,\n]", raw):
        skill_id = item.strip()
        if not skill_id or skill_id in seen:
            continue
        if not SKILL_ID_RE.fullmatch(skill_id):
            app.logger.warning("Skip invalid MANAGER_SKILL_PRESETS entry: %s", skill_id)
            continue
        seen.add(skill_id)
        presets.append(skill_id)
    return presets


def get_user_dir(user_id):
    return PUBLIC_DIR / "users" / user_id


def get_actor_user():
    return (request.headers.get("X-Remote-User") or request.headers.get("X-Forwarded-User") or "").strip()


def get_instance_user():
    return validate_user_id(request.headers.get("X-OpenClaw-User"))


def is_admin_user(user_id=None):
    user_id = (user_id or get_actor_user()).strip()
    return user_id in ADMIN_USERS


def forbidden(message="Forbidden"):
    return render_template("error.html", message=message), 403


def batch_path_from_form(value):
    value = (value or "").strip()
    if not value:
        return None, "Path is required."

    path = Path(value)
    if not path.is_absolute():
        path = PUBLIC_DIR / "batches" / path

    try:
        resolved = path.resolve()
        batches_root = (PUBLIC_DIR / "batches").resolve()
        resolved.relative_to(batches_root)
    except ValueError:
        return None, "Path must be under batches directory."

    return resolved, ""


def read_text_preview(path, max_chars=8000):
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if len(text) > max_chars:
        return text[:max_chars] + "\n... truncated ..."
    return text


def normalize_basic_auth_enabled(value):
    value = (value or "true").strip().lower()
    if value in {"", "true", "yes", "y", "1", "on", "enabled"}:
        return "true"
    if value in {"false", "no", "n", "0", "off", "disabled"}:
        return "false"
    return None


def read_active_users_csv():
    active_users = set()
    users_csv = PUBLIC_DIR / "users.csv"
    if not users_csv.is_file():
        return active_users

    with users_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            user_id = (row.get("user_id") or "").strip()
            status = (row.get("status") or "").strip()
            if user_id and status == "active":
                active_users.add(user_id)
    return active_users


def read_port_config():
    config_file = MANAGER_DIR / "config" / "openclaw-manager.env"
    values = {}
    if not config_file.is_file():
        return None, None, None, f"Config file not found: {config_file}"

    for line in config_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key in {"PORT_START", "PORT_END", "PORT_FILE"}:
            values[key] = raw_value.strip().strip("\"'")

    try:
        port_start = int(values.get("PORT_START", ""))
        port_end = int(values.get("PORT_END", ""))
    except ValueError:
        return None, None, None, "PORT_START/PORT_END are missing or invalid in config."

    port_file = Path(values.get("PORT_FILE") or (PUBLIC_DIR / "ports.txt"))
    return port_start, port_end, port_file, ""


def estimate_port_capacity():
    port_start, port_end, port_file, error = read_port_config()
    if error:
        return None, error

    next_port = port_start
    if port_file and port_file.is_file():
        try:
            next_port = int(port_file.read_text(encoding="utf-8", errors="ignore").strip())
        except ValueError:
            next_port = port_start
    next_port = max(port_start, next_port)

    used_ports = set()
    for conf in NGINX_USERS_CONF_DIR.glob("*.conf"):
        text = conf.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"^\s*listen\s+([0-9]+)\b", text, re.MULTILINE):
            used_ports.add(int(match.group(1)))

    if port_start is None or port_end is None:
        return None, "Port range is not configured."

    total = max(0, port_end - port_start + 1)
    used_in_range = sum(1 for port in used_ports if port_start <= port <= port_end)
    allocatable = [port for port in range(next_port, port_end + 1) if port not in used_ports]
    return {
        "start": port_start,
        "end": port_end,
        "next": next_port,
        "total": total,
        "used": used_in_range,
        "available": len(allocatable),
    }, ""


def parse_batch_create_csv(path):
    rows = []
    errors = []
    seen = set()

    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            if not fieldnames or fieldnames[0] != "user_id":
                return [], ["CSV first column must be user_id."]

            for line_no, row in enumerate(reader, start=2):
                raw_user_id = (row.get("user_id") or "").strip()
                if not raw_user_id:
                    continue

                user_id = validate_user_id(raw_user_id)
                password = (row.get("basic_auth_password") or "").strip()
                basic_auth_enabled = normalize_basic_auth_enabled(row.get("basic_auth_enabled"))

                item = {
                    "line": line_no,
                    "user_id": raw_user_id,
                    "password_provided": bool(password),
                    "basic_auth_enabled": basic_auth_enabled or (row.get("basic_auth_enabled") or "").strip(),
                    "status": "ready",
                    "message": "",
                }

                if not user_id:
                    item["status"] = "invalid"
                    item["message"] = "Invalid user_id."
                    errors.append(f"line {line_no}: invalid user_id {raw_user_id}")
                elif user_id in seen:
                    item["status"] = "duplicate"
                    item["message"] = "Duplicate user_id in CSV."
                    errors.append(f"line {line_no}: duplicate user_id {user_id}")
                elif basic_auth_enabled is None:
                    item["status"] = "invalid"
                    item["message"] = "Invalid basic_auth_enabled."
                    errors.append(f"line {line_no}: invalid basic_auth_enabled")
                else:
                    item["user_id"] = user_id
                    seen.add(user_id)

                rows.append(item)
    except UnicodeDecodeError:
        return [], ["CSV must be UTF-8 encoded."]

    if not rows:
        errors.append("CSV has no user rows.")

    return rows, errors


def batch_create_paths(batch_id):
    batch_dir = PUBLIC_DIR / "batches" / "web-create-users" / batch_id
    return batch_dir, batch_dir / "input.csv", batch_dir / "results.csv"


def batch_relative_path(path):
    try:
        return str(path.resolve().relative_to((PUBLIC_DIR / "batches").resolve()))
    except ValueError:
        return ""


def batch_create_context(input_csv=None, output_csv=None, rows=None, result="", error="", capacity=None):
    return {
        "batch_create": {
            "input_csv": str(input_csv) if input_csv else "",
            "output_csv": str(output_csv) if output_csv else "",
            "output_relative_path": batch_relative_path(output_csv) if output_csv else "",
            "rows": rows or [],
            "result": result,
            "error": error,
            "capacity": capacity,
            "can_execute": bool(input_csv and rows and not error),
        }
    }


def preflight_batch_create(input_csv):
    rows, errors = parse_batch_create_csv(input_csv)
    try:
        active_users = read_active_users_csv()
    except OSError as exc:
        active_users = set()
        errors.append(f"Could not read users.csv: {exc}")

    ready_count = 0
    for row in rows:
        if row["status"] != "ready":
            continue

        user_id = row["user_id"]
        if user_id in active_users or get_user_dir(user_id).exists():
            row["status"] = "exists"
            row["message"] = "User already exists."
            errors.append(f"{user_id}: user already exists")
            continue

        ready_count += 1

    capacity, capacity_error = estimate_port_capacity()
    if capacity_error:
        errors.append(capacity_error)
    elif capacity and capacity["available"] < ready_count:
        errors.append(
            f"Not enough available ports: need {ready_count}, available {capacity['available']} "
            f"in {capacity['start']}-{capacity['end']}."
        )

    script = MANAGER_DIR / "scripts" / "batch_create_users.sh"
    if not script.is_file():
        errors.append(f"Batch create script not found: {script}")

    config_file = MANAGER_DIR / "config" / "openclaw-manager.env"
    if not config_file.is_file():
        errors.append(f"Runtime config not found: {config_file}")

    return rows, errors, capacity


def save_batch_create_account_records(output_csv):
    if not output_csv.is_file():
        return 0

    saved = 0
    with output_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            user_id = validate_user_id(row.get("user_id"))
            if not user_id or row.get("status") != "created":
                continue
            LAST_CREATED_ACCOUNTS[user_id] = row
            save_account_record(row)
            saved += 1
    return saved


def parse_create_user_output(output, user_id, basic_auth_enabled, basic_auth_password):
    account = {
        "user_id": user_id,
        "basic_auth_username": user_id,
        "basic_auth_password": basic_auth_password,
        "openclaw_token": "",
        "access_url": "",
        "admin_url": "",
        "port": "",
        "container_name": f"openclaw_{user_id}",
        "basic_auth_enabled": basic_auth_enabled,
        "status": "created",
    }

    lines = output.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Port:"):
            account["port"] = stripped.split(":", 1)[1].strip()
        elif stripped == "Access URL:" and index + 1 < len(lines):
            account["access_url"] = lines[index + 1].replace("👉", "").strip()
        elif stripped.startswith("👉 https://"):
            account["access_url"] = stripped.replace("👉", "").strip()
        elif stripped.startswith("👉 username:"):
            account["basic_auth_username"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("👉 password:"):
            account["basic_auth_password"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("👉 ") and "Token not generated" not in stripped:
            token = stripped.replace("👉", "").strip()
            if re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,}", token):
                account["openclaw_token"] = token

    if not account["access_url"] and account["port"]:
        host = PUBLIC_HOST or request.host.split(":", 1)[0]
        account["access_url"] = f"https://{host}:{account['port']}"
    if account["access_url"]:
        account["admin_url"] = account["access_url"].rstrip("/") + "/admin/"

    return account


def persist_created_instance_metadata(account):
    user_id = account.get("user_id", "")
    if not user_id:
        return ""

    port_text = account.get("port") or ""
    try:
        port = int(port_text) if port_text else None
    except ValueError:
        port = None

    try:
        access_url = account.get("access_url") or build_access_url(port_text)
        admin_url = account.get("admin_url") or (access_url.rstrip("/") + "/admin/" if access_url else "")
        openclaw_version = detect_openclaw_version(user_id)

        metadata_store.initialize(schema_file=MANAGER_DIR / "db" / "schema.sql")
        with metadata_store.connect() as conn:
            metadata_store.upsert_instance(
                user_id=user_id,
                product="openclaw",
                port=port,
                status="active",
                openclaw_version=openclaw_version,
                basic_auth_enabled=account.get("basic_auth_enabled") == "true",
                container_name=account.get("container_name") or f"openclaw_{user_id}",
                access_url=access_url,
                admin_url=admin_url,
                data_path=str(get_user_dir(user_id)),
                nginx_conf_path=str(NGINX_USERS_CONF_DIR / f"{user_id}.conf"),
                conn=conn,
            )
            metadata_store.upsert_credentials(
                user_id=user_id,
                basic_auth_username=account.get("basic_auth_username") or None,
                openclaw_token=account.get("openclaw_token") or None,
                conn=conn,
            )
            if port is not None:
                metadata_store.record_port(port, user_id=user_id, status="allocated", conn=conn)
            metadata_store.record_operation(
                action="create_instance",
                status="success",
                actor=get_actor_user() or None,
                user_id=user_id,
                message=f"port={port_text or 'unknown'} version={openclaw_version or 'unknown'}",
                finished_at=metadata_store.utc_now(),
                conn=conn,
            )
    except Exception as exc:
        app.logger.warning("Could not persist metadata for %s: %s", user_id, exc)
        return f"\n[WARN] Metadata persistence failed: {exc}"

    return ""


def persist_lifecycle_metadata(user_id, action, output=""):
    status_by_action = {
        "start": "active",
        "stop": "stopped",
        "restart": "active",
        "delete": "deleted",
    }
    status = status_by_action.get(action)
    if status is None:
        return ""

    try:
        metadata_store.initialize(schema_file=MANAGER_DIR / "db" / "schema.sql")
        existing = metadata_store.get_instance(user_id) or {}

        port_text = detect_port(user_id) or existing.get("port") or ""
        try:
            port = int(port_text) if port_text else None
        except (TypeError, ValueError):
            port = None

        access_url = existing.get("access_url") or build_access_url(port_text)
        admin_url = existing.get("admin_url") or (access_url.rstrip("/") + "/admin/" if access_url else "")
        deleted_at = metadata_store.utc_now() if action == "delete" else existing.get("deleted_at")
        detected_basic_auth_enabled = is_basic_auth_enabled(user_id)
        if detected_basic_auth_enabled is None:
            basic_auth_enabled = existing.get("basic_auth_enabled", 1) != 0
        else:
            basic_auth_enabled = detected_basic_auth_enabled

        with metadata_store.connect() as conn:
            metadata_store.upsert_instance(
                user_id=user_id,
                product=existing.get("product") or "openclaw",
                port=port,
                status=status,
                openclaw_version=existing.get("openclaw_version") or detect_openclaw_version(user_id),
                basic_auth_enabled=basic_auth_enabled,
                container_name=existing.get("container_name") or f"openclaw_{user_id}",
                access_url=access_url,
                admin_url=admin_url,
                data_path=existing.get("data_path") or str(get_user_dir(user_id)),
                nginx_conf_path=existing.get("nginx_conf_path") or str(NGINX_USERS_CONF_DIR / f"{user_id}.conf"),
                deleted_at=deleted_at,
                conn=conn,
            )
            if port is not None:
                metadata_store.record_port(
                    port,
                    user_id=None if action == "delete" else user_id,
                    status="released" if action == "delete" else "allocated",
                    conn=conn,
                )
            metadata_store.record_operation(
                action=f"{action}_instance",
                status="success",
                actor=get_actor_user() or None,
                user_id=user_id,
                message=(output or "")[-800:] or None,
                finished_at=metadata_store.utc_now(),
                conn=conn,
            )
    except Exception as exc:
        app.logger.warning("Could not persist lifecycle metadata for %s %s: %s", user_id, action, exc)
        return f"\n[WARN] Metadata persistence failed: {exc}"

    return ""


def persist_basic_auth_metadata(user_id, enabled, output=""):
    try:
        metadata_store.initialize(schema_file=MANAGER_DIR / "db" / "schema.sql")
        existing = metadata_store.get_instance(user_id) or {}

        port_text = detect_port(user_id) or existing.get("port") or ""
        try:
            port = int(port_text) if port_text else None
        except (TypeError, ValueError):
            port = None

        access_url = existing.get("access_url") or build_access_url(port_text)
        admin_url = existing.get("admin_url") or (access_url.rstrip("/") + "/admin/" if access_url else "")

        with metadata_store.connect() as conn:
            instance_status = existing.get("status")
            if not instance_status:
                instance_status = "stopped" if get_container_status(user_id) == "STOPPED" else "active"
            metadata_store.upsert_instance(
                user_id=user_id,
                product=existing.get("product") or "openclaw",
                port=port,
                status=instance_status,
                openclaw_version=existing.get("openclaw_version") or detect_openclaw_version(user_id),
                basic_auth_enabled=enabled,
                container_name=existing.get("container_name") or f"openclaw_{user_id}",
                access_url=access_url,
                admin_url=admin_url,
                data_path=existing.get("data_path") or str(get_user_dir(user_id)),
                nginx_conf_path=existing.get("nginx_conf_path") or str(NGINX_USERS_CONF_DIR / f"{user_id}.conf"),
                deleted_at=existing.get("deleted_at"),
                conn=conn,
            )
            metadata_store.record_operation(
                action="set_basic_auth",
                status="success",
                actor=get_actor_user() or None,
                user_id=user_id,
                message=f"enabled={str(enabled).lower()} {(output or '')[-700:]}".strip(),
                finished_at=metadata_store.utc_now(),
                conn=conn,
            )
    except Exception as exc:
        app.logger.warning("Could not persist Basic Auth metadata for %s: %s", user_id, exc)
        return f"\n[WARN] Metadata persistence failed: {exc}"

    return ""


def persist_operation_metadata(action, user_id=None, status="success", message=None, actor=None):
    try:
        metadata_store.initialize(schema_file=MANAGER_DIR / "db" / "schema.sql")
        metadata_store.record_operation(
            action=action,
            status=status,
            actor=actor or get_actor_user() or None,
            user_id=user_id,
            message=message,
            finished_at=metadata_store.utc_now(),
        )
    except Exception as exc:
        app.logger.warning("Could not persist operation metadata for %s %s: %s", action, user_id, exc)
        return f"\n[WARN] Metadata persistence failed: {exc}"

    return ""


def account_csv(account):
    fields = [
        "user_id",
        "basic_auth_username",
        "basic_auth_password",
        "openclaw_token",
        "access_url",
        "port",
        "container_name",
        "basic_auth_enabled",
        "status",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerow({field: account.get(field, "") for field in fields})
    return buffer.getvalue()


def account_records_dir():
    return PUBLIC_DIR / "accounts"


def account_record_path(user_id):
    return account_records_dir() / f"{user_id}_account.csv"


def save_account_record(account):
    user_id = account.get("user_id", "")
    if not user_id:
        return
    records_dir = account_records_dir()
    records_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(records_dir, 0o700)
    record_path = account_record_path(user_id)
    record_path.write_text(account_csv(account), encoding="utf-8")
    os.chmod(record_path, 0o600)


def load_account_record(user_id):
    account = LAST_CREATED_ACCOUNTS.get(user_id)
    if account is not None:
        return account

    path = account_record_path(user_id)
    if not path.is_file():
        return None

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return None

    account = rows[0]
    LAST_CREATED_ACCOUNTS[user_id] = account
    return account


def require_admin():
    actor = get_actor_user()
    if not actor or not is_admin_user(actor):
        return forbidden("Forbidden: admin access required.")
    return None


def require_instance_access(user_id, allow_admin=True):
    actor = get_actor_user()
    if not actor:
        return forbidden("Forbidden: missing authenticated user.")
    if actor == user_id:
        return None
    if allow_admin and is_admin_user(actor):
        return None
    return forbidden("Forbidden: you can only access your own instance.")


def get_upload_dir(user_id):
    return get_user_dir(user_id) / "uploads"


def list_uploaded_files(user_id):
    upload_dir = get_upload_dir(user_id)
    if not upload_dir.is_dir():
        return []

    files = []
    for path in sorted(upload_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "size": format_bytes(stat.st_size),
                "container_path": f"{CONTAINER_UPLOAD_DIR}/{path.name}",
            }
        )
    return files


def format_bytes(size):
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def is_downloadable_file(path):
    return path.is_file() and path.suffix.lower() in DOWNLOAD_EXTENSIONS


def is_protected_file(path):
    return path.name.lower() in PROTECTED_FILENAMES


def list_downloadable_files(user_id):
    user_dir = get_user_dir(user_id)
    files = []

    for root_key, (label, relative_dir, container_dir) in WORKSPACE_FILE_ROOTS.items():
        root = user_dir / relative_dir
        if not root.is_dir():
            continue

        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if not is_downloadable_file(path):
                continue
            relative_path = path.relative_to(root).as_posix()
            stat = path.stat()
            files.append(
                {
                    "root": root_key,
                    "root_label": label,
                    "name": path.name,
                    "relative_path": relative_path,
                    "container_path": f"{container_dir}/{relative_path}",
                    "size": format_bytes(stat.st_size),
                    "mtime": stat.st_mtime,
                    "can_delete": not is_protected_file(path),
                }
            )

    return files


def resolve_workspace_file(user_id, root_key, relative_path):
    root_config = WORKSPACE_FILE_ROOTS.get(root_key)
    if root_config is None:
        return None

    root = (get_user_dir(user_id) / root_config[1]).resolve()
    target = (root / relative_path).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        return None

    if not is_downloadable_file(target):
        return None

    return target


def resolve_direct_download_file(user_id, filename):
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        return None

    candidates = []
    for _, relative_dir, _ in WORKSPACE_FILE_ROOTS.values():
        root = get_user_dir(user_id) / relative_dir
        if root.is_dir():
            candidate = root / safe_name
            if is_downloadable_file(candidate):
                candidates.append(candidate)

    if len(candidates) != 1:
        return None

    return candidates[0]


def resolve_deletable_file(user_id, root_key, relative_path):
    if "/" in relative_path or "\\" in relative_path:
        return None

    target = resolve_workspace_file(user_id, root_key, relative_path)
    if target is None or is_protected_file(target):
        return None

    root_config = WORKSPACE_FILE_ROOTS.get(root_key)
    if root_config is None:
        return None

    root = (get_user_dir(user_id) / root_config[1]).resolve()
    if target.parent.resolve() != root:
        return None

    return target


def detect_port(user_id):
    for nginx_conf in nginx_user_conf_candidates(user_id):
        if not nginx_conf.is_file():
            continue
        for line in nginx_conf.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = re.match(r"^\s*listen\s+([0-9]+)\b", line)
            if match:
                return match.group(1)

    return ""


def is_basic_auth_enabled(user_id):
    for nginx_conf in nginx_user_conf_candidates(user_id):
        if not nginx_conf.is_file():
            continue

        text = nginx_conf.read_text(encoding="utf-8", errors="ignore")
        if "auth_basic off;" in text:
            return False
        if 'auth_basic "OpenClaw Login";' in text:
            return True
    return None


def nginx_disabled_conf_dir():
    return NGINX_USERS_CONF_DIR / "_disabled"


def nginx_legacy_disabled_conf_dir():
    return Path(str(NGINX_USERS_CONF_DIR) + ".disabled")


def nginx_active_user_conf(user_id):
    return NGINX_USERS_CONF_DIR / f"{user_id}.conf"


def nginx_disabled_user_conf(user_id):
    return nginx_disabled_conf_dir() / f"{user_id}.conf"


def nginx_legacy_disabled_user_conf(user_id):
    return nginx_legacy_disabled_conf_dir() / f"{user_id}.conf"


def nginx_user_conf_candidates(user_id):
    return [
        nginx_active_user_conf(user_id),
        nginx_disabled_user_conf(user_id),
        nginx_legacy_disabled_user_conf(user_id),
    ]


def run_command(command, timeout=30, cwd=None):
    result = subprocess.run(
        command,
        cwd=str(cwd or MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    return result.returncode, output


def reload_nginx():
    test_code, test_output = run_command(["docker", "exec", NGINX_CONTAINER_NAME, "nginx", "-t"], timeout=30)
    if test_code != 0:
        return test_code, f"Nginx test failed:\n{test_output}"

    reload_code, reload_output = run_command(
        ["docker", "exec", NGINX_CONTAINER_NAME, "nginx", "-s", "reload"],
        timeout=30,
    )
    if reload_code != 0:
        return reload_code, f"Nginx reload failed:\n{reload_output}"

    return 0, "\n".join(part for part in [test_output, reload_output] if part)


def refresh_nginx_after_create(user_id, actor=None):
    time.sleep(2)
    compose_code, compose_output = run_command(
        ["docker", "compose", "up", "-d"],
        timeout=90,
        cwd=NGINX_COMPOSE_DIR,
    )
    if compose_code != 0:
        persist_operation_metadata(
            "refresh_nginx_after_create",
            user_id=user_id,
            status="failed",
            actor=actor,
            message=f"Nginx compose update failed:\n{compose_output[-1200:]}",
        )
        return

    reload_code, reload_output = reload_nginx()
    status = "success" if reload_code == 0 else "failed"
    message = "\n".join(
        part
        for part in [
            f"Nginx compose update:\n{compose_output[-800:]}",
            reload_output[-1200:] if reload_output else "",
        ]
        if part
    )
    persist_operation_metadata(
        "refresh_nginx_after_create",
        user_id=user_id,
        status=status,
        actor=actor,
        message=message,
    )


def disable_nginx_user_conf(user_id):
    active_conf = nginx_active_user_conf(user_id)
    disabled_conf = nginx_disabled_user_conf(user_id)
    if not active_conf.is_file():
        if disabled_conf.is_file():
            return 0, f"Nginx config already disabled: {disabled_conf}"
        return 0, f"Nginx config not found, skip disabling: {active_conf}"

    disabled_conf.parent.mkdir(parents=True, exist_ok=True)
    if disabled_conf.exists():
        return 1, f"Disabled nginx config already exists: {disabled_conf}"

    shutil.move(str(active_conf), str(disabled_conf))
    reload_code, reload_output = reload_nginx()
    if reload_code == 0:
        return 0, f"Disabled nginx config: {disabled_conf}\n{reload_output}".strip()

    shutil.move(str(disabled_conf), str(active_conf))
    rollback_code, rollback_output = reload_nginx()
    rollback_note = "\nRolled back nginx config disable."
    if rollback_code != 0:
        rollback_note += f"\nRollback reload failed:\n{rollback_output}"
    return reload_code, f"{reload_output}{rollback_note}"


def enable_nginx_user_conf(user_id):
    active_conf = nginx_active_user_conf(user_id)
    disabled_conf = nginx_disabled_user_conf(user_id)
    legacy_disabled_conf = nginx_legacy_disabled_user_conf(user_id)
    if active_conf.is_file():
        return 0, f"Nginx config already enabled: {active_conf}"
    if not disabled_conf.is_file():
        if legacy_disabled_conf.is_file():
            disabled_conf = legacy_disabled_conf
        else:
            return 1, f"Disabled nginx config not found: {disabled_conf}"

    shutil.move(str(disabled_conf), str(active_conf))
    reload_code, reload_output = reload_nginx()
    if reload_code == 0:
        return 0, f"Enabled nginx config: {active_conf}\n{reload_output}".strip()

    shutil.move(str(active_conf), str(disabled_conf))
    rollback_code, rollback_output = reload_nginx()
    rollback_note = "\nRolled back nginx config enable."
    if rollback_code != 0:
        rollback_note += f"\nRollback reload failed:\n{rollback_output}"
    return reload_code, f"{reload_output}{rollback_note}"


def detect_openclaw_version(user_id):
    compose_file = get_user_dir(user_id) / "docker-compose.yml"
    if not compose_file.is_file():
        return ""

    text = compose_file.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"image:\s*ghcr\.io/openclaw/openclaw:([^\s]+)", text)
    if not match:
        return ""
    return match.group(1).strip().strip('"').strip("'")


def get_container_status(user_id):
    container_name = f"openclaw_{user_id}"
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{container_name}$", "--format", "{{.Status}}"],
        cwd=str(MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() or "STOPPED"


def get_container_logs(user_id, tail=120):
    container_name = f"openclaw_{user_id}"
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container_name],
        cwd=str(MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return output or "Could not read container logs."
    return output or "No recent logs."


def install_skill_for_user(user_id, skill_id):
    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return 1, f"User not found: {user_id}"

    status = get_container_status(user_id)
    if not status.startswith("Up"):
        return 1, f"Instance is not running: {status}"

    container_name = f"openclaw_{user_id}"
    result = subprocess.run(
        ["docker", "exec", container_name, "openclaw", "skills", "install", skill_id],
        cwd=str(MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=SKILL_INSTALL_TIMEOUT,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    return result.returncode, output


def run_instance_lifecycle_action(user_id, action):
    user_dir = get_user_dir(user_id)
    if action != "delete" and not user_dir.is_dir():
        return 1, f"User not found: {user_id}"

    container_name = f"openclaw_{user_id}"
    if action == "start":
        start_code, start_output = run_command(["docker", "start", container_name], timeout=90)
        if start_code != 0:
            return start_code, start_output

        nginx_code, nginx_output = enable_nginx_user_conf(user_id)
        combined_output = "\n".join(part for part in [start_output, nginx_output] if part)
        if nginx_code == 0:
            return 0, combined_output

        rollback_code, rollback_output = run_command(["docker", "stop", container_name], timeout=60)
        rollback_note = "\nRolled back container start."
        if rollback_code != 0:
            rollback_note += f"\nRollback stop failed:\n{rollback_output}"
        return nginx_code, f"{combined_output}{rollback_note}"
    elif action == "stop":
        nginx_code, nginx_output = disable_nginx_user_conf(user_id)
        if nginx_code != 0:
            return nginx_code, nginx_output

        stop_code, stop_output = run_command(["docker", "stop", container_name], timeout=60)
        combined_output = "\n".join(part for part in [nginx_output, stop_output] if part)
        if stop_code == 0:
            return 0, combined_output

        rollback_code, rollback_output = enable_nginx_user_conf(user_id)
        rollback_note = "\nRolled back nginx config disable."
        if rollback_code != 0:
            rollback_note += f"\nRollback enable failed:\n{rollback_output}"
        return stop_code, f"{combined_output}{rollback_note}"
    elif action == "restart":
        command = ["docker", "restart", container_name]
        timeout = 90
    elif action == "delete":
        command = [str(MANAGER_DIR / "scripts" / "delete_user.sh"), user_id]
        timeout = 180
    else:
        return 1, "Invalid lifecycle action."

    returncode, output = run_command(command, timeout=timeout)
    return returncode, output


def read_devices_cache(user_id):
    cache_file = get_user_dir(user_id) / "devices.txt"
    if not cache_file.is_file():
        return "No device cache found yet."
    return cache_file.read_text(encoding="utf-8", errors="ignore")


def build_access_url(port):
    if not port:
        return ""
    host = PUBLIC_HOST or request.host.split(":", 1)[0]
    return f"https://{host}:{port}"


def list_active_users(status_filter="running"):
    users_dir = PUBLIC_DIR / "users"
    if not users_dir.is_dir():
        return []

    if status_filter not in {"running", "stopped", "all"}:
        status_filter = "running"

    users = []
    for user_dir in sorted(users_dir.iterdir(), key=lambda item: item.name):
        if not user_dir.is_dir():
            continue
        user_id = validate_user_id(user_dir.name)
        if not user_id:
            continue
        port = detect_port(user_id)
        status = get_container_status(user_id)
        is_stopped = status == "STOPPED"
        if status_filter == "running" and is_stopped:
            continue
        if status_filter == "stopped" and not is_stopped:
            continue
        users.append(
            {
                "user_id": user_id,
                "status": status,
                "port": port,
                "openclaw_version": detect_openclaw_version(user_id),
                "access_url": build_access_url(port),
                "basic_auth_enabled": is_basic_auth_enabled(user_id),
            }
        )
    return users


def render_user_dashboard(user_id, instance_mode=False):
    port = detect_port(user_id)
    current_user = get_actor_user() or (user_id if instance_mode else "")
    current_is_admin = False if instance_mode else is_admin_user(current_user)
    current_can_manage = instance_mode or current_is_admin or current_user == user_id

    if instance_mode:
        approve_url = "/admin/approve-latest"
        refresh_url = "/admin/refresh-devices"
        upload_url = "/admin/upload"
        wechat_bind_url = "/admin/wechat-bind-url"
        wechat_bind_cancel_url = "/admin/wechat-bind-cancel"
        download_endpoint = ""
        delete_endpoint = ""
    else:
        approve_url = url_for("approve_latest", user_id=user_id)
        refresh_url = url_for("refresh_devices", user_id=user_id)
        upload_url = url_for("upload_file", user_id=user_id)
        wechat_bind_url = url_for("user_wechat_bind_url", user_id=user_id)
        wechat_bind_cancel_url = url_for("user_wechat_bind_cancel", user_id=user_id)
        download_endpoint = "download_workspace_file"
        delete_endpoint = "delete_workspace_file"

    with WECHAT_BIND_JOBS_LOCK:
        wechat_bind_job = dict(WECHAT_BIND_JOBS.get(user_id, {}))
    wechat_url = request.args.get("wechat_url", "") or wechat_bind_job.get("bind_url", "")

    return render_template(
        "user.html",
        user_id=user_id,
        current_user=current_user,
        is_admin=current_is_admin,
        can_manage=current_can_manage,
        show_admin_links=(current_is_admin and not instance_mode),
        show_global_admin_nav=(current_is_admin and not instance_mode),
        instance_mode=instance_mode,
        port=port,
        access_url=build_access_url(port),
        status=get_container_status(user_id),
        devices_cache=read_devices_cache(user_id),
        recent_logs=get_container_logs(user_id),
        uploaded_files=list_uploaded_files(user_id),
        downloadable_files=list_downloadable_files(user_id),
        download_extensions=", ".join(sorted(DOWNLOAD_EXTENSIONS)),
        protected_filenames=", ".join(sorted(PROTECTED_FILENAMES)),
        container_upload_dir=CONTAINER_UPLOAD_DIR,
        max_upload_mb=MAX_UPLOAD_BYTES // 1024 // 1024,
        wechat_bind_timeout_seconds=WECHAT_BIND_TIMEOUT,
        wechat_bind_timeout_minutes=max(1, WECHAT_BIND_TIMEOUT // 60),
        approve_url=approve_url,
        refresh_url=refresh_url,
        upload_url=upload_url,
        wechat_bind_url=wechat_bind_url,
        wechat_bind_cancel_url=wechat_bind_cancel_url,
        download_endpoint=download_endpoint,
        delete_endpoint=delete_endpoint,
        wechat_url=wechat_url,
        wechat_bind_job=wechat_bind_job,
        result=request.args.get("result", ""),
        error=request.args.get("error", ""),
    )


def redirect_to_user_dashboard(user_id, instance_mode=False, result="", error="", wechat_url=""):
    values = {}
    if result:
        values["result"] = result
    if error:
        values["error"] = error
    if wechat_url:
        values["wechat_url"] = wechat_url

    if instance_mode:
        query = urlencode(values)
        return redirect("/admin/" + (f"?{query}" if query else ""))
    return redirect(url_for("user_detail", user_id=user_id, **values))


def extract_wechat_bind_url(output):
    match = re.search(r"https://liteapp\.weixin\.qq\.com/q/[^\s\"'<>]+", output or "")
    if not match:
        return ""
    return match.group(0).strip()


def update_wechat_bind_job(user_id, job_id=None, **values):
    with WECHAT_BIND_JOBS_LOCK:
        job = WECHAT_BIND_JOBS.setdefault(user_id, {})
        if job_id is not None and job.get("job_id") != job_id:
            return False
        job.update(values)
        return True


def run_wechat_bind_job(user_id, container_name, job_id):
    command = [
        "docker",
        "exec",
        container_name,
        "sh",
        "-lc",
        f"timeout {WECHAT_BIND_TIMEOUT}s npx -y @tencent-weixin/openclaw-weixin-cli install",
    ]
    output_parts = []
    try:
        process = subprocess.Popen(
            command,
            cwd=str(MANAGER_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if not update_wechat_bind_job(user_id, job_id=job_id, process=process, status="running"):
            process.terminate()
            return

        assert process.stdout is not None
        for line in process.stdout:
            output_parts.append(line)
            output = "".join(output_parts)
            bind_url = extract_wechat_bind_url(output)
            if bind_url:
                update_wechat_bind_job(user_id, job_id=job_id, status="waiting_confirmation", bind_url=bind_url)

        returncode = process.wait(timeout=5)
        output = "".join(output_parts).strip()
        if returncode == 0:
            update_wechat_bind_job(
                user_id,
                job_id=job_id,
                status="success",
                bind_url="",
                output_preview=output[-500:] if output else "",
                process=None,
            )
            persist_operation_metadata("generate_wechat_bind_url", user_id=user_id, message="wechat bind completed")
        else:
            update_wechat_bind_job(
                user_id,
                job_id=job_id,
                status="failed",
                bind_url="",
                error=output[-500:] if output else "微信插件命令执行失败或超时。",
                process=None,
            )
    except Exception as exc:
        output = "".join(output_parts).strip()
        update_wechat_bind_job(
            user_id,
            job_id=job_id,
            status="failed",
            error=f"{exc}\n{output[-500:] if output else ''}".strip(),
            process=None,
        )


def cancel_wechat_bind_job_for_user(user_id, instance_mode=False):
    with WECHAT_BIND_JOBS_LOCK:
        job = WECHAT_BIND_JOBS.pop(user_id, None)

    process = job.get("process") if job else None
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except Exception:
            pass

    return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, result="微信绑定任务已取消，页面状态已清除。")


def generate_wechat_bind_url_for_user(user_id, instance_mode=False):
    container_name = f"openclaw_{user_id}"
    if get_container_status(user_id) == "STOPPED":
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error="实例容器未运行，无法生成微信绑定链接。")

    with WECHAT_BIND_JOBS_LOCK:
        existing_job = WECHAT_BIND_JOBS.get(user_id)
        if existing_job and existing_job.get("status") in {"starting", "running", "waiting_confirmation"}:
            bind_url = existing_job.get("bind_url", "")
            message = (
                f"微信绑定链接已生成，请在 {max(1, WECHAT_BIND_TIMEOUT // 60)} 分钟内完成绑定；如需重新生成，请先取消当前任务。"
                if bind_url
                else f"微信绑定任务正在运行，请在 {max(1, WECHAT_BIND_TIMEOUT // 60)} 分钟内完成绑定。"
            )
            return redirect_to_user_dashboard(
                user_id,
                instance_mode=instance_mode,
                result=message,
                wechat_url=bind_url,
            )
        job_id = f"{user_id}:{metadata_store.utc_now()}"
        WECHAT_BIND_JOBS[user_id] = {"job_id": job_id, "status": "starting", "bind_url": "", "error": "", "output_preview": "", "process": None}

    thread = threading.Thread(target=run_wechat_bind_job, args=(user_id, container_name, job_id), daemon=True)
    thread.start()

    return redirect_to_user_dashboard(
        user_id,
        instance_mode=instance_mode,
        result="微信绑定任务已启动，页面会自动刷新并显示绑定链接。",
    )


def summarize_approval_output(output):
    if "No pending device request found" in output:
        return "No pending device request found."
    if re.search(r"\bApproved\b|\bapproved\b", output):
        return "Approved latest device request."
    if "Device cache updated" in output:
        return "Device approval command completed."
    return output[-800:]


def approve_latest_for_user(user_id, instance_mode=False):
    script = MANAGER_DIR / "scripts" / "approve_device.sh"
    result = subprocess.run(
        [str(script), user_id, "--latest"],
        cwd=str(MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error=summarize_approval_output(output))
    summary = summarize_approval_output(output)
    operation_status = "skipped" if summary == "No pending device request found." else "success"
    summary += persist_operation_metadata("approve_device", user_id=user_id, status=operation_status, message=summary)
    return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, result=summary)


def refresh_devices_for_user(user_id, instance_mode=False):
    script = MANAGER_DIR / "scripts" / "approve_device.sh"
    result = subprocess.run(
        [str(script), user_id, "--list-only"],
        cwd=str(MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error=output[-1200:])
    message = "Device cache refreshed."
    message += persist_operation_metadata("refresh_devices", user_id=user_id, message=message)
    return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, result=message)


def upload_file_for_user(user_id, instance_mode=False):
    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error="No file selected.")

    filename = secure_filename(uploaded.filename)
    if not filename:
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error="Invalid filename.")
    if Path(filename).suffix.lower() not in DOWNLOAD_EXTENSIONS:
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error=f"Unsupported file type: {filename}")

    upload_dir = get_upload_dir(user_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    target = upload_dir / filename
    if target.exists():
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error=f"File already exists: {filename}")

    uploaded.save(target)
    os.chmod(target, 0o644)

    message = f"Uploaded {filename} to {CONTAINER_UPLOAD_DIR}/{filename}"
    message += persist_operation_metadata("upload_file", user_id=user_id, message=message)
    return redirect_to_user_dashboard(
        user_id,
        instance_mode=instance_mode,
        result=message,
    )


def download_workspace_file_for_user(user_id, root_key, relative_path):
    target = resolve_workspace_file(user_id, root_key, relative_path)
    if target is None:
        return render_template("error.html", message="File not found."), 404
    return send_file(target, as_attachment=True, download_name=target.name)


def download_direct_file_for_user(user_id, filename):
    target = resolve_direct_download_file(user_id, filename)
    if target is None:
        return render_template("error.html", message="File not found or filename is ambiguous."), 404
    return send_file(target, as_attachment=True, download_name=target.name)


def delete_file_for_user(user_id, root_key, relative_path, instance_mode=False):
    target = resolve_deletable_file(user_id, root_key, relative_path)
    if target is None:
        return redirect_to_user_dashboard(
            user_id,
            instance_mode=instance_mode,
            error="File cannot be deleted from this panel.",
        )

    filename = target.name
    target.unlink()
    message = f"Deleted {filename}."
    message += persist_operation_metadata(
        "delete_file",
        user_id=user_id,
        message=f"Deleted {root_key}/{relative_path}",
    )
    return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, result=message)


@app.get("/")
def index():
    actor = get_actor_user()
    if actor and is_admin_user(actor):
        return redirect(url_for("admin_users"))
    if actor:
        return redirect(url_for("my_instance"))
    return render_template("index.html", current_user="", is_admin=False)


@app.get("/me")
def my_instance():
    actor = get_actor_user()
    if not actor:
        return forbidden("Forbidden: missing authenticated user.")
    if is_admin_user(actor):
        return redirect(url_for("admin_users"))
    return redirect(url_for("user_detail", user_id=actor))


@app.get("/admin")
@app.get("/admin/")
def admin_home():
    denied = require_admin()
    if denied:
        return denied
    return redirect(url_for("admin_users"))


@app.get("/admin/users")
def admin_users():
    denied = require_admin()
    if denied:
        return denied
    status_filter = request.args.get("status", "running").strip().lower()
    if status_filter not in {"running", "stopped", "all"}:
        status_filter = "running"
    return render_template(
        "admin_users.html",
        users=list_active_users(status_filter),
        status_filter=status_filter,
        skill_presets=configured_skill_presets(),
        result=request.args.get("result", ""),
        error=request.args.get("error", ""),
    )


@app.get("/admin/metadata")
def admin_metadata():
    denied = require_admin()
    if denied:
        return denied

    error = ""
    counts = {}
    instances = []
    operations = []
    db_file = str(metadata_store.DB_FILE)
    try:
        metadata_store.initialize(schema_file=MANAGER_DIR / "db" / "schema.sql")
        counts = metadata_store.table_counts()
        instances = metadata_store.list_instances()[:20]
        operations = metadata_store.list_operations(limit=20)
    except Exception as exc:
        error = f"Could not read metadata database: {exc}"

    return render_template(
        "admin_metadata.html",
        db_file=db_file,
        counts=counts,
        instances=instances,
        operations=operations,
        error=error,
    )


@app.get("/admin/create-user")
def admin_create_user():
    denied = require_admin()
    if denied:
        return denied
    return render_template(
        "admin_create_user.html",
        user_id="",
        basic_auth_enabled="true",
        basic_auth_password="",
        account=None,
        account_csv="",
        result="",
        error="",
        **batch_create_context(),
    )


@app.get("/admin/create-user/<user_id>")
def admin_created_user_detail(user_id):
    denied = require_admin()
    if denied:
        return denied

    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    account = load_account_record(user_id)
    if account is None:
        return render_template("error.html", message="Created account record not found. Create the instance again or use batch export."), 404

    return render_template(
        "admin_create_user.html",
        user_id="",
        basic_auth_enabled="true",
        basic_auth_password="",
        account=account,
        account_csv=account_csv(account),
        result=request.args.get("result", ""),
        error="",
        **batch_create_context(),
    )


@app.post("/admin/create-user/batch/preview")
def preview_admin_batch_create_users():
    denied = require_admin()
    if denied:
        return denied

    upload = request.files.get("input_file")
    existing_input = (request.form.get("input_csv") or "").strip()
    if upload and upload.filename:
        if not upload.filename.lower().endswith(".csv"):
            return render_template(
                "admin_create_user.html",
                user_id="",
                basic_auth_enabled="true",
                basic_auth_password="",
                account=None,
                account_csv="",
                result="",
                error="",
                **batch_create_context(error="Uploaded file must be a CSV."),
            ), 400
        batch_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        batch_dir, input_csv, output_csv = batch_create_paths(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)
        upload.save(input_csv)
    else:
        input_csv, input_error = batch_path_from_form(existing_input)
        if input_error:
            return render_template(
                "admin_create_user.html",
                user_id="",
                basic_auth_enabled="true",
                basic_auth_password="",
                account=None,
                account_csv="",
                result="",
                error="",
                **batch_create_context(error=input_error),
            ), 400
        output_csv = input_csv.with_name(input_csv.stem + "_results.csv")

    rows, errors, capacity = preflight_batch_create(input_csv)
    error = "\n".join(errors)
    return render_template(
        "admin_create_user.html",
        user_id="",
        basic_auth_enabled="true",
        basic_auth_password="",
        account=None,
        account_csv="",
        result="",
        error="",
        **batch_create_context(input_csv=input_csv, output_csv=output_csv, rows=rows, error=error, capacity=capacity),
    ), 400 if errors else 200


@app.post("/admin/create-user/batch/run")
def run_admin_batch_create_users():
    denied = require_admin()
    if denied:
        return denied

    input_csv, input_error = batch_path_from_form(request.form.get("input_csv"))
    output_csv, output_error = batch_path_from_form(request.form.get("output_csv"))
    if input_error or output_error:
        return render_template(
            "admin_create_user.html",
            user_id="",
            basic_auth_enabled="true",
            basic_auth_password="",
            account=None,
            account_csv="",
            result="",
            error="",
            **batch_create_context(error=input_error or output_error),
        ), 400

    if not input_csv.is_file():
        return render_template(
            "admin_create_user.html",
            user_id="",
            basic_auth_enabled="true",
            basic_auth_password="",
            account=None,
            account_csv="",
            result="",
            error="",
            **batch_create_context(input_csv=input_csv, output_csv=output_csv, error=f"Input CSV not found: {input_csv}"),
        ), 400

    rows, errors, capacity = preflight_batch_create(input_csv)
    if errors:
        return render_template(
            "admin_create_user.html",
            user_id="",
            basic_auth_enabled="true",
            basic_auth_password="",
            account=None,
            account_csv="",
            result="",
            error="",
            **batch_create_context(
                input_csv=input_csv,
                output_csv=output_csv,
                rows=rows,
                error="\n".join(errors),
                capacity=capacity,
            ),
        ), 400

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    script = MANAGER_DIR / "scripts" / "batch_create_users.sh"
    process = subprocess.run(
        [str(script), str(input_csv), str(output_csv)],
        cwd=str(MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=BATCH_CREATE_TIMEOUT,
        check=False,
    )
    command_output = (process.stdout + "\n" + process.stderr).strip()
    result_csv = read_text_preview(output_csv, max_chars=12000)
    saved_count = save_batch_create_account_records(output_csv)
    result = f"{command_output}\n\nResult CSV:\n{result_csv}".strip()

    if process.returncode != 0:
        return render_template(
            "admin_create_user.html",
            user_id="",
            basic_auth_enabled="true",
            basic_auth_password="",
            account=None,
            account_csv="",
            result=result,
            error="Batch create failed.",
            **batch_create_context(input_csv=input_csv, output_csv=output_csv, rows=rows, capacity=capacity),
        ), 500

    result += persist_operation_metadata(
        "batch_create_users",
        message=f"input={input_csv.name} output={output_csv.name} saved_accounts={saved_count} {command_output[-500:]}".strip(),
    )
    return render_template(
        "admin_create_user.html",
        user_id="",
        basic_auth_enabled="true",
        basic_auth_password="",
        account=None,
        account_csv="",
        result=result,
        error="",
        **batch_create_context(input_csv=input_csv, output_csv=output_csv, rows=rows, capacity=capacity),
    )


@app.get("/admin/create-user/batch/results/<path:relative_path>")
def download_batch_create_results(relative_path):
    denied = require_admin()
    if denied:
        return denied

    output_csv, error = batch_path_from_form(relative_path)
    if error:
        return render_template("error.html", message=error), 400
    if not output_csv.is_file():
        return render_template("error.html", message="Result CSV not found."), 404
    return send_file(output_csv, as_attachment=True, download_name=output_csv.name)


@app.post("/admin/create-user")
def run_admin_create_user():
    denied = require_admin()
    if denied:
        return denied

    user_id_input = (request.form.get("user_id") or "").strip()
    user_id = validate_user_id(user_id_input)
    basic_auth_enabled = (request.form.get("basic_auth_enabled") or "true").strip().lower()
    basic_auth_password = request.form.get("basic_auth_password") or ""

    def render_create_form(result="", error="", status=200):
        return (
            render_template(
                "admin_create_user.html",
                user_id=user_id_input,
                basic_auth_enabled=basic_auth_enabled,
                basic_auth_password="",
                account=None,
                account_csv="",
                result=result,
                error=error,
                **batch_create_context(),
            ),
            status,
        )

    def render_create_success(account, result=""):
        return render_template(
            "admin_create_user.html",
            user_id="",
            basic_auth_enabled="true",
            basic_auth_password="",
            account=account,
            account_csv=account_csv(account),
            result=result,
            error="",
            **batch_create_context(),
        )

    if not user_id:
        return render_create_form(error="Invalid user id. Use letters, numbers, dot, underscore, or hyphen.", status=400)

    if basic_auth_enabled not in {"true", "false"}:
        return render_create_form(error="Invalid Basic Auth state.", status=400)

    if not basic_auth_password:
        return render_create_form(error="Basic Auth password is required for the instance admin page.", status=400)

    if get_user_dir(user_id).exists():
        account = load_account_record(user_id)
        if account is not None:
            return render_create_success(account, result=f"Instance already created: {user_id}")
        return render_create_form(error=f"User already exists: {user_id}", status=400)

    with CREATE_EVENTS_LOCK:
        create_event = CREATE_EVENTS.get(user_id)
        if create_event is None:
            create_event = threading.Event()
            CREATE_EVENTS[user_id] = create_event
            owns_create = True
        else:
            owns_create = False

    if not owns_create:
        create_event.wait(timeout=430)
        account = load_account_record(user_id)
        if account is not None:
            return render_create_success(account, result=f"Instance creation completed: {user_id}")
        if get_user_dir(user_id).exists():
            return render_create_form(
                result=f"Instance directory now exists for {user_id}. Check /admin/users for details.",
                error="Create request was already submitted.",
                status=409,
            )
        return render_create_form(error=f"Create request is already running: {user_id}", status=409)

    command = [
        str(MANAGER_DIR / "scripts" / "create_user.sh"),
        user_id,
        "--basic-auth-enabled",
        basic_auth_enabled,
        "--skip-nginx-reload",
    ]
    if basic_auth_password:
        command.extend(["--password", basic_auth_password])

    try:
        process = subprocess.run(
            command,
            cwd=str(MANAGER_DIR),
            text=True,
            capture_output=True,
            timeout=420,
            check=False,
        )
        output = (process.stdout + "\n" + process.stderr).strip()
        if process.returncode != 0:
            return render_create_form(result=output, error="Create instance failed.", status=500)

        account = parse_create_user_output(output, user_id, basic_auth_enabled, basic_auth_password)
        LAST_CREATED_ACCOUNTS[user_id] = account
        save_account_record(account)
        actor = get_actor_user() or None
        threading.Thread(
            target=refresh_nginx_after_create,
            args=(user_id, actor),
            daemon=True,
        ).start()
        return redirect(
            url_for(
                "admin_created_user_detail",
                user_id=user_id,
                result="Instance created. Nginx update is running in the background.",
            )
        )
    finally:
        create_event.set()
        with CREATE_EVENTS_LOCK:
            if CREATE_EVENTS.get(user_id) is create_event:
                del CREATE_EVENTS[user_id]


@app.get("/admin/create-user/<user_id>/account.csv")
def download_created_account_csv(user_id):
    denied = require_admin()
    if denied:
        return denied

    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    account = load_account_record(user_id)
    if account is None:
        return render_template("error.html", message="Created account record not found. Create the instance again or use batch export."), 404

    return Response(
        account_csv(account),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={user_id}_account.csv"},
    )


@app.post("/admin/users/<user_id>/basic-auth")
def admin_set_basic_auth(user_id):
    denied = require_admin()
    if denied:
        return denied

    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    enabled = request.form.get("enabled", "").strip().lower()
    if enabled not in {"true", "false"}:
        return redirect(url_for("admin_users", error="Invalid Basic Auth state."))

    nginx_conf = NGINX_USERS_CONF_DIR / f"{user_id}.conf"
    if not nginx_conf.is_file():
        return redirect(url_for("admin_users", error=f"Nginx config not found: {user_id}"))

    backup_fd, backup_name = tempfile.mkstemp(prefix=f"{user_id}.", suffix=".conf")
    os.close(backup_fd)
    backup_path = Path(backup_name)
    shutil.copy2(nginx_conf, backup_path)

    try:
        update = subprocess.run(
            [str(MANAGER_DIR / "scripts" / "set_basic_auth.sh"), enabled, user_id],
            cwd=str(MANAGER_DIR),
            env={**os.environ, "OPENCLAW_SKIP_METADATA_WRITE": "1"},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        update_output = (update.stdout + "\n" + update.stderr).strip()
        if update.returncode != 0:
            shutil.copy2(backup_path, nginx_conf)
            return redirect(url_for("admin_users", error=update_output[-1200:]))

        test = subprocess.run(
            ["docker", "exec", NGINX_CONTAINER_NAME, "nginx", "-t"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        test_output = (test.stdout + "\n" + test.stderr).strip()
        if test.returncode != 0:
            shutil.copy2(backup_path, nginx_conf)
            return redirect(url_for("admin_users", error=f"Nginx test failed. Restored config.\n{test_output[-1200:]}"))

        reload_result = subprocess.run(
            ["docker", "exec", NGINX_CONTAINER_NAME, "nginx", "-s", "reload"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        reload_output = (reload_result.stdout + "\n" + reload_result.stderr).strip()
        if reload_result.returncode != 0:
            shutil.copy2(backup_path, nginx_conf)
            subprocess.run(
                ["docker", "exec", NGINX_CONTAINER_NAME, "nginx", "-s", "reload"],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            return redirect(url_for("admin_users", error=f"Nginx reload failed. Restored config.\n{reload_output[-1200:]}"))

        state = "enabled" if enabled == "true" else "disabled"
        metadata_warning = persist_basic_auth_metadata(user_id, enabled == "true", update_output)
        return redirect(url_for("admin_users", result=f"Basic Auth {state}: {user_id}{metadata_warning}"))
    finally:
        backup_path.unlink(missing_ok=True)


@app.post("/admin/users/<user_id>/lifecycle")
def admin_instance_lifecycle(user_id):
    denied = require_admin()
    if denied:
        return denied

    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    action = (request.form.get("action") or "").strip().lower()
    if action not in {"start", "stop", "restart", "delete"}:
        return redirect(url_for("admin_users", error="Invalid instance action."))

    returncode, output = run_instance_lifecycle_action(user_id, action)
    clipped_output = output[-1200:] if output else ""
    label = action.capitalize()
    if returncode != 0:
        return redirect(url_for("admin_users", error=f"{label} failed: {user_id}\n{clipped_output}"))

    output += persist_lifecycle_metadata(user_id, action, output)
    clipped_output = output[-1200:] if output else ""
    return redirect(url_for("admin_users", result=f"{label} completed: {user_id}\n{clipped_output}"))


@app.post("/admin/users/bulk-skill-install")
def admin_bulk_skill_install():
    denied = require_admin()
    if denied:
        return denied

    presets = configured_skill_presets()
    skill_id = (request.form.get("skill_id") or "").strip()
    if skill_id not in presets:
        return redirect(url_for("admin_users", error="Invalid or unconfigured skill preset."))

    raw_user_ids = request.form.get("user_ids", "")
    user_ids = []
    seen = set()
    for item in re.split(r"[\s,]+", raw_user_ids):
        user_id = validate_user_id(item)
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        user_ids.append(user_id)

    if not user_ids:
        return redirect(url_for("admin_users", error="No valid user ids selected for skill installation."))

    summaries = []
    errors = []
    for user_id in user_ids:
        returncode, output = install_skill_for_user(user_id, skill_id)
        clipped_output = output[-500:] if output else ""
        if returncode == 0:
            summaries.append(f"[OK] {user_id}: {skill_id} installed")
            persist_operation_metadata("install_skill", user_id=user_id, message=f"Installed {skill_id}")
        else:
            errors.append(f"[ERROR] {user_id}: {clipped_output or 'skill install failed'}")

    message = "\n".join(summaries + errors)
    if errors:
        return redirect(url_for("admin_users", error=message[-1800:]))
    return redirect(url_for("admin_users", result=message[-1800:]))


@app.get("/instance-admin")
@app.get("/instance-admin/")
def instance_admin():
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return render_user_dashboard(user_id, instance_mode=True)


@app.get("/instance-admin/help")
def instance_admin_help():
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return render_template(
        "instance_admin_help.html",
        user_id=user_id,
        current_user=user_id,
        is_admin=False,
        instance_mode=True,
        upload_dir=CONTAINER_UPLOAD_DIR,
        max_upload_mb=MAX_UPLOAD_BYTES // 1024 // 1024,
        download_extensions=", ".join(sorted(DOWNLOAD_EXTENSIONS)),
        protected_filenames=", ".join(sorted(PROTECTED_FILENAMES)),
    )


@app.post("/instance-admin/approve-latest")
def instance_approve_latest():
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return approve_latest_for_user(user_id, instance_mode=True)


@app.post("/instance-admin/refresh-devices")
def instance_refresh_devices():
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return refresh_devices_for_user(user_id, instance_mode=True)


@app.post("/instance-admin/upload")
def instance_upload_file():
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return upload_file_for_user(user_id, instance_mode=True)


@app.post("/instance-admin/wechat-bind-url")
def instance_wechat_bind_url():
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return generate_wechat_bind_url_for_user(user_id, instance_mode=True)


@app.post("/instance-admin/wechat-bind-cancel")
def instance_wechat_bind_cancel():
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return cancel_wechat_bind_job_for_user(user_id, instance_mode=True)


@app.get("/instance-admin/files/<root_key>/<path:relative_path>")
def instance_download_workspace_file(root_key, relative_path):
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return download_workspace_file_for_user(user_id, root_key, relative_path)


@app.get("/instance-admin/files/<filename>")
def instance_download_direct_file(filename):
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return download_direct_file_for_user(user_id, filename)


@app.post("/instance-admin/files/<root_key>/<path:relative_path>/delete")
def instance_delete_workspace_file(root_key, relative_path):
    user_id = get_instance_user()
    if not user_id:
        return forbidden("Forbidden: missing instance user header.")
    return delete_file_for_user(user_id, root_key, relative_path, instance_mode=True)


@app.get("/admin/device-approvals")
def admin_device_approvals():
    denied = require_admin()
    if denied:
        return denied
    return render_template("admin_device_approvals.html", result="", error="")


@app.post("/admin/device-approvals")
def run_admin_device_approvals():
    denied = require_admin()
    if denied:
        return denied

    input_path, input_error = batch_path_from_form(request.form.get("input_csv"))
    output_path, output_error = batch_path_from_form(request.form.get("output_csv"))
    action = request.form.get("action", "approve")

    if input_error or output_error:
        return render_template(
            "admin_device_approvals.html",
            result="",
            error=input_error or output_error,
        ), 400

    if not input_path.is_file():
        return render_template(
            "admin_device_approvals.html",
            result="",
            error=f"Input CSV not found: {input_path}",
        ), 400

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if action == "preview":
        script = MANAGER_DIR / "scripts" / "batch_preview_device_requests.sh"
    else:
        script = MANAGER_DIR / "scripts" / "batch_approve_devices.sh"

    process = subprocess.run(
        [str(script), str(input_path), str(output_path)],
        cwd=str(MANAGER_DIR),
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    command_output = (process.stdout + "\n" + process.stderr).strip()
    result_csv = read_text_preview(output_path)
    result = f"{command_output}\n\nResult CSV:\n{result_csv}".strip()

    if process.returncode != 0:
        return render_template("admin_device_approvals.html", result=result, error="Batch device operation failed."), 500

    result += persist_operation_metadata(
        "batch_device_approvals",
        message=f"action={action} input={input_path.name} output={output_path.name} {command_output[-500:]}".strip(),
    )
    return render_template("admin_device_approvals.html", result=result, error="")


@app.post("/go")
def go():
    user_id = validate_user_id(request.form.get("user_id"))
    if not user_id:
        return render_template("index.html", error="Invalid user id."), 400
    return redirect(url_for("user_detail", user_id=user_id))


@app.get("/users/<user_id>")
def user_detail(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return render_user_dashboard(user_id)


@app.post("/users/<user_id>/approve-latest")
def approve_latest(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return approve_latest_for_user(user_id)


@app.post("/users/<user_id>/refresh-devices")
def refresh_devices(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return refresh_devices_for_user(user_id)


@app.post("/users/<user_id>/upload")
def upload_file(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return upload_file_for_user(user_id)


@app.post("/users/<user_id>/wechat-bind-url")
def user_wechat_bind_url(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return generate_wechat_bind_url_for_user(user_id)


@app.post("/users/<user_id>/wechat-bind-cancel")
def user_wechat_bind_cancel(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return cancel_wechat_bind_job_for_user(user_id)


@app.get("/users/<user_id>/files/<root_key>/<path:relative_path>")
def download_workspace_file(user_id, root_key, relative_path):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return download_workspace_file_for_user(user_id, root_key, relative_path)


@app.post("/users/<user_id>/files/<root_key>/<path:relative_path>/delete")
def delete_workspace_file(user_id, root_key, relative_path):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    denied = require_instance_access(user_id)
    if denied:
        return denied

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    return delete_file_for_user(user_id, root_key, relative_path)


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    app.run(host=host, port=port)
