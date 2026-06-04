import os
import re
import shutil
import subprocess
import tempfile
import csv
import io
import threading
from urllib.parse import urlencode
from pathlib import Path

from flask import Flask, Response, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

import metadata_store


APP_DIR = Path(__file__).resolve().parent
MANAGER_DIR = Path(os.environ.get("OPENCLAW_MANAGER_DIR", "/opt/openclaw-manager"))
PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
NGINX_USERS_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
NGINX_CONTAINER_NAME = os.environ.get("NGINX_CONTAINER_NAME", "openclaw-nginx")

USER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_UPLOAD_BYTES = int(os.environ.get("MANAGER_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
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

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def validate_user_id(user_id):
    user_id = (user_id or "").strip()
    if not USER_ID_RE.fullmatch(user_id):
        return None
    return user_id


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


def parse_create_user_output(output, user_id, basic_auth_enabled, basic_auth_password):
    account = {
        "user_id": user_id,
        "basic_auth_username": user_id if basic_auth_enabled == "true" else "",
        "basic_auth_password": basic_auth_password if basic_auth_enabled == "true" else "",
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
                "size": stat.st_size,
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
    nginx_conf = NGINX_USERS_CONF_DIR / f"{user_id}.conf"
    if not nginx_conf.is_file():
        return ""

    for line in nginx_conf.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"^\s*listen\s+([0-9]+)\b", line)
        if match:
            return match.group(1)

    return ""


def is_basic_auth_enabled(user_id):
    nginx_conf = NGINX_USERS_CONF_DIR / f"{user_id}.conf"
    if not nginx_conf.is_file():
        return None

    text = nginx_conf.read_text(encoding="utf-8", errors="ignore")
    if "auth_basic off;" in text:
        return False
    if 'auth_basic "OpenClaw Login";' in text:
        return True
    return None


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


def run_instance_lifecycle_action(user_id, action):
    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return 1, f"User not found: {user_id}"

    if action == "start":
        command = ["docker", "compose", "up", "-d"]
        timeout = 90
    elif action == "stop":
        command = ["docker", "compose", "stop"]
        timeout = 60
    elif action == "restart":
        command = ["docker", "compose", "restart"]
        timeout = 90
    elif action == "delete":
        command = [str(MANAGER_DIR / "scripts" / "delete_user.sh"), user_id]
        timeout = 180
        user_dir = MANAGER_DIR
    else:
        return 1, "Invalid lifecycle action."

    result = subprocess.run(
        command,
        cwd=str(user_dir),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    return result.returncode, output


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
        download_endpoint = ""
        delete_endpoint = ""
    else:
        approve_url = url_for("approve_latest", user_id=user_id)
        refresh_url = url_for("refresh_devices", user_id=user_id)
        upload_url = url_for("upload_file", user_id=user_id)
        download_endpoint = "download_workspace_file"
        delete_endpoint = "delete_workspace_file"

    return render_template(
        "user.html",
        user_id=user_id,
        current_user=current_user,
        is_admin=current_is_admin,
        can_manage=current_can_manage,
        show_admin_links=(current_is_admin and not instance_mode),
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
        approve_url=approve_url,
        refresh_url=refresh_url,
        upload_url=upload_url,
        download_endpoint=download_endpoint,
        delete_endpoint=delete_endpoint,
        result=request.args.get("result", ""),
        error=request.args.get("error", ""),
    )


def redirect_to_user_dashboard(user_id, instance_mode=False, result="", error=""):
    values = {}
    if result:
        values["result"] = result
    if error:
        values["error"] = error

    if instance_mode:
        query = urlencode(values)
        return redirect("/admin/" + (f"?{query}" if query else ""))
    return redirect(url_for("user_detail", user_id=user_id, **values))


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
    )


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
        )

    if not user_id:
        return render_create_form(error="Invalid user id. Use letters, numbers, dot, underscore, or hyphen.", status=400)

    if basic_auth_enabled not in {"true", "false"}:
        return render_create_form(error="Invalid Basic Auth state.", status=400)

    if basic_auth_enabled == "true" and not basic_auth_password:
        return render_create_form(error="Basic Auth password is required when Basic Auth is enabled.", status=400)

    if get_user_dir(user_id).exists():
        account = LAST_CREATED_ACCOUNTS.get(user_id)
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
        account = LAST_CREATED_ACCOUNTS.get(user_id)
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
        return render_create_success(account, result=output)
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

    account = LAST_CREATED_ACCOUNTS.get(user_id)
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

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    denied = require_instance_access(user_id)
    if denied:
        return denied

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

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    denied = require_instance_access(user_id)
    if denied:
        return denied

    return refresh_devices_for_user(user_id)


@app.post("/users/<user_id>/upload")
def upload_file(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    denied = require_instance_access(user_id)
    if denied:
        return denied

    return upload_file_for_user(user_id)


@app.get("/users/<user_id>/files/<root_key>/<path:relative_path>")
def download_workspace_file(user_id, root_key, relative_path):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    denied = require_instance_access(user_id)
    if denied:
        return denied

    return download_workspace_file_for_user(user_id, root_key, relative_path)


@app.post("/users/<user_id>/files/<root_key>/<path:relative_path>/delete")
def delete_workspace_file(user_id, root_key, relative_path):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    denied = require_instance_access(user_id)
    if denied:
        return denied

    return delete_file_for_user(user_id, root_key, relative_path)


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    app.run(host=host, port=port)
