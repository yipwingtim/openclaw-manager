import os
import re
import subprocess
from urllib.parse import urlencode
from pathlib import Path

from flask import Flask, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename


APP_DIR = Path(__file__).resolve().parent
MANAGER_DIR = Path(os.environ.get("OPENCLAW_MANAGER_DIR", "/opt/openclaw-manager"))
PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
NGINX_USERS_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")

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


def list_active_users():
    users_dir = PUBLIC_DIR / "users"
    if not users_dir.is_dir():
        return []

    users = []
    for user_dir in sorted(users_dir.iterdir(), key=lambda item: item.name):
        if not user_dir.is_dir():
            continue
        user_id = validate_user_id(user_dir.name)
        if not user_id:
            continue
        port = detect_port(user_id)
        users.append(
            {
                "user_id": user_id,
                "status": get_container_status(user_id),
                "port": port,
                "access_url": build_access_url(port),
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
        return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, error=output[-1200:])
    return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, result=output[-1200:])


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
    return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, result="Device cache refreshed.")


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

    return redirect_to_user_dashboard(
        user_id,
        instance_mode=instance_mode,
        result=f"Uploaded {filename} to {CONTAINER_UPLOAD_DIR}/{filename}",
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
    return redirect_to_user_dashboard(user_id, instance_mode=instance_mode, result=f"Deleted {filename}.")


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
    return render_template("admin_users.html", users=list_active_users())


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
