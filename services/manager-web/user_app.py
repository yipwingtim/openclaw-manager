import re
import urllib.parse

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

import control_client
import executor_client
import web_common


app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = web_common.SESSION_SECRET or None
app.before_request(web_common.require_internal_token)
app.before_request(web_common.require_csrf)
app.context_processor(web_common.context)

PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
OAUTH_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,512}$")


def oauth_error(error, status):
    response = jsonify({"error": error})
    response.headers["Cache-Control"] = "no-store"
    return response, status


def forwarded_https():
    return request.headers.get("X-Forwarded-Proto", "").lower() == "https"


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login")
def login():
    return web_common.login_page(app)


@app.post("/login")
def login_submit():
    return web_common.local_login(app)


@app.get("/auth/callback")
def auth_callback():
    return web_common.external_callback(app)


@app.get("/auth/uis/login")
def uis_login():
    if not web_common.external_auth_enabled():
        return render_template("error.html", message="External login is disabled."), 404
    client, config = web_common.external_client(app)
    return client.authorize_redirect(config["redirect_uri"])


@app.get("/auth/uis/logout")
def uis_logout_callback():
    return web_common.external_logout_callback()


@app.get("/auth/hermes/authorize")
def hermes_authorize():
    values = {key: request.args.get(key, "") for key in (
        "response_type", "client_id", "redirect_uri", "state",
        "code_challenge", "code_challenge_method",
    )}
    if (
        not forwarded_https()
        or values["response_type"] != "code"
        or values["code_challenge_method"] != "S256"
        or not PKCE_CHALLENGE_RE.fullmatch(values["code_challenge"])
        or any(not OAUTH_VALUE_RE.fullmatch(values[key]) for key in (
            "client_id", "redirect_uri", "state",
        ))
    ):
        return oauth_error("invalid_request", 400)
    raw_session = request.cookies.get(web_common.COOKIE_NAME, "")
    if not raw_session or not web_common.actor():
        response = app.make_response(redirect(url_for("login")))
        response.set_cookie(
            web_common.HERMES_RETURN_COOKIE,
            request.full_path.rstrip("?"),
            secure=web_common.COOKIE_SECURE, httponly=True,
            samesite="Lax", max_age=600,
        )
        return response
    try:
        result = control_client.authorize_hermes({
            "client_id": values["client_id"],
            "redirect_uri": values["redirect_uri"],
            "code_challenge": values["code_challenge"],
            "session_hash": web_common.token_hash(raw_session),
        })
    except control_client.ControlError as exc:
        return oauth_error(
            "access_denied" if exc.status == 403 else "temporarily_unavailable",
            exc.status if exc.status in {403, 503} else 400,
        )
    separator = "&" if urllib.parse.urlsplit(values["redirect_uri"]).query else "?"
    query = urllib.parse.urlencode({"code": result["code"], "state": values["state"]})
    response = redirect(f'{values["redirect_uri"]}{separator}{query}')
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/auth/hermes/token")
def hermes_token():
    if not forwarded_https() or request.mimetype != "application/x-www-form-urlencoded":
        return oauth_error("invalid_request", 400)
    if request.form.get("grant_type") != "authorization_code":
        return oauth_error("unsupported_grant_type", 400)
    required = ("code", "client_id", "client_secret", "redirect_uri", "code_verifier")
    if any(not request.form.get(key) or len(request.form[key]) > 2048 for key in required):
        return oauth_error("invalid_request", 400)
    try:
        result = control_client.redeem_hermes({key: request.form[key] for key in required})
    except control_client.ControlError as exc:
        return oauth_error(
            "temporarily_unavailable" if exc.status == 503 else
            "invalid_client" if exc.status == 401 else "invalid_grant",
            503 if exc.status == 503 else 401 if exc.status == 401 else 400,
        )
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/hermes/jwks.json")
def hermes_jwks():
    try:
        response = jsonify(control_client.hermes_jwks())
    except control_client.ControlError:
        return oauth_error("temporarily_unavailable", 503)
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.post("/logout")
def logout():
    return web_common.logout()


@app.get("/")
def index():
    return redirect(url_for("my_instances")) if web_common.actor() else redirect(url_for("login"))


@app.get("/me")
def my_instances():
    current = web_common.actor()
    if not current:
        return render_template("error.html", message="Forbidden"), 403
    instances = control_client.list_instances(current["public_id"])
    for instance in instances:
        instance["allowed_actions"] = ["access", "status"]
    return render_template("my_instances.html", instances=instances)


@app.get("/instance-admin")
@app.get("/instance-admin/")
def legacy_instance_admin():
    current = web_common.actor()
    legacy_user_id = request.headers.get("X-OpenClaw-User", "").strip()
    if not current or not legacy_user_id:
        return render_template("error.html", message="Forbidden"), 403
    instance = next(
        (
            item
            for item in control_client.list_instances(current["public_id"])
            if item.get("legacy_user_id") == legacy_user_id
        ),
        None,
    )
    if instance is None:
        return render_template("error.html", message="Instance not found"), 404
    return redirect(url_for("instance_detail", instance_public_id=instance["public_id"]))


@app.get("/instances/<instance_public_id>")
def instance_detail(instance_public_id):
    current = web_common.actor()
    if not current:
        return render_template("error.html", message="Forbidden"), 403
    instance = control_client.get_instance(current["public_id"], instance_public_id)
    members = (
        control_client.list_members(current["public_id"], instance_public_id)
        if instance.get("access_role") in {"owner", "manager"}
        else []
    )
    snapshot = executor_client.snapshot(current["public_id"], instance_public_id)
    job = None
    job_id = request.args.get("job", "")
    try:
        if job_id:
            job = control_client.get_execution_job(job_id, current["public_id"])
        else:
            job = control_client.get_wechat_bind_job(
                current["public_id"], instance_public_id
            )
    except control_client.ControlError:
        job = None
    files = [
        {
            **item,
            "root_label": item["root"],
            "container_path": f'{item["root"]}/{item["relative_path"]}',
        }
        for item in snapshot["files"]
    ]
    wechat_job = None
    wechat_url = ""
    if job:
        status = job["status"]
        if status == "queued":
            status = "starting"
        elif status == "succeeded":
            status = "success"
        elif status == "running" and (job.get("output") or "").startswith(
            "https://liteapp.weixin.qq.com/q/"
        ):
            status = "waiting_confirmation"
            wechat_url = job["output"]
        wechat_job = {
            "request_id": job["request_id"],
            "status": status,
            "error": job.get("error_summary") or "",
        }
    return render_template(
        "user.html",
        instance_name=instance["instance_name"],
        instance_public_id=instance["public_id"],
        instance_mode=False,
        show_admin_links=False,
        status=snapshot["status"],
        port=instance.get("port"),
        access_url=instance.get("access_url"),
        allowed_actions=["access"],
        result=request.args.get("result", ""),
        error=request.args.get("error", ""),
        can_manage=snapshot["can_manage"],
        can_device_pairing=snapshot["can_manage"] and instance["product"] == "openclaw",
        can_file_upload=snapshot["can_manage"],
        can_file_download=snapshot["can_manage"],
        can_file_delete=snapshot["can_manage"],
        can_view_logs=snapshot["can_manage"],
        can_manage_members=instance["access_role"] in {"owner", "manager"},
        members=members,
        approve_url=url_for("device_action", instance_public_id=instance_public_id, action="approve-latest"),
        refresh_url=url_for("device_action", instance_public_id=instance_public_id, action="refresh"),
        wechat_bind_url=url_for("wechat_bind", instance_public_id=instance_public_id),
        wechat_bind_cancel_url=(
            url_for("cancel_wechat_bind", instance_public_id=instance_public_id, job_id=job["request_id"])
            if job and job["status"] in {"queued", "running"}
            else ""
        ),
        wechat_bind_timeout_minutes=5,
        wechat_url=wechat_url,
        wechat_bind_job=wechat_job,
        upload_url=url_for("upload_file", instance_public_id=instance_public_id),
        uploaded_files=[item for item in files if item["root"] == "uploads"],
        downloadable_files=files,
        download_endpoint="download_file",
        delete_endpoint="delete_file",
        download_extensions=snapshot["download_extensions"],
        protected_filenames=snapshot["protected_filenames"],
        container_upload_dir=snapshot["upload_dir"],
        max_upload_mb=snapshot["max_upload_bytes"] // 1024 // 1024,
        devices_cache=snapshot["devices"],
        recent_logs=snapshot["logs"],
        back_url=url_for("my_instances"),
    )


@app.get("/instances/<instance_public_id>/open")
def open_instance(instance_public_id):
    current = web_common.actor()
    if not current:
        return redirect(url_for("login", instance=instance_public_id))
    try:
        instance = control_client.get_instance_entry(
            current["public_id"], instance_public_id
        )
    except control_client.ControlError:
        return render_template("error.html", message="Forbidden"), 403
    if not instance.get("access_url"):
        return render_template(
            "error.html", message="Instance access URL is unavailable."
        ), 404
    return redirect(instance["access_url"])


@app.post("/instances/<instance_public_id>/members")
def add_member(instance_public_id):
    current = web_common.actor()
    control_client.add_member(
        current["public_id"], instance_public_id,
        request.form.get("username", ""), request.form.get("role", ""),
    )
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, result="实例成员已保存。"))


@app.post("/instances/<instance_public_id>/members/<member_public_id>/delete")
def remove_member(instance_public_id, member_public_id):
    current = web_common.actor()
    control_client.remove_member(current["public_id"], instance_public_id, member_public_id)
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, result="实例成员已移除。"))


@app.post("/instances/<instance_public_id>/devices/<action>")
def device_action(instance_public_id, action):
    current = web_common.actor()
    if action not in {"approve-latest", "refresh"}:
        return render_template("error.html", message="Invalid action"), 400
    try:
        executor_client.device_action(current["public_id"], instance_public_id, action)
    except executor_client.ExecutorError:
        message = "Device approval failed." if action == "approve-latest" else "Device refresh failed."
        return redirect(url_for("instance_detail", instance_public_id=instance_public_id, error=message))
    message = "Device approval command completed." if action == "approve-latest" else "Device cache refreshed."
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, result=message))


@app.post("/instances/<instance_public_id>/wechat-bind")
def wechat_bind(instance_public_id):
    current = web_common.actor()
    if not current:
        return render_template("error.html", message="Forbidden"), 403
    try:
        job = control_client.create_wechat_bind(current["public_id"], instance_public_id)
    except control_client.ControlError as exc:
        return redirect(url_for("instance_detail", instance_public_id=instance_public_id, error=str(exc)))
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, job=job["request_id"]))


@app.post("/instances/<instance_public_id>/wechat-bind/<job_id>/cancel")
def cancel_wechat_bind(instance_public_id, job_id):
    current = web_common.actor()
    if not current:
        return render_template("error.html", message="Forbidden"), 403
    try:
        control_client.cancel_execution_job(job_id, current["public_id"])
    except control_client.ControlError as exc:
        return redirect(url_for("instance_detail", instance_public_id=instance_public_id, error=str(exc)))
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, result="微信绑定任务已取消。"))


@app.post("/instances/<instance_public_id>/files")
def upload_file(instance_public_id):
    current = web_common.actor()
    try:
        executor_client.upload(current["public_id"], instance_public_id, request.files.get("file"))
    except executor_client.ExecutorError as exc:
        return render_template("error.html", message=str(exc)), exc.status_code or 502
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, result="File uploaded"))


@app.get("/instances/<instance_public_id>/files/<root_key>/<path:relative_path>")
def download_file(instance_public_id, root_key, relative_path):
    current = web_common.actor()
    upstream = executor_client.download(
        current["public_id"], instance_public_id, root_key, relative_path
    )
    return Response(
        upstream.iter_content(64 * 1024),
        headers={
            "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
            "Content-Disposition": upstream.headers.get("Content-Disposition", "attachment"),
        },
    )


@app.post("/instances/<instance_public_id>/files/<root_key>/<path:relative_path>/delete")
def delete_file(instance_public_id, root_key, relative_path):
    current = web_common.actor()
    executor_client.delete(
        current["public_id"], instance_public_id, root_key, relative_path
    )
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, result="File deleted"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
