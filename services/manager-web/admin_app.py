import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, redirect, render_template, request, url_for

import control_client
import web_common


app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = web_common.SESSION_SECRET or None
app.before_request(web_common.require_internal_token)
app.before_request(web_common.require_csrf)
app.context_processor(web_common.context)
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_.@/-]{1,128}$")


def configured_skill_presets():
    values = []
    for item in re.split(r"[,\n]", os.environ.get("MANAGER_SKILL_PRESETS", "")):
        item = item.strip()
        if item and SKILL_ID_RE.fullmatch(item) and item not in values:
            values.append(item)
    return values


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/admin/login")
def login():
    if web_common.external_auth_enabled():
        return redirect("/login")
    return web_common.login_page(app, "/admin/login")


@app.post("/admin/login")
def login_submit():
    return web_common.local_login(app, "/admin/login")


@app.get("/emergency/login")
def emergency_login():
    username = request.headers.get("X-Remote-User", "").strip()
    allowed = {
        value.strip()
        for value in os.environ.get("MANAGER_EMERGENCY_USERS", "").split(",")
        if value.strip()
    }
    if not web_common.external_auth_enabled() or username not in allowed:
        return render_template("error.html", message="Forbidden"), 403
    raw_token = secrets.token_urlsafe(48)
    control_client.emergency_login(
        {
            "username": username,
            "provider": web_common.AUTH_PROVIDER,
            "token_hash": web_common.token_hash(raw_token),
            "csrf_token": secrets.token_urlsafe(32),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=web_common.SESSION_HOURS)
            ).replace(microsecond=0).isoformat(),
        }
    )
    response = app.make_response(redirect(url_for("index")))
    response.set_cookie(
        web_common.COOKIE_NAME, raw_token, secure=web_common.COOKIE_SECURE,
        httponly=True, samesite="Lax", max_age=web_common.SESSION_HOURS * 3600,
    )
    return response


@app.post("/logout")
def logout():
    return web_common.logout()


@app.get("/")
@app.get("/admin")
@app.get("/admin/")
def index():
    return redirect(url_for("instances")) if web_common.actor() else redirect(url_for("login"))


@app.get("/admin/instances")
def instances():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    return render_template(
        "admin_instances.html", instances=control_client.list_admin_instances(),
        skill_presets=configured_skill_presets(),
        result=request.args.get("result", ""), error=request.args.get("error", ""),
    )


@app.get("/admin/metadata")
def metadata():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        summary = control_client.get_admin_metadata()
        error = ""
    except control_client.ControlError as exc:
        summary = {"counts": {}, "instances": [], "operations": []}
        error = str(exc)
    return render_template("admin_metadata.html", error=error, **summary)


@app.post("/admin/instances/<instance_public_id>/lifecycle")
def lifecycle(instance_public_id):
    current = web_common.actor()
    action = request.form.get("action", "")
    if not current or current["role"] != "admin" or action not in {"start", "stop", "restart"}:
        return render_template("error.html", message="Forbidden"), 403
    control_client.create_execution_job(
        {
            "request_id": str(uuid.uuid4()),
            "actor_user_public_id": current["public_id"],
            "instance_public_id": instance_public_id,
            "action": f"instance.{action}",
            "params": {},
        }
    )
    return redirect(url_for("instances", result=f"{action} task queued"))


@app.post("/admin/instances/<instance_public_id>/retention")
def retention(instance_public_id):
    current = web_common.actor()
    action = request.form.get("action", "")
    if not current or current["role"] != "admin" or action not in {"delete", "restore"}:
        return render_template("error.html", message="Forbidden"), 403
    try:
        control_client.create_execution_job(
            {
                "request_id": str(uuid.uuid4()),
                "actor_user_public_id": current["public_id"],
                "instance_public_id": instance_public_id,
                "action": f"instance.{action}",
                "params": {},
            }
        )
    except control_client.ControlError as exc:
        return redirect(url_for("instances", error=str(exc)))
    return redirect(url_for("instances", result=f"{action} task queued"))


@app.post("/admin/instances/<instance_public_id>/basic-auth")
def basic_auth(instance_public_id):
    current = web_common.actor()
    enabled = request.form.get("enabled", "")
    if (
        not current
        or current["role"] != "admin"
        or enabled not in {"true", "false"}
    ):
        return render_template("error.html", message="Forbidden"), 403
    try:
        control_client.create_execution_job(
            {
                "request_id": str(uuid.uuid4()),
                "actor_user_public_id": current["public_id"],
                "instance_public_id": instance_public_id,
                "action": "instance.set_basic_auth",
                "params": {"enabled": enabled == "true"},
            }
        )
    except control_client.ControlError as exc:
        return redirect(url_for("instances", error=str(exc)))
    state = "enable" if enabled == "true" else "disable"
    return redirect(url_for("instances", result=f"Basic Auth {state} task queued"))


@app.post("/admin/instances/<instance_public_id>/version")
def update_version(instance_public_id):
    current = web_common.actor()
    version = request.form.get("version", "").strip()
    restore_model_provider = request.form.get("restore_model_provider") == "true"
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        control_client.create_execution_job(
            {
                "request_id": str(uuid.uuid4()),
                "actor_user_public_id": current["public_id"],
                "instance_public_id": instance_public_id,
                "action": "instance.update_version",
                "params": {
                    "version": version,
                    "restore_model_provider": restore_model_provider,
                },
            }
        )
    except control_client.ControlError as exc:
        return redirect(url_for("instances", error=str(exc)))
    return redirect(url_for("instances", result=f"Version update queued: {version}"))


@app.post("/admin/instances/<instance_public_id>/skill")
def install_skill(instance_public_id):
    current = web_common.actor()
    skill_id = request.form.get("skill_id", "").strip()
    if not current or current["role"] != "admin" or not skill_id:
        return render_template("error.html", message="Forbidden"), 403
    try:
        control_client.create_execution_job(
            {
                "request_id": str(uuid.uuid4()),
                "actor_user_public_id": current["public_id"],
                "instance_public_id": instance_public_id,
                "action": "instance.install_skill",
                "params": {"skill_id": skill_id},
            }
        )
    except control_client.ControlError as exc:
        return redirect(url_for("instances", error=str(exc)))
    return redirect(url_for("instances", result=f"Skill install queued: {skill_id}"))


@app.post("/admin/instances/<instance_public_id>/devices")
def devices(instance_public_id):
    current = web_common.actor()
    action = request.form.get("action", "")
    if (
        not current
        or current["role"] != "admin"
        or action not in {"refresh", "approve_latest"}
    ):
        return render_template("error.html", message="Forbidden"), 403
    execution_action = (
        "instance.refresh_devices"
        if action == "refresh"
        else "instance.approve_latest_device"
    )
    try:
        control_client.create_execution_job(
            {
                "request_id": str(uuid.uuid4()),
                "actor_user_public_id": current["public_id"],
                "instance_public_id": instance_public_id,
                "action": execution_action,
                "params": {},
            }
        )
    except control_client.ControlError as exc:
        return redirect(url_for("instances", error=str(exc)))
    return redirect(url_for("instances", result=f"Device {action} task queued"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
