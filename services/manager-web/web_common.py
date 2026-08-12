import hashlib
import hmac
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

from flask import redirect, render_template, request, url_for

import control_client
import auth_providers


AUTH_PROVIDER = os.environ.get("MANAGER_AUTH_PROVIDER", "nginx-basic").strip()
LOCAL_AUTH_ENABLED = os.environ.get(
    "MANAGER_LOCAL_AUTH_ENABLED", "false"
).lower() in {"1", "true", "yes", "on"}
COOKIE_NAME = "openclaw_manager_session"
INSTANCE_RETURN_COOKIE = "openclaw_manager_instance_return"
HERMES_RETURN_COOKIE = "openclaw_manager_hermes_return"
INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
COOKIE_SECURE = os.environ.get("MANAGER_COOKIE_SECURE", "true").lower() not in {
    "0", "false", "no"
}
SESSION_HOURS = int(os.environ.get("MANAGER_SESSION_HOURS", "8"))
INTERNAL_TOKEN = os.environ.get("OPENCLAW_INTERNAL_TOKEN", "").strip()
SESSION_SECRET = os.environ.get("MANAGER_SESSION_SECRET", "").strip()


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def actor():
    try:
        if AUTH_PROVIDER == "nginx-basic":
            subject = (
                request.headers.get("X-Remote-User")
                or request.headers.get("X-Forwarded-User")
                or ""
            ).strip()
            return control_client.resolve_identity("nginx-basic", subject) if subject else None
        raw_token = request.cookies.get(COOKIE_NAME, "")
        if not raw_token:
            return None
        providers = [AUTH_PROVIDER]
        if local_auth_enabled() and AUTH_PROVIDER != "local":
            providers.append("local")
        for provider in providers:
            try:
                return control_client.resolve_session(token_hash(raw_token), provider)
            except control_client.ControlError:
                continue
        return None
    except control_client.ControlError:
        return None


def require_internal_token():
    if request.path == "/health":
        return None
    if not INTERNAL_TOKEN:
        return None
    if not hmac.compare_digest(
        request.headers.get("X-OpenClaw-Internal-Token", ""), INTERNAL_TOKEN
    ):
        return render_template("error.html", message="Forbidden"), 403
    return None


def external_auth_enabled():
    return AUTH_PROVIDER not in {"nginx-basic", "local"}


def local_auth_enabled():
    return AUTH_PROVIDER == "local" or (
        external_auth_enabled() and LOCAL_AUTH_ENABLED
    )


def external_client(app):
    if not SESSION_SECRET:
        raise auth_providers.AuthConfigurationError("MANAGER_SESSION_SECRET is required")
    config = auth_providers.external_auth_config()
    return auth_providers.register_external_client(app, config), config


def external_callback(app):
    try:
        client, config = external_client(app)
        token = client.authorize_access_token()
        identity = auth_providers.external_identity(client, token, config)
        access_token = token.get("access_token", "")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("external authentication returned no access token")
        raw_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        ).replace(microsecond=0).isoformat()
        user = control_client.external_login(
            {
                **identity,
                "token_hash": token_hash(raw_token),
                "external_token_hash": token_hash(access_token),
                "csrf_token": csrf_token,
                "expires_at": expires_at,
            }
        )
    except Exception:
        return render_template("error.html", message="External authentication failed."), 401
    instance_id = request.cookies.get(INSTANCE_RETURN_COOKIE, "")
    hermes_return = request.cookies.get(HERMES_RETURN_COOKIE, "")
    destination = (
        hermes_return
        if hermes_return.startswith("/auth/hermes/authorize?")
        else
        url_for("open_instance", instance_public_id=instance_id)
        if INSTANCE_ID_RE.fullmatch(instance_id)
        else "/admin" if user["role"] == "admin" else url_for("index")
    )
    response = app.make_response(redirect(destination))
    response.set_cookie(
        COOKIE_NAME, raw_token, secure=COOKIE_SECURE, httponly=True,
        samesite="Lax", max_age=SESSION_HOURS * 3600,
    )
    response.delete_cookie(INSTANCE_RETURN_COOKIE)
    response.delete_cookie(HERMES_RETURN_COOKIE)
    return response


def login_page(app, action="/login"):
    instance_id = request.args.get("instance", "")
    if instance_id and not INSTANCE_ID_RE.fullmatch(instance_id):
        return render_template("error.html", message="Invalid instance return target."), 400
    if external_auth_enabled() and not local_auth_enabled():
        client, config = external_client(app)
        response = app.make_response(client.authorize_redirect(config["redirect_uri"]))
        if instance_id:
            response.set_cookie(
                INSTANCE_RETURN_COOKIE, instance_id, secure=COOKIE_SECURE,
                httponly=True, samesite="Lax", max_age=600,
            )
        return response
    login_csrf = secrets.token_urlsafe(32)
    response = render_template(
        "login.html", error="", login_csrf=login_csrf, login_action=action,
        external_login_url="/auth/uis/login" if external_auth_enabled() else "",
    )
    response = app.make_response(response)
    response.set_cookie(
        "openclaw_manager_login_csrf", login_csrf, secure=COOKIE_SECURE,
        httponly=True, samesite="Lax", max_age=600,
    )
    if instance_id:
        response.set_cookie(
            INSTANCE_RETURN_COOKIE, instance_id, secure=COOKIE_SECURE,
            httponly=True, samesite="Lax", max_age=600,
        )
    return response


def local_login(app, login_action="/login"):
    if not local_auth_enabled():
        return render_template("error.html", message="Local login is disabled."), 404
    cookie_csrf = request.cookies.get("openclaw_manager_login_csrf", "")
    if not cookie_csrf or not hmac.compare_digest(
        cookie_csrf, request.form.get("csrf_token", "")
    ):
        return render_template("error.html", message="Forbidden: invalid CSRF token."), 403
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
    ).replace(microsecond=0).isoformat()
    try:
        control_client.local_login(
            {
                "username": request.form.get("username", ""),
                "password": request.form.get("password", ""),
                "token_hash": token_hash(raw_token),
                "csrf_token": csrf_token,
                "expires_at": expires_at,
            }
        )
    except control_client.ControlError:
        return render_template(
            "login.html", error="Invalid username or password.",
            login_csrf=cookie_csrf, login_action=login_action,
            external_login_url="/auth/uis/login" if external_auth_enabled() else "",
        ), 401
    instance_id = request.cookies.get(INSTANCE_RETURN_COOKIE, "")
    hermes_return = request.cookies.get(HERMES_RETURN_COOKIE, "")
    destination = (
        hermes_return
        if hermes_return.startswith("/auth/hermes/authorize?")
        else
        url_for("open_instance", instance_public_id=instance_id)
        if INSTANCE_ID_RE.fullmatch(instance_id)
        else url_for("index")
    )
    response = app.make_response(redirect(destination))
    response.set_cookie(
        COOKIE_NAME, raw_token, secure=COOKIE_SECURE, httponly=True,
        samesite="Lax", max_age=SESSION_HOURS * 3600,
    )
    response.delete_cookie("openclaw_manager_login_csrf")
    response.delete_cookie(INSTANCE_RETURN_COOKIE)
    response.delete_cookie(HERMES_RETURN_COOKIE)
    return response


def logout():
    raw_token = request.cookies.get(COOKIE_NAME, "")
    current = actor() if raw_token else None
    if raw_token:
        control_client.delete_session(token_hash(raw_token))
    try:
        config = auth_providers.external_auth_config() if external_auth_enabled() else {}
    except auth_providers.AuthConfigurationError:
        config = {}
    if (
        current
        and current.get("provider") != "local"
        and config.get("logout_url")
        and config.get("post_logout_redirect_uri")
    ):
        query = urllib.parse.urlencode(
            {
                "redirectToLogin": "true",
                "redirectToUrl": config["post_logout_redirect_uri"],
            }
        )
        response = redirect(f'{config["logout_url"]}?{query}')
    else:
        response = redirect(url_for("login"))
    response.delete_cookie(COOKIE_NAME)
    return response


def external_logout_callback():
    external_token = request.headers.get("X-UIS-Logout-Token", "").strip()
    if not external_auth_enabled() or not external_token:
        return "", 400
    control_client.delete_external_session(token_hash(external_token))
    return "", 204


def require_csrf():
    if (
        AUTH_PROVIDER == "nginx-basic"
        or request.method != "POST"
        or request.path in {"/login", "/admin/login"}
        or request.path == "/auth/hermes/token"
    ):
        return None
    current = actor()
    if not current or not hmac.compare_digest(
        request.form.get("csrf_token", ""), current.get("csrf_token", "")
    ):
        return render_template("error.html", message="Forbidden: invalid CSRF token."), 403
    return None


def context():
    current = actor()
    display_name = ""
    if current:
        display_name = (
            current.get("display_name") or current["username"]
            if current.get("provider") == "campus-uis"
            else current["username"]
        )
    return {
        "current_user": display_name,
        "is_admin": bool(current and current["role"] == "admin"),
        "show_admin_instance_nav": bool(current and current["role"] == "admin"),
        "show_global_admin_nav": False,
        "csrf_token": current.get("csrf_token", "") if current else "",
        "auth_provider": AUTH_PROVIDER,
    }
