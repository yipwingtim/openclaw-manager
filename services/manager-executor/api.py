import hmac
import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from werkzeug.utils import secure_filename

from executor import ControlClient, get_adapter, resolve_instance_file
from product_capabilities import product_supports


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


def require_product_capability(instance, capability):
    if product_supports(instance.get("product"), capability):
        return None
    label = capability.replace("_", " ")
    return jsonify({"error": f"instance product does not support {label}"}), 400


def _atomic_upload(upload, target):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise RuntimeError("secure atomic upload is not supported")
    root_fd = _open_directory_chain(target.parent)
    fd = None
    created = False
    try:
        fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=root_fd)
        created = True
        with os.fdopen(fd, "wb") as output:
            fd = None
            upload.save(output)
        return True
    except Exception:
        if fd is not None:
            os.close(fd)
        if created:
            try:
                os.unlink(target.name, dir_fd=root_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(root_fd)


def _atomic_download(target):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        return None
    root_fd = _open_directory_chain(target.parent)
    fd = None
    try:
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if actual != target:
            os.close(fd)
            fd = None
            return None
        return os.fdopen(fd, "rb")
    except (OSError, ValueError):
        if fd is not None:
            os.close(fd)
        return None
    finally:
        os.close(root_fd)


def _open_directory_chain(path):
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


@app.get("/internal/v1/instances/<instance_public_id>/snapshot")
def snapshot(instance_public_id):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    unsupported = require_product_capability(instance, "status")
    if unsupported:
        return unsupported
    adapter = get_adapter(instance["product"])
    can_manage = instance.get("access_role") in {"owner", "manager", "admin"}
    files = []
    if can_manage and adapter.supports("file_download"):
        for root_key in ("workspace", "workspaces", "uploads"):
            root = resolve_instance_file(instance, root_key, ".")
            if root and root.is_dir():
                files.extend(
                    file_payload(path, root_key, root)
                    for path in sorted(root.iterdir())
                    if path.is_file() and path.suffix.lower() in DOWNLOAD_EXTENSIONS
                )
    data_path = instance.get("data_path")
    devices = (
        Path(data_path) / "devices.txt"
        if data_path and adapter.supports("device_pairing")
        else None
    )
    return jsonify(
        {
            "status": adapter.status(instance),
            "logs": adapter.logs(instance)
            if can_manage and adapter.supports("logs")
            else "",
            "files": files,
            "devices": devices.read_text(encoding="utf-8", errors="ignore")
            if can_manage and devices and devices.is_file()
            else "No device cache found yet.",
            "can_manage": can_manage,
            "download_extensions": ", ".join(sorted(DOWNLOAD_EXTENSIONS)),
            "protected_filenames": ", ".join(sorted(PROTECTED_FILENAMES)),
            "upload_dir": "uploads",
            "max_upload_bytes": app.config["MAX_CONTENT_LENGTH"],
        }
    )


@app.post("/internal/v1/admin/instance-statuses")
def admin_instance_statuses():
    if caller_role() != "admin":
        return jsonify({"error": "admin service token is required"}), 403
    actor = request.headers.get("X-Actor-User-Public-Id", "").strip()
    instance_ids = (request.get_json(silent=True) or {}).get("instance_public_ids")
    if not actor:
        return jsonify({"error": "actor user public ID is required"}), 400
    if (
        not isinstance(instance_ids, list) or not 1 <= len(instance_ids) <= 100
        or any(not isinstance(value, str) or not value for value in instance_ids)
        or len(set(instance_ids)) != len(instance_ids)
    ):
        return jsonify({"error": "instance_public_ids must contain 1-100 unique IDs"}), 400
    statuses = []
    for instance_id in instance_ids:
        try:
            instance = CONTROL.get_runtime_instance(instance_id, actor, True)
            raw = get_adapter(instance["product"]).status(instance)
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError):
            raw = "UNKNOWN"
        status = "running" if raw.startswith("Up") else "stopped" if raw == "STOPPED" else "unknown"
        statuses.append({"instance_public_id": instance_id, "status": status})
    return jsonify({"statuses": statuses})


@app.post("/internal/v1/instances/<instance_public_id>/devices/<action>")
def device_action(instance_public_id, action):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    if instance.get("access_role") not in {"owner", "manager", "admin"}:
        return jsonify({"error": "device action is not allowed"}), 403
    if action not in {"approve-latest", "refresh"}:
        return jsonify({"error": "unsupported device action"}), 400
    unsupported = require_product_capability(instance, "device_pairing")
    if unsupported:
        return unsupported
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
    unsupported = require_product_capability(instance, "file_upload")
    if unsupported:
        return unsupported
    upload = request.files.get("file")
    filename = secure_filename(upload.filename) if upload else ""
    if not filename or Path(filename).suffix.lower() not in DOWNLOAD_EXTENSIONS:
        return jsonify({"error": "invalid file"}), 400
    target = resolve_instance_file(instance, "uploads", filename)
    if target is None or target.exists():
        return jsonify({"error": "file already exists"}), 409
    if not target.parent.is_dir():
        return jsonify({"error": "upload directory not found"}), 409
    try:
        _atomic_upload(upload, target)
    except FileExistsError:
        return jsonify({"error": "file already exists"}), 409
    except (OSError, RuntimeError, ValueError):
        return jsonify({"error": "failed to save file"}), 500
    return jsonify({"name": filename}), 201


@app.get("/internal/v1/instances/<instance_public_id>/files/<root_key>/<path:relative_path>")
def download_file(instance_public_id, root_key, relative_path):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    if instance.get("access_role") not in {"owner", "manager", "admin"}:
        return jsonify({"error": "file download is not allowed"}), 403
    unsupported = require_product_capability(instance, "file_download")
    if unsupported:
        return unsupported
    target = resolve_instance_file(instance, root_key, relative_path)
    if target is None or not target.is_file() or target.suffix.lower() not in DOWNLOAD_EXTENSIONS:
        return jsonify({"error": "file not found"}), 404
    opened = _atomic_download(target)
    if opened is None:
        return jsonify({"error": "file not found"}), 404
    return send_file(opened, as_attachment=True, download_name=target.name)


@app.delete("/internal/v1/instances/<instance_public_id>/files/<root_key>/<path:relative_path>")
def delete_file(instance_public_id, root_key, relative_path):
    instance, error = runtime_instance(instance_public_id)
    if error:
        return error
    if instance.get("access_role") not in {"owner", "manager", "admin"}:
        return jsonify({"error": "file deletion is not allowed"}), 403
    unsupported = require_product_capability(instance, "file_delete")
    if unsupported:
        return unsupported
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
