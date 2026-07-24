import os


SUPPORTED_EXTERNAL_TYPES = {"oauth2", "oidc"}


class AuthConfigurationError(ValueError):
    pass


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
    return config


def register_external_client(app, config):
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(app)
    kwargs = {
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
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
    return {
        "provider": config["provider"],
        "subject": subject.strip(),
        "external_username": str(claims.get("preferred_username") or claims.get("username") or "").strip(),
        "profile": claims,
    }
