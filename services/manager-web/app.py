import os
import re
import subprocess
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename


APP_DIR = Path(__file__).resolve().parent
MANAGER_DIR = Path(os.environ.get("OPENCLAW_MANAGER_DIR", "/opt/openclaw-manager"))
PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
NGINX_USERS_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")

USER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_UPLOAD_BYTES = int(os.environ.get("MANAGER_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
CONTAINER_UPLOAD_DIR = "/workspaces/uploads"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def validate_user_id(user_id):
    user_id = (user_id or "").strip()
    if not USER_ID_RE.fullmatch(user_id):
        return None
    return user_id


def get_user_dir(user_id):
    return PUBLIC_DIR / "users" / user_id


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


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/admin/users")
def admin_users():
    return render_template("admin_users.html", users=list_active_users())


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

    port = detect_port(user_id)
    return render_template(
        "user.html",
        user_id=user_id,
        port=port,
        access_url=build_access_url(port),
        status=get_container_status(user_id),
        devices_cache=read_devices_cache(user_id),
        recent_logs=get_container_logs(user_id),
        uploaded_files=list_uploaded_files(user_id),
        container_upload_dir=CONTAINER_UPLOAD_DIR,
        max_upload_mb=MAX_UPLOAD_BYTES // 1024 // 1024,
        result=request.args.get("result", ""),
        error=request.args.get("error", ""),
    )


@app.post("/users/<user_id>/approve-latest")
def approve_latest(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

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
        return redirect(url_for("user_detail", user_id=user_id, error=output[-1200:]))
    return redirect(url_for("user_detail", user_id=user_id, result=output[-1200:]))


@app.post("/users/<user_id>/refresh-devices")
def refresh_devices(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

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
        return redirect(url_for("user_detail", user_id=user_id, error=output[-1200:]))
    return redirect(url_for("user_detail", user_id=user_id, result="Device cache refreshed."))


@app.post("/users/<user_id>/upload")
def upload_file(user_id):
    user_id = validate_user_id(user_id)
    if not user_id:
        return render_template("error.html", message="Invalid user id."), 400

    user_dir = get_user_dir(user_id)
    if not user_dir.is_dir():
        return render_template("error.html", message=f"User not found: {user_id}"), 404

    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        return redirect(url_for("user_detail", user_id=user_id, error="No file selected."))

    filename = secure_filename(uploaded.filename)
    if not filename:
        return redirect(url_for("user_detail", user_id=user_id, error="Invalid filename."))

    upload_dir = get_upload_dir(user_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    target = upload_dir / filename
    if target.exists():
        return redirect(url_for("user_detail", user_id=user_id, error=f"File already exists: {filename}"))

    uploaded.save(target)
    os.chmod(target, 0o644)

    return redirect(
        url_for(
            "user_detail",
            user_id=user_id,
            result=f"Uploaded {filename} to {CONTAINER_UPLOAD_DIR}/{filename}",
        )
    )


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    app.run(host=host, port=port)
