import csv
import io
import os
import re
import secrets
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, redirect, render_template, request, url_for

import control_client
import executor_client
import web_common


app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = web_common.SESSION_SECRET or None
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.before_request(web_common.require_internal_token)
app.before_request(web_common.require_csrf)
app.context_processor(web_common.context)
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_.@/-]{1,128}$")
MAX_DEVICE_BATCH_ROWS = 100
MAX_INSTANCE_BATCH_ROWS = 100
LEGACY_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODEL_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
INSTANCE_PAGE_SIZE_OPTIONS = (10, 20, 50, 100)
DEFAULT_INSTANCE_PAGE_SIZE = 20


def configured_skill_presets():
    values = []
    for item in re.split(r"[,\n]", os.environ.get("MANAGER_SKILL_PRESETS", "")):
        item = item.strip()
        if item and SKILL_ID_RE.fullmatch(item) and item not in values:
            values.append(item)
    return values


def instance_list_context(instances):
    status_filter = request.args.get("status", "running").strip().lower()
    if status_filter not in {"running", "stopped", "deleted", "all"}:
        status_filter = "running"
    query = request.args.get("q", "").strip()
    filtered = [
        instance for instance in instances
        if (status_filter == "all")
        or (status_filter == "deleted" and instance["status"] == "deleted")
        or (instance["status"] != "deleted" and instance["runtime_status"] == status_filter)
    ]
    if query:
        needle = query.lower()
        filtered = [
            instance for instance in filtered
            if needle in " ".join(str(instance.get(key) or "") for key in (
                "instance_name", "legacy_user_id", "public_id", "product",
            )).lower()
        ]
    filtered.sort(key=lambda item: (item.get("instance_name") or "").lower())
    try:
        per_page = int(request.args.get("per_page", DEFAULT_INSTANCE_PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = DEFAULT_INSTANCE_PAGE_SIZE
    if per_page not in INSTANCE_PAGE_SIZE_OPTIONS:
        per_page = DEFAULT_INSTANCE_PAGE_SIZE
    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = min(max(page, 1), total_pages)
    start = (page - 1) * per_page
    return {
        "instances": filtered[start:start + per_page],
        "status_filter": status_filter,
        "query": query,
        "page_size_options": INSTANCE_PAGE_SIZE_OPTIONS,
        "pagination": {
            "page": page, "per_page": per_page, "total": total,
            "total_pages": total_pages, "start": start + 1 if total else 0,
            "end": min(start + per_page, total), "has_prev": page > 1,
            "has_next": page < total_pages, "prev_page": max(1, page - 1),
            "next_page": min(total_pages, page + 1),
        },
    }


def render_instances(*, instances=None, batch_result=None, result="", error="", status=200):
    current = web_common.actor()
    instances = control_client.list_admin_instances() if instances is None else instances
    active_ids = [item["public_id"] for item in instances if item["status"] != "deleted"]
    runtime_statuses = {}
    if current and active_ids:
        try:
            for start in range(0, len(active_ids), 100):
                runtime_statuses.update({
                    item["instance_public_id"]: item["status"]
                    for item in executor_client.admin_instance_statuses(
                        current["public_id"], active_ids[start:start + 100]
                    )
                })
        except executor_client.ExecutorError as exc:
            error = error or f"无法读取实例运行状态：{exc}"
    for instance in instances:
        instance["runtime_status"] = (
            "deleted" if instance["status"] == "deleted"
            else runtime_statuses.get(instance["public_id"], "unknown")
        )
    context = instance_list_context(instances)
    response = render_template(
        "admin_instances.html", **context,
        skill_presets=configured_skill_presets(), batch_result=batch_result,
        result=result, error=error,
    )
    return response if status == 200 else (response, status)


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
    return render_instances(
        result=request.args.get("result", ""), error=request.args.get("error", ""),
    )


@app.get("/admin/create-instance")
def create_instance_page():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        users = [
            user for user in control_client.list_admin_users()
            if user["status"] == "active"
        ]
        error = request.args.get("error", "")
    except control_client.ControlError as exc:
        users, error = [], str(exc)
    return render_template(
        "admin_create_instance.html", users=users, job=None, instance=None,
        batch=None, error=error,
    )


@app.post("/admin/create-instance")
def create_instance():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    owner_public_id = request.form.get("owner_user_public_id", "").strip()
    legacy_user_id = request.form.get("legacy_user_id", "").strip()
    instance_name = request.form.get("instance_name", "").strip()
    product = request.form.get("product", "openclaw").strip()
    password = request.form.get("basic_auth_password", "")
    basic_auth_enabled = request.form.get("basic_auth_enabled") == "true"
    if not owner_public_id or not LEGACY_USER_ID_RE.fullmatch(legacy_user_id):
        return redirect(url_for("create_instance_page", error="请选择 Owner 并填写有效的实例 ID。"))
    if product not in {"openclaw", "hermes"}:
        return redirect(url_for("create_instance_page", error="不支持该实例产品。"))
    if not instance_name or len(instance_name) > 128 or not password:
        return redirect(url_for("create_instance_page", error="实例名称和 Basic Auth 密码不能为空。"))
    request_id = "instance-create-" + uuid.uuid4().hex
    try:
        result = control_client.create_admin_instance(
            {
                "request_id": request_id,
                "actor_user_public_id": current["public_id"],
                "owner_user_public_id": owner_public_id,
                "legacy_user_id": legacy_user_id,
                "instance_name": instance_name,
                "product": product,
                "basic_auth_enabled": basic_auth_enabled,
                "basic_auth_password": password,
            }
        )
    except control_client.ControlError as exc:
        return redirect(url_for("create_instance_page", error=str(exc)))
    return redirect(
        url_for("create_instance_job", request_id=result["job"]["request_id"])
    )


@app.get("/admin/create-instance/<request_id>")
def create_instance_job(request_id):
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        job = control_client.get_execution_job(request_id, current["public_id"])
        if job["action"] != "instance.create":
            return render_template("error.html", message="创建任务不存在"), 404
        instance = next(
            (item for item in control_client.list_admin_instances()
             if item["public_id"] == job["instance_public_id"]),
            None,
        )
        error = ""
    except control_client.ControlError as exc:
        job, instance, error = None, None, str(exc)
    return render_template(
        "admin_create_instance.html", users=[], job=job, instance=instance,
        batch=None, error=error,
    )


@app.post("/admin/create-instance/batch")
def create_instance_batch():
    current = web_common.actor()
    upload = request.files.get("input_csv")
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    if upload is None:
        return redirect(url_for("create_instance_page", error="请选择 CSV 文件。"))
    try:
        rows = list(csv.DictReader(io.StringIO(upload.read().decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error):
        return redirect(url_for("create_instance_page", error="CSV 文件格式无效。"))
    required = {
        "owner_username", "legacy_user_id", "instance_name",
        "basic_auth_password", "basic_auth_enabled",
    }
    if not rows or len(rows) > MAX_INSTANCE_BATCH_ROWS:
        return redirect(url_for(
            "create_instance_page",
            error=f"CSV 必须包含 1-{MAX_INSTANCE_BATCH_ROWS} 行数据。",
        ))
    if set(rows[0]) != required:
        return redirect(url_for("create_instance_page", error="CSV 表头不符合要求。"))
    try:
        users = {
            user["username"].casefold(): user
            for user in control_client.list_admin_users()
            if user["status"] == "active"
        }
    except control_client.ControlError as exc:
        return redirect(url_for("create_instance_page", error=str(exc)))
    instances = []
    seen = set()
    for line_number, row in enumerate(rows, 2):
        owner = users.get((row.get("owner_username") or "").strip().casefold())
        legacy_user_id = (row.get("legacy_user_id") or "").strip()
        instance_name = (row.get("instance_name") or "").strip()
        password = row.get("basic_auth_password") or ""
        enabled = (row.get("basic_auth_enabled") or "").strip().lower()
        if owner is None:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 Owner 不存在或未启用。",
            ))
        if not LEGACY_USER_ID_RE.fullmatch(legacy_user_id) or legacy_user_id in seen:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行实例 ID 无效或重复。",
            ))
        if not instance_name or len(instance_name) > 128 or not password:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行名称或密码无效。",
            ))
        if enabled not in {"true", "false"}:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 Basic Auth 开关无效。",
            ))
        seen.add(legacy_user_id)
        instances.append({
            "owner_user_public_id": owner["public_id"],
            "legacy_user_id": legacy_user_id,
            "instance_name": instance_name,
            "product": "openclaw",
            "basic_auth_enabled": enabled == "true",
            "basic_auth_password": password,
        })
    try:
        result = control_client.create_instance_batch({
            "request_id": "instance-batch-" + uuid.uuid4().hex,
            "actor_user_public_id": current["public_id"],
            "instances": instances,
        })
    except control_client.ControlError as exc:
        return redirect(url_for("create_instance_page", error=str(exc)))
    return redirect(url_for(
        "create_instance_batch_job", request_id=result["parent"]["request_id"]
    ))


@app.get("/admin/create-instance/batch/<request_id>")
def create_instance_batch_job(request_id):
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        batch = control_client.get_instance_batch(request_id)
        error = ""
    except control_client.ControlError as exc:
        batch, error = None, str(exc)
    return render_template(
        "admin_create_instance.html", users=[], job=None, instance=None,
        batch=batch, error=error,
    )


@app.get("/admin/model-provider-batches")
def model_provider_batch_page():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    return render_template(
        "admin_model_provider_batch.html", batch=None,
        error=request.args.get("error", ""),
    )


@app.post("/admin/model-provider-batches")
def create_model_provider_batch():
    current = web_common.actor()
    upload = request.files.get("input_csv")
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    if upload is None:
        return redirect(url_for("model_provider_batch_page", error="请选择 CSV 文件。"))
    try:
        rows = list(csv.DictReader(io.StringIO(upload.read().decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error):
        return redirect(url_for("model_provider_batch_page", error="CSV 文件格式无效。"))
    expected = [
        "user_id", "model_provider_id", "model_id", "model_base_url",
        "model_api_key", "model_alias",
    ]
    if not rows or len(rows) > 100:
        return redirect(url_for(
            "model_provider_batch_page", error="CSV 必须包含 1-100 行数据。"
        ))
    if list(rows[0]) != expected:
        return redirect(url_for("model_provider_batch_page", error="CSV 表头不符合要求。"))
    try:
        instances = {
            item["legacy_user_id"]: item
            for item in control_client.list_admin_instances()
            if item.get("legacy_user_id")
            and item["status"] == "active"
            and "batch_set_model_provider" in item.get("capabilities", ())
        }
    except control_client.ControlError as exc:
        return redirect(url_for("model_provider_batch_page", error=str(exc)))
    payload_rows = []
    seen = set()
    for line_number, row in enumerate(rows, 2):
        user_id = (row.get("user_id") or "").strip()
        provider_id = (row.get("model_provider_id") or "").strip()
        model_id = (row.get("model_id") or "").strip()
        base_url = (row.get("model_base_url") or "").strip()
        alias = (row.get("model_alias") or "").strip() or model_id
        instance = instances.get(user_id)
        if instance is None or instance["public_id"] in seen:
            return redirect(url_for(
                "model_provider_batch_page",
                error=f"第 {line_number} 行实例不存在、未启用或重复。",
            ))
        if not MODEL_PROVIDER_ID_RE.fullmatch(provider_id):
            return redirect(url_for(
                "model_provider_batch_page", error=f"第 {line_number} 行供应商 ID 无效。"
            ))
        if not MODEL_ID_RE.fullmatch(model_id) or len(alias) > 128:
            return redirect(url_for(
                "model_provider_batch_page", error=f"第 {line_number} 行模型配置无效。"
            ))
        if base_url:
            parsed = urllib.parse.urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return redirect(url_for(
                    "model_provider_batch_page", error=f"第 {line_number} 行模型地址无效。"
                ))
        seen.add(instance["public_id"])
        payload_rows.append({
            "instance_public_id": instance["public_id"],
            "model_provider_id": provider_id,
            "model_id": model_id,
            "model_base_url": base_url,
            "model_alias": alias,
        })
    try:
        result = control_client.create_model_provider_batch({
            "request_id": "model-provider-batch-" + uuid.uuid4().hex,
            "actor_user_public_id": current["public_id"],
            "instances": payload_rows,
        })
    except control_client.ControlError as exc:
        return redirect(url_for("model_provider_batch_page", error=str(exc)))
    return redirect(url_for(
        "model_provider_batch_job", request_id=result["parent"]["request_id"]
    ))


@app.get("/admin/model-provider-batches/<request_id>")
def model_provider_batch_job(request_id):
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        batch = control_client.get_model_provider_batch(request_id)
        error = ""
    except control_client.ControlError as exc:
        batch, error = None, str(exc)
    return render_template(
        "admin_model_provider_batch.html", batch=batch, error=error
    )


@app.post("/admin/action-batches")
def run_action_batch():
    current = web_common.actor()
    action = request.form.get("action", "")
    instance_public_ids = request.form.getlist("instance_public_ids")
    skill_id = request.form.get("skill_id", "").strip()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    if action not in {"start", "stop", "restart", "install_skill"}:
        return render_instances(instances=[], error="无效的批量操作。", status=400)
    if not instance_public_ids or len(instance_public_ids) > 100 or len(set(instance_public_ids)) != len(instance_public_ids):
        return render_instances(error="请选择 1-100 个不同实例。", status=400)
    payload = {
        "request_id": "action-batch-" + uuid.uuid4().hex,
        "actor_user_public_id": current["public_id"],
        "action": action,
        "instance_public_ids": instance_public_ids,
    }
    if action == "install_skill":
        if skill_id not in configured_skill_presets():
            return render_instances(error="请选择已配置的 Skill。", status=400)
        payload["skill_id"] = skill_id
    try:
        result = control_client.create_action_batch(payload)
    except control_client.ControlError as exc:
        return redirect(url_for("instances", error=str(exc)))
    return redirect(url_for("action_batch", request_id=result["parent"]["request_id"]))


@app.get("/admin/action-batches/<request_id>")
def action_batch(request_id):
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        result = control_client.get_action_batch(request_id)
    except control_client.ControlError as exc:
        return redirect(url_for("instances", error=str(exc)))
    return render_instances(batch_result=result)


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


@app.get("/admin/device-approvals")
def device_approvals():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    return render_template("admin_device_approvals.html", result=None, error="")


@app.post("/admin/device-approvals")
def run_device_approvals():
    current = web_common.actor()
    action = request.form.get("action", "")
    upload = request.files.get("input_csv")
    if not current or current["role"] != "admin" or action not in {"preview", "approve"}:
        return render_template("error.html", message="Forbidden"), 403
    if upload is None:
        return render_template(
            "admin_device_approvals.html", result=None, error="请选择 CSV 文件。"
        ), 400
    try:
        rows = list(csv.DictReader(io.StringIO(upload.read().decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error):
        return render_template(
            "admin_device_approvals.html", result=None, error="CSV 文件格式无效。"
        ), 400
    if not rows or len(rows) > MAX_DEVICE_BATCH_ROWS:
        return render_template(
            "admin_device_approvals.html", result=None,
            error=f"CSV 必须包含 1-{MAX_DEVICE_BATCH_ROWS} 行数据。",
        ), 400
    for line_number, row in enumerate(rows, 2):
        if (row.get("request_id") or "").strip():
            return render_template(
                "admin_device_approvals.html", result=None,
                error=f"第 {line_number} 行包含 request_id；新版批量审批仅支持审批最新请求。",
            ), 400

    instances = control_client.list_admin_instances()
    by_public_id = {item["public_id"]: item for item in instances}
    by_legacy_id = {
        item["legacy_user_id"]: item for item in instances if item.get("legacy_user_id")
    }
    instance_ids = []
    seen = set()
    for line_number, row in enumerate(rows, 2):
        public_id = (row.get("instance_public_id") or "").strip()
        legacy_id = (row.get("user_id") or "").strip()
        instance = by_public_id.get(public_id) if public_id else by_legacy_id.get(legacy_id)
        if instance is None:
            return render_template(
                "admin_device_approvals.html", result=None,
                error=f"第 {line_number} 行实例不存在或标识无效。",
            ), 400
        if instance["public_id"] not in seen:
            seen.add(instance["public_id"])
            instance_ids.append(instance["public_id"])

    try:
        result = control_client.create_device_batch(
            {
                "request_id": "device-batch-" + uuid.uuid4().hex,
                "actor_user_public_id": current["public_id"],
                "action": action,
                "instance_public_ids": instance_ids,
            }
        )
    except control_client.ControlError as exc:
        return render_template(
            "admin_device_approvals.html", result=None, error=str(exc)
        ), exc.status
    return redirect(url_for("device_batch", request_id=result["parent"]["request_id"]))


@app.get("/admin/device-approvals/<request_id>")
def device_batch(request_id):
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        result = control_client.get_device_batch(request_id)
    except control_client.ControlError as exc:
        return render_template(
            "admin_device_approvals.html", result=None, error=str(exc)
        ), exc.status
    return render_template("admin_device_approvals.html", result=result, error="")


@app.get("/admin/device-approvals/<request_id>.csv")
def download_device_batch(request_id):
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        result = control_client.get_device_batch(request_id)
    except control_client.ControlError as exc:
        return render_template("error.html", message=str(exc)), exc.status
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["instance_public_id", "request_id", "status", "message"])
    for job in result["children"]:
        writer.writerow(
            [job["instance_public_id"], job["request_id"], job["status"], job["summary"]]
        )
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{request_id}.csv"'},
    )


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
