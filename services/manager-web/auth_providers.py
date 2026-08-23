import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


SUPPORTED_EXTERNAL_TYPES = {"oauth2", "oidc"}


class AuthConfigurationError(ValueError):
    pass


def _https_url(value, label):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AuthConfigurationError(f"{label} must be an HTTPS URL with a host")
    return parsed


def _display_url(value):
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def external_auth_config(environ=None):
    values = os.environ if environ is None else environ
    provider = values.get("MANAGER_AUTH_PROVIDER", "").strip()
    auth_type = values.get("MANAGER_AUTH_TYPE", "").strip().lower()
    if auth_type not in SUPPORTED_EXTERNAL_TYPES:
        raise AuthConfigurationError("MANAGER_AUTH_TYPE must be oauth2 or oidc")

    config = {
        "provider": provider,
        "auth_type": auth_type,
        "client_id": values.get("MANAGER_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": values.get("MANAGER_OAUTH_CLIENT_SECRET", "").strip(),
        "scope": values.get("MANAGER_OAUTH_SCOPES", "openid profile email" if auth_type == "oidc" else "").strip(),
        "server_metadata_url": values.get("MANAGER_OIDC_DISCOVERY_URL", "").strip(),
        "authorize_url": values.get("MANAGER_OAUTH_AUTHORIZE_URL", "").strip(),
        "access_token_url": values.get("MANAGER_OAUTH_TOKEN_URL", "").strip(),
        "userinfo_endpoint": values.get("MANAGER_OAUTH_USERINFO_URL", "").strip(),
        "subject_claim": values.get("MANAGER_OAUTH_SUBJECT_CLAIM", "sub").strip(),
        "redirect_uri": values.get("MANAGER_OAUTH_REDIRECT_URI", "").strip(),
        "logout_url": values.get("MANAGER_OAUTH_LOGOUT_URL", "").strip(),
        "post_logout_redirect_uri": values.get(
            "MANAGER_OAUTH_POST_LOGOUT_REDIRECT_URI", ""
        ).strip(),
    }
    if not provider or provider in {"local", "nginx-basic"}:
        raise AuthConfigurationError("external authentication requires a named MANAGER_AUTH_PROVIDER")
    if not config["client_id"] or not config["client_secret"]:
        raise AuthConfigurationError("OAuth client id and secret are required")
    if not config["redirect_uri"]:
        raise AuthConfigurationError("OAuth redirect URI is required")
    if auth_type == "oidc" and not config["server_metadata_url"]:
        raise AuthConfigurationError("OIDC discovery URL is required")
    if auth_type == "oauth2" and not all(
        config[key] for key in ("authorize_url", "access_token_url", "userinfo_endpoint", "subject_claim")
    ):
        raise AuthConfigurationError("OAuth2 endpoints and subject claim are required")
    _https_url(config["redirect_uri"], "MANAGER_OAUTH_REDIRECT_URI")
    if config["logout_url"]:
        _https_url(config["logout_url"], "MANAGER_OAUTH_LOGOUT_URL")
    if config["post_logout_redirect_uri"]:
        _https_url(config["post_logout_redirect_uri"], "MANAGER_OAUTH_POST_LOGOUT_REDIRECT_URI")
    if auth_type == "oidc":
        _https_url(config["server_metadata_url"], "MANAGER_OIDC_DISCOVERY_URL")
    else:
        for key, label in (
            ("authorize_url", "MANAGER_OAUTH_AUTHORIZE_URL"),
            ("access_token_url", "MANAGER_OAUTH_TOKEN_URL"),
            ("userinfo_endpoint", "MANAGER_OAUTH_USERINFO_URL"),
        ):
            _https_url(config[key], label)
    return config


def provider_health(environ=None, *, probe=False, timeout=3):
    values = os.environ if environ is None else environ
    provider = values.get("MANAGER_AUTH_PROVIDER", "nginx-basic").strip() or "nginx-basic"
    local_enabled = values.get("MANAGER_LOCAL_AUTH_ENABLED", "false").lower() in {
        "1", "true", "yes", "on"
    }
    result = {
        "provider": provider,
        "mode": "local" if provider == "local" else "nginx-basic" if provider == "nginx-basic" else "external",
        "configured": True,
        "checks": [],
        "local_login_enabled": provider == "local" or local_enabled,
        "emergency_users_configured": bool(
            values.get("MANAGER_EMERGENCY_USERS", "").strip()
        ),
        "emergency_ready": bool(
            values.get("MANAGER_EMERGENCY_USERS", "").strip()
            and values.get("OPENCLAW_INTERNAL_TOKEN", "").strip()
        ),
    }
    if provider in {"local", "nginx-basic"}:
        result["status"] = "ok"
        result["checks"].append({"name": "provider", "status": "ok"})
        return result
    try:
        config = external_auth_config(values)
    except AuthConfigurationError as exc:
        result.update(status="error", configured=False, error=str(exc))
        result["checks"].append({"name": "configuration", "status": "error", "detail": str(exc)})
        return result
    session_secret_ok = bool(values.get("MANAGER_SESSION_SECRET", "").strip())
    result["checks"].append({
        "name": "session_secret",
        "status": "ok" if session_secret_ok else "error",
        "detail": "MANAGER_SESSION_SECRET is required" if not session_secret_ok else "",
    })
    endpoints = (
        {"oidc_discovery": config["server_metadata_url"]}
        if config["auth_type"] == "oidc"
        else {
            "authorize": config["authorize_url"],
            "token": config["access_token_url"],
            "userinfo": config["userinfo_endpoint"],
        }
    )
    for name, url in endpoints.items():
        check = {"name": name, "status": "configured", "url": _display_url(url)}
        if probe:
            try:
                request = Request(url, method="GET", headers={"Accept": "application/json"})
                with urlopen(request, timeout=timeout) as response:
                    check.update(
                        status="error" if response.status >= 500 else "ok",
                        http_status=response.status,
                    )
            except HTTPError as exc:
                check.update(
                    status="error" if exc.code >= 500 else "ok",
                    http_status=exc.code,
                )
            except (OSError, URLError, ValueError) as exc:
                check.update(status="error", detail=str(exc))
        result["checks"].append(check)
    fallback_ok = result["local_login_enabled"] or result["emergency_ready"]
    result["checks"].append({
        "name": "fallback_or_emergency",
        "status": "ok" if fallback_ok else "error",
        "detail": "Local fallback or a complete emergency login configuration is required" if not fallback_ok else "",
    })
    result["status"] = "ok" if all(item["status"] in {"ok", "configured"} for item in result["checks"]) else "error"
    return result


def register_external_client(app, config):
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(app)
    kwargs = {
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "token_endpoint_auth_method": "client_secret_basic",
        "client_kwargs": {"scope": config["scope"], "code_challenge_method": "S256"},
    }
    if config["auth_type"] == "oidc":
        kwargs["server_metadata_url"] = config["server_metadata_url"]
    else:
        kwargs.update(
            authorize_url=config["authorize_url"],
            access_token_url=config["access_token_url"],
            userinfo_endpoint=config["userinfo_endpoint"],
        )
    return oauth.register(name="manager_external", **kwargs)


def external_identity(client, token, config):
    claims = token.get("userinfo") if config["auth_type"] == "oidc" else None
    if not claims:
        response = client.get(config["userinfo_endpoint"], token=token)
        response.raise_for_status()
        claims = response.json()
    claims = dict(claims or {})
    subject = claims.get(config["subject_claim"])
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("external identity has no stable subject")
    profile = claims
    if config["subject_claim"] == "user_id":
        profile = {
            key: claims[key]
            for key in (
                "user_id", "user_name", "user_type", "email", "department"
            )
            if key in claims
        }
    return {
        "provider": config["provider"],
        "subject": subject.strip(),
        "external_username": str(
            claims.get("preferred_username")
            or claims.get("username")
            or claims.get("user_name")
            or claims.get("userName")
            or ""
        ).strip(),
        "profile": profile,
    }
