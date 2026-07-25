import hmac
import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from werkzeug.utils import secure_filename

from executor import ControlClient, get_adapter, resolve_instance_file


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(
    os.environ.get("MANAGER_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024))
)
CONTROL = ControlClient()
TOKENS = {
    "user": os.environ.get("MANAGER_CONTROL_USER_WEB_TOKEN", "").strip(),
    "admin": os.environ.get("MANAGER_CONTROL_ADMIN_WEB_TOKEN", "").strip(),
}
DOWNLOAD_EXTENSIONS = {
    value.strip().lower()
    for value in os.environ.get(
        "MANAGER_DOWNLOAD_EXTENSIONS",
        ".md,.markdown,.txt,.pdf,.doc,.docx,.xls,.xlsx,.csv,.ppt,.pptx,.zip",
    ).split(",")
    if value.strip()
}
PROTECTED_FILENAMES = {
    value.strip().lower()
    for value in os.environ.get(
        "MANAGER_PROTECTED_FILENAMES",
        "agents.md,soul.md,tools.md,identity.md,user.md,heartbeat.md,bootstrap.md,memory.md",
    ).split(",")
    if value.strip()
}


@app.get("/health")
def health():
    return jsonify({"ok": True})


def caller_role():
    value = request.headers.get("Authorization", "")
    provided = value.split(None, 1)[1] if value.lower().startswith("bearer ") else ""
    return next(
        (role for role, token in TOKENS.items() if token and hmac.compare_digest(provided, token)),
        None,
    )


def runtime_instance(instance_public_id):
    role = caller_role()
    if role is None:
        return None, (jsonify({"error": "invalid service token"}), 401)
    actor = request.headers.get("X-Actor-User-Public-Id", "").strip()
    if not actor:
        return None, (jsonify({"error": "actor user public ID is required"}), 400)
    try:
        instance = CONTROL.get_runtime_instance(instance_public_id, actor, role == "admin")
    except RuntimeError as exc:
        return None, (jsonify({"error": str(exc)}), 403)
    return instance, None


def file_payload(path, root_key, root):
    stat = path.stat()
    return {
        "root": root_key,
        "name": path.name,
        "relative_path": path.relative_to(root).as_posix(),
        "size": stat.st_size,
        "can_delete": path.name.lower() not in PROTECTED_FILENAMES,
    }


@app.get("/internal/v1/instances/<instance_public_id>/snapshot")
def snapshot(instance_public_id):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    adapter = get_adapter(instance["product"])
    can_manage = instance.get("access_role") in {"owner", "manager", "admin"}
    files = []
    if can_manage:
        for root_key in ("workspace", "workspaces", "uploads"):
            root = resolve_instance_file(instance, root_key, ".")
            if root and root.is_dir():
                files.extend(
                    file_payload(path, root_key, root)
                    for path in sorted(root.iterdir())
                    if path.is_file() and path.suffix.lower() in DOWNLOAD_EXTENSIONS
                )
    data_path = instance.get("data_path")
    devices = Path(data_path) / "devices.txt" if data_path else None
    return jsonify(
        {
            "status": adapter.status(instance),
            "logs": adapter.logs(instance) if can_manage else "",
            "files": files,
            "devices": devices.read_text(encoding="utf-8", errors="ignore")
            if can_manage and devices and devices.is_file()
            else "No device cache found yet.",
            "can_manage": can_manage,
        }
    )


@app.post("/internal/v1/instances/<instance_public_id>/devices/<action>")
def device_action(instance_public_id, action):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    if instance.get("access_role") not in {"owner", "manager", "admin"}:
        return jsonify({"error": "device action is not allowed"}), 403
    if action not in {"approve-latest", "refresh"} or instance["product"] != "openclaw":
        return jsonify({"error": "unsupported device action"}), 400
    user_id = instance.get("legacy_user_id")
    if not user_id:
        return jsonify({"error": "legacy user ID is required"}), 409
    command = [
        str(Path(os.environ.get("OPENCLAW_MANAGER_DIR", "/opt/openclaw-manager")) / "scripts" / "approve_device.sh"),
        user_id,
        "--latest" if action == "approve-latest" else "--list-only",
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
    output = (result.stdout + "\n" + result.stderr).strip()
    return jsonify({"output": output}), 200 if result.returncode == 0 else 500


@app.post("/internal/v1/instances/<instance_public_id>/files")
def upload_file(instance_public_id):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    if instance.get("access_role") not in {"owner", "manager", "admin"}:
        return jsonify({"error": "file upload is not allowed"}), 403
    upload = request.files.get("file")
    filename = secure_filename(upload.filename) if upload else ""
    if not filename or Path(filename).suffix.lower() not in DOWNLOAD_EXTENSIONS:
        return jsonify({"error": "invalid file"}), 400
    target = resolve_instance_file(instance, "uploads", filename)
    if target is None or target.exists():
        return jsonify({"error": "file already exists"}), 409
    target.parent.mkdir(parents=True, exist_ok=True)
    upload.save(target)
    os.chmod(target, 0o644)
    return jsonify({"name": filename}), 201


@app.get("/internal/v1/instances/<instance_public_id>/files/<root_key>/<path:relative_path>")
def download_file(instance_public_id, root_key, relative_path):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    if instance.get("access_role") not in {"owner", "manager", "admin"}:
        return jsonify({"error": "file download is not allowed"}), 403
    target = resolve_instance_file(instance, root_key, relative_path)
    if target is None or not target.is_file() or target.suffix.lower() not in DOWNLOAD_EXTENSIONS:
        return jsonify({"error": "file not found"}), 404
    return send_file(target, as_attachment=True, download_name=target.name)


@app.delete("/internal/v1/instances/<instance_public_id>/files/<root_key>/<path:relative_path>")
def delete_file(instance_public_id, root_key, relative_path):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    if instance.get("access_role") not in {"owner", "manager", "admin"}:
        return jsonify({"error": "file deletion is not allowed"}), 403
    target = resolve_instance_file(instance, root_key, relative_path)
    if (
        target is None or not target.is_file() or "/" in relative_path
        or "\\" in relative_path or target.name.lower() in PROTECTED_FILENAMES
    ):
        return jsonify({"error": "file cannot be deleted"}), 400
    target.unlink()
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8083)
