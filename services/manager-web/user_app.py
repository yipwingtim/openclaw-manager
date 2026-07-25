from flask import Flask, Response, redirect, render_template, request, url_for

import control_client
import executor_client
import web_common


app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = web_common.SESSION_SECRET or None
app.before_request(web_common.require_internal_token)
app.before_request(web_common.require_csrf)
app.context_processor(web_common.context)


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
    return render_template("my_instances.html", instances=instances, current_user=current["username"])


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
    return render_template(
        "instance_portal.html", instance=instance, members=members,
        snapshot=snapshot,
        current_user=current["username"], result=request.args.get("result", ""),
        error=request.args.get("error", ""),
    )


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
    result = executor_client.device_action(current["public_id"], instance_public_id, action)
    return redirect(url_for("instance_detail", instance_public_id=instance_public_id, result=result.get("output", "")))


@app.post("/instances/<instance_public_id>/files")
def upload_file(instance_public_id):
    current = web_common.actor()
    executor_client.upload(current["public_id"], instance_public_id, request.files.get("file"))
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
