import csv
import io
import os
import re
import secrets
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
MAX_PLATFORM_USER_IMPORT_ROWS = 100
LEGACY_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODEL_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
INSTANCE_PAGE_SIZE_OPTIONS = (10, 20, 50, 100)
DEFAULT_INSTANCE_PAGE_SIZE = 20
VERSION_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:-]{0,255}|sha256:[0-9a-fA-F]{64})$")
BATCH_VERSION_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,63}|sha256:[0-9a-fA-F]{64})$")
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def default_instance_version(product):
    if product == "openclaw":
        return os.environ.get("OPENCLAW_VERSION", "").strip()
    if product == "hermes":
        return os.environ.get("HERMES_VERSION", "v2026.7.20").strip()
    image = os.environ.get("EVOSCIENTIST_IMAGE", "").strip()
    return image.rsplit("@", 1)[-1] if "@" in image else image.rsplit(":", 1)[-1]


def load_default_versions():
    try:
        return control_client.get_default_versions()
    except (control_client.ControlError, AttributeError):
        return {product: default_instance_version(product) for product in ("openclaw", "hermes", "evoscientist")}


def configured_skill_presets():
    values = []
    for item in re.split(r"[,\n]", os.environ.get("MANAGER_SKILL_PRESETS", "")):
        item = item.strip()
        if item and SKILL_ID_RE.fullmatch(item) and item not in values:
            values.append(item)
    return values


def activity_metric_items(metrics):
    items = []
    for name, value in sorted(metrics.items()):
        if name in {"last_activity_at_ms", "last_activity_at_s"} and value:
            seconds = value / 1000 if name.endswith("_ms") else value
            try:
                value = datetime.fromtimestamp(seconds, DISPLAY_TIMEZONE).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (OverflowError, OSError, TypeError, ValueError):
                pass
        items.append((name, value))
    return items


def instance_list_context(instances):
    status_filter = request.args.get("status", "running").strip().lower()
    if status_filter not in {"running", "stopped", "deleted", "all"}:
        status_filter = "running"
    product_filter = request.args.get("product", "all").strip().lower()
    if product_filter not in {"all", "openclaw", "hermes", "evoscientist"}:
        product_filter = "all"
    query = request.args.get("q", "").strip()
    filtered = [
        instance for instance in instances
        if (product_filter == "all" or instance.get("product") == product_filter)
        and (
            (status_filter == "all")
            or (status_filter == "deleted" and instance["status"] == "deleted")
            or (instance["status"] != "deleted" and instance["runtime_status"] == status_filter)
        )
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
        "product_filter": product_filter,
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
            if exc.status_code is not None:
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
    if web_common.external_auth_enabled() and not web_common.local_auth_enabled():
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
    error = request.args.get("error", "")
    return render_template(
        "admin_create_instance.html", job=None, instance=None,
        batch=None, error=error,
        default_versions=load_default_versions(),
    )


@app.post("/admin/create-instance")
def create_instance():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    owner_identity_type = request.form.get("owner_identity_type", "").strip()
    owner_identity = request.form.get("owner_identity", "").strip()
    legacy_user_id = request.form.get("legacy_user_id", "").strip()
    instance_name = request.form.get("instance_name", "").strip()
    product = request.form.get("product", "openclaw").strip()
    password = request.form.get("basic_auth_password", "")
    basic_auth_enabled = request.form.get("basic_auth_enabled") == "true"
    default_versions = load_default_versions()
    version = request.form.get("version", "").strip() or default_versions.get(product, "")
    confirm_latest = request.form.get("confirm_latest") == "true"
    if owner_identity_type not in {"local", "campus-uis"} or not owner_identity:
        return redirect(url_for("create_instance_page", error="请选择 Owner 身份类型并填写身份标识。"))
    if not LEGACY_USER_ID_RE.fullmatch(legacy_user_id):
        return redirect(url_for("create_instance_page", error="请填写有效的实例 ID。"))
    if product not in {"openclaw", "hermes", "evoscientist"}:
        return redirect(url_for("create_instance_page", error="不支持该实例产品。"))
    if not instance_name or len(instance_name) > 128:
        return redirect(url_for("create_instance_page", error="实例名称不能为空。"))
    if product != "hermes" and not password:
        return redirect(url_for("create_instance_page", error="Basic Auth 密码不能为空。"))
    if product == "hermes":
        basic_auth_enabled = False
    if version and not VERSION_RE.fullmatch(version):
        return redirect(url_for("create_instance_page", error="请填写有效的实例版本。"))
    if product == "evoscientist" and version == "latest" and not confirm_latest:
        return redirect(url_for("create_instance_page", error="使用 latest 前必须确认风险。"))
    request_id = "instance-create-" + uuid.uuid4().hex
    try:
        payload = {
                "request_id": request_id,
                "actor_user_public_id": current["public_id"],
                "owner_identity_type": owner_identity_type,
                "owner_identity": owner_identity,
                "legacy_user_id": legacy_user_id,
                "instance_name": instance_name,
                "product": product,
        }
        if product != "hermes":
            payload["basic_auth_enabled"] = basic_auth_enabled
            payload["basic_auth_password"] = password
        if version:
            payload["version"] = version
        if confirm_latest:
            payload["confirm_latest"] = True
        result = control_client.create_admin_instance(payload)
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
        batch=None, error=error, default_versions={},
    )


@app.get("/admin/default-versions")
def default_versions_page():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        versions = control_client.get_default_versions()
        error = request.args.get("error", "")
    except control_client.ControlError as exc:
        versions, error = {}, str(exc)
    return render_template("admin_default_versions.html", versions=versions, error=error)


@app.post("/admin/default-versions")
def update_default_versions():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    values = {product: request.form.get(product, "").strip() for product in ("openclaw", "hermes", "evoscientist")}
    payload = {**values, "confirm_latest": request.form.get("confirm_latest") == "true"}
    try:
        control_client.update_default_versions(payload)
    except control_client.ControlError as exc:
        return redirect(url_for("default_versions_page", error=str(exc)))
    return redirect(url_for("default_versions_page"))


@app.get("/admin/platform-users")
def platform_users():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    provider = request.args.get("provider", "all").strip().lower()
    status_filter = request.args.get("status", "all").strip().lower()
    query = request.args.get("q", "").strip()
    if provider not in {"all", "local", "campus-uis"}:
        provider = "all"
    if status_filter not in {"all", "active", "disabled", "locked"}:
        status_filter = "all"
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("per_page", DEFAULT_INSTANCE_PAGE_SIZE))
    except (TypeError, ValueError):
        page, per_page = 1, DEFAULT_INSTANCE_PAGE_SIZE
    if per_page not in INSTANCE_PAGE_SIZE_OPTIONS:
        per_page = DEFAULT_INSTANCE_PAGE_SIZE
    try:
        result = control_client.list_platform_users(
            provider=provider,
            status=status_filter,
            query=query,
            page=page,
            per_page=per_page,
        )
        users, pagination = result["users"], result["pagination"]
        error = request.args.get("error", "")
        imported = request.args.get("imported", "")
    except control_client.ControlError as exc:
        users, pagination, error = [], {
            "page": 1, "per_page": per_page, "total": 0, "total_pages": 1,
        }, str(exc)
        imported = ""
    pagination = {
        **pagination,
        "start": (pagination["page"] - 1) * pagination["per_page"] + 1
        if pagination["total"] else 0,
        "end": min(pagination["page"] * pagination["per_page"], pagination["total"]),
        "has_prev": pagination["page"] > 1,
        "has_next": pagination["page"] < pagination["total_pages"],
        "prev_page": max(1, pagination["page"] - 1),
        "next_page": min(pagination["total_pages"], pagination["page"] + 1),
    }
    return render_template(
        "admin_platform_users.html",
        users=users,
        provider_filter=provider,
        status_filter=status_filter,
        query=query,
        page_size_options=INSTANCE_PAGE_SIZE_OPTIONS,
        pagination=pagination,
        actor_public_id=current["public_id"],
        error=error,
        imported=imported,
    )


@app.post("/admin/platform-users/<user_public_id>/status")
def update_platform_user_status(user_public_id):
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        control_client.update_admin_user_status(
            current["public_id"], user_public_id, request.form.get("status", "")
        )
    except control_client.ControlError as exc:
        return redirect(url_for("platform_users", error=str(exc)))
    return redirect(url_for("platform_users"))


@app.post("/admin/platform-users/import")
def import_platform_users():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    provider = request.form.get("provider", "").strip()
    upload = request.files.get("input_csv")
    if provider not in {"campus-uis", "local"} or upload is None:
        return redirect(url_for("platform_users", error="请选择用户类型和 CSV 文件。"))
    try:
        reader = csv.DictReader(io.StringIO(upload.read().decode("utf-8-sig")))
        fields = set(reader.fieldnames or ())
        required = {"user_id", "name"} if provider == "campus-uis" else {
            "username", "name", "password",
        }
        allowed = required | ({"email", "status"} if provider == "campus-uis" else {"email"})
        if not required <= fields:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(required - fields))}")
        unsupported = fields - allowed
        if unsupported:
            raise ValueError(
                f"CSV contains unsupported columns: {', '.join(sorted(str(value) for value in unsupported))}"
            )
        rows = []
        for number, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f"row {number}: too many columns")
            rows.append({
                key: (value or "") if key == "password" else (value or "").strip()
                for key, value in row.items()
            })
        if not 1 <= len(rows) <= MAX_PLATFORM_USER_IMPORT_ROWS:
            raise ValueError("CSV must contain 1 to 100 rows")
        result = control_client.import_platform_users(
            current["public_id"], provider, rows
        )
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        return redirect(url_for("platform_users", error=str(exc)))
    except control_client.ControlError as exc:
        return redirect(url_for("platform_users", error=str(exc)))
    summary = f"created={result['created']} updated={result['updated']}"
    return redirect(url_for("platform_users", imported=summary))


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
    legacy_fields = {
        "owner_username", "legacy_user_id", "instance_name",
        "basic_auth_password", "basic_auth_enabled",
    }
    product_fields = legacy_fields | {"product", "version", "confirm_latest"}
    identity_legacy_fields = (legacy_fields - {"owner_username"}) | {
        "owner_identity_type", "owner_identity",
    }
    identity_product_fields = identity_legacy_fields | {
        "product", "version", "confirm_latest",
    }
    if not rows or len(rows) > MAX_INSTANCE_BATCH_ROWS:
        return redirect(url_for(
            "create_instance_page",
            error=f"CSV 必须包含 1-{MAX_INSTANCE_BATCH_ROWS} 行数据。",
        ))
    fields = frozenset(rows[0])
    accepted_fields = {
        frozenset(legacy_fields), frozenset(product_fields),
        frozenset(identity_legacy_fields), frozenset(identity_product_fields),
    }
    if fields not in accepted_fields:
        return redirect(url_for("create_instance_page", error="CSV 表头不符合要求。"))
    identity_format = fields in {
        frozenset(identity_legacy_fields), frozenset(identity_product_fields),
    }
    users = {}
    if not identity_format:
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
        owner_identity_type = (row.get("owner_identity_type") or "").strip()
        owner_identity = (row.get("owner_identity") or "").strip()
        owner = users.get((row.get("owner_username") or "").strip().casefold())
        legacy_user_id = (row.get("legacy_user_id") or "").strip()
        instance_name = (row.get("instance_name") or "").strip()
        password = row.get("basic_auth_password") or ""
        enabled = (row.get("basic_auth_enabled") or "").strip().lower()
        product = (row.get("product") or "openclaw").strip().lower()
        version = (row.get("version") or "").strip()
        confirm_latest = (row.get("confirm_latest") or "false").strip().lower()
        if identity_format and (
            owner_identity_type not in {"local", "campus-uis"}
            or not owner_identity or len(owner_identity) > 128
        ):
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 Owner 身份无效。",
            ))
        if not identity_format and owner is None:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 Owner 不存在或未启用。",
            ))
        if not LEGACY_USER_ID_RE.fullmatch(legacy_user_id) or legacy_user_id in seen:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行实例 ID 无效或重复。",
            ))
        if not instance_name or len(instance_name) > 128 or (product != "hermes" and not password):
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行名称或密码无效。",
            ))
        if product == "hermes":
            enabled = "false"
        elif enabled not in {"true", "false"}:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 Basic Auth 开关无效。",
            ))
        if product not in {"openclaw", "hermes", "evoscientist"}:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行产品无效。",
            ))
        if version and not BATCH_VERSION_RE.fullmatch(version):
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行版本无效。",
            ))
        if product == "evoscientist" and version not in {"", "latest"} and not re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}", version
        ):
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 EvoScientist 版本无效。",
            ))
        if product == "hermes" and version.startswith("sha256:"):
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 Hermes 版本无效。",
            ))
        if confirm_latest not in {"true", "false"}:
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行 latest 确认值无效。",
            ))
        if product == "evoscientist" and enabled != "true":
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行必须启用 Basic Auth。",
            ))
        if product == "evoscientist" and version == "latest" and confirm_latest != "true":
            return redirect(url_for(
                "create_instance_page", error=f"第 {line_number} 行使用 latest 前必须确认风险。",
            ))
        seen.add(legacy_user_id)
        instance = {
            **(
                {"owner_identity_type": owner_identity_type, "owner_identity": owner_identity}
                if identity_format else {"owner_user_public_id": owner["public_id"]}
            ),
            "legacy_user_id": legacy_user_id,
            "instance_name": instance_name,
            "product": product,
            **({"version": version} if version else {}),
        }
        if product != "hermes":
            instance["basic_auth_enabled"] = enabled == "true"
            instance["basic_auth_password"] = password
        if fields in {frozenset(product_fields), frozenset(identity_product_fields)}:
            instance["confirm_latest"] = confirm_latest == "true"
        instances.append(instance)
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
        batch=batch, error=error, default_versions={},
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
        try:
            per_page = int(request.args.get("per_page", DEFAULT_INSTANCE_PAGE_SIZE))
        except (TypeError, ValueError):
            per_page = DEFAULT_INSTANCE_PAGE_SIZE
        if per_page not in INSTANCE_PAGE_SIZE_OPTIONS:
            per_page = DEFAULT_INSTANCE_PAGE_SIZE
        try:
            instances_page = max(1, int(request.args.get("instances_page", 1)))
            operations_page = max(1, int(request.args.get("operations_page", 1)))
        except (TypeError, ValueError):
            instances_page = operations_page = 1
        summary = control_client.get_admin_metadata(
            instances_page=instances_page,
            operations_page=operations_page,
            per_page=per_page,
        )
        error = ""
    except control_client.ControlError as exc:
        summary = {
            "counts": {}, "overview": {
                "users": {}, "identities": {}, "instances": {"products": {}},
                "activity": {},
            }, "instance_public_ids": [],
            "instances": [], "operations": [],
            "instances_pagination": {"page": 1, "per_page": per_page, "total": 0, "total_pages": 1},
            "operations_pagination": {"page": 1, "per_page": per_page, "total": 0, "total_pages": 1},
        }
        error = str(exc)
    runtime = {"running": 0, "stopped": 0, "unknown": 0}
    instance_ids = summary.pop("instance_public_ids", [])
    for start in range(0, len(instance_ids), 100):
        batch = instance_ids[start:start + 100]
        try:
            statuses = executor_client.admin_instance_statuses(
                current["public_id"], batch
            )
        except executor_client.ExecutorError as exc:
            runtime["unknown"] += len(batch)
            error = error or f"无法读取实例运行状态：{exc}"
            continue
        for item in statuses:
            status = item["status"] if item["status"] in runtime else "unknown"
            runtime[status] += 1
        runtime["unknown"] += len(batch) - len(statuses)
    summary.setdefault("overview", {})["runtime"] = runtime
    for key in ("instances_pagination", "operations_pagination"):
        pagination = summary.setdefault(key, {"page": 1, "per_page": per_page, "total": 0, "total_pages": 1})
        pagination.update({
            "start": (pagination["page"] - 1) * pagination["per_page"] + 1 if pagination["total"] else 0,
            "end": min(pagination["page"] * pagination["per_page"], pagination["total"]),
            "has_prev": pagination["page"] > 1,
            "has_next": pagination["page"] < pagination["total_pages"],
            "prev_page": max(1, pagination["page"] - 1),
            "next_page": min(pagination["total_pages"], pagination["page"] + 1),
        })
    return render_template("admin_metadata.html", error=error, page_size_options=INSTANCE_PAGE_SIZE_OPTIONS, **summary)


@app.get("/admin/activity")
def activity_page():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    product = request.args.get("product", "all").strip().lower()
    if product not in {"all", "openclaw", "hermes", "evoscientist"}:
        product = "all"
    status_filter = request.args.get("status", "running").strip().lower()
    if status_filter not in {"running", "stopped", "all"}:
        status_filter = "running"
    query = request.args.get("q", "").strip()
    try:
        snapshots = control_client.get_activity_snapshots(current["public_id"])
        runtime_statuses = {}
        for start in range(0, len(snapshots), 100):
            runtime_statuses.update({
                item["instance_public_id"]: item["status"]
                for item in executor_client.admin_instance_statuses(
                    current["public_id"], [
                        item["instance_public_id"]
                        for item in snapshots[start:start + 100]
                    ],
                )
            })
        for snapshot in snapshots:
            snapshot["runtime_status"] = runtime_statuses.get(
                snapshot["instance_public_id"], "unknown"
            )
            snapshot["metric_items"] = activity_metric_items(snapshot["metrics"])
        error = request.args.get("error", "")
    except (control_client.ControlError, executor_client.ExecutorError) as exc:
        snapshots, error = [], str(exc)
    needle = query.casefold()
    snapshots = [
        snapshot for snapshot in snapshots
        if (product == "all" or snapshot["product"] == product)
        and (status_filter == "all" or snapshot["runtime_status"] == status_filter)
        and (not needle or needle in " ".join(
            str(snapshot.get(key) or "") for key in (
                "instance_name", "owner_username", "owner_display_name",
                "owner_uis_user_id",
            )
        ).casefold())
    ]
    try:
        per_page = int(request.args.get("per_page", DEFAULT_INSTANCE_PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = DEFAULT_INSTANCE_PAGE_SIZE
    if per_page not in INSTANCE_PAGE_SIZE_OPTIONS:
        per_page = DEFAULT_INSTANCE_PAGE_SIZE
    total = len(snapshots)
    total_pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = min(max(page, 1), total_pages)
    start = (page - 1) * per_page
    return render_template(
        "admin_activity.html", snapshots=snapshots[start:start + per_page],
        product_filter=product, status_filter=status_filter, query=query,
        page_size_options=INSTANCE_PAGE_SIZE_OPTIONS,
        pagination={
            "page": page, "per_page": per_page, "total": total,
            "total_pages": total_pages, "start": start + 1 if total else 0,
            "end": min(start + per_page, total), "has_prev": page > 1,
            "has_next": page < total_pages, "prev_page": max(1, page - 1),
            "next_page": min(total_pages, page + 1),
        },
        result=request.args.get("result", ""), error=error,
    )


@app.post("/admin/activity/collect")
def collect_activity():
    current = web_common.actor()
    if not current or current["role"] != "admin":
        return render_template("error.html", message="Forbidden"), 403
    try:
        instances = [
            item for item in control_client.list_admin_instances()
            if item["status"] != "deleted"
        ]
        available_ids = [item["public_id"] for item in instances]
        requested_id = request.form.get("instance_public_id", "").strip()
        if requested_id and requested_id not in available_ids:
            return redirect(url_for("activity_page", error="实例不存在或已删除。"))
        status_filter = request.form.get("status", "running").strip().lower()
        if status_filter not in {"running", "stopped", "all"}:
            status_filter = "running"
        if requested_id:
            instance_ids = [requested_id]
        elif status_filter == "all":
            instance_ids = available_ids
        else:
            runtime_statuses = {}
            for start in range(0, len(available_ids), 100):
                runtime_statuses.update({
                    item["instance_public_id"]: item["status"]
                    for item in executor_client.admin_instance_statuses(
                        current["public_id"], available_ids[start:start + 100]
                    )
                })
            instance_ids = [
                value for value in available_ids
                if runtime_statuses.get(value) == status_filter
            ]
        results = []
        for start in range(0, len(instance_ids), 100):
            results.extend(executor_client.collect_activity_snapshots(
                current["public_id"], instance_ids[start:start + 100]
            ))
    except (control_client.ControlError, executor_client.ExecutorError) as exc:
        return redirect(url_for("activity_page", error=str(exc)))
    failed = sum(item["status"] != "success" for item in results)
    return redirect(url_for(
        "activity_page",
        status=status_filter,
        result=f"采集完成：成功 {len(results) - failed}，失败 {failed}。",
    ))


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
    if not current or current["role"] != "admin" or action not in {"delete", "restore", "purge_deleted", "cleanup_failed"}:
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
