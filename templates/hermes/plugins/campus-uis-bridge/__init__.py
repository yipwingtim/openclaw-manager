"""Hermes Dashboard provider for the Manager UIS authentication bridge."""
import base64
import hashlib
import os
import secrets
import ssl
import time
import urllib.parse
import uuid

import httpx
import jwt
from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCodeError,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)


class CampusUISBridgeProvider(DashboardAuthProvider):
    name = "campus-uis-bridge"
    display_name = "Campus UIS"

    def __init__(
        self, issuer, client_id, client_secret, instance_id, redirect_uri, ca_file
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.instance_id = str(uuid.UUID(instance_id))
        self.redirect_uri = redirect_uri
        self.ssl_context = ssl.create_default_context(cafile=ca_file)
        self.jwks = jwt.PyJWKClient(
            f"{self.issuer}/jwks.json", cache_keys=True, lifespan=300,
            ssl_context=self.ssl_context,
        )

    def start_login(self, *, redirect_uri):
        del redirect_uri
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.client_id,
            "redirect_uri": self.redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return LoginStart(
            redirect_url=f"{self.issuer}/authorize?{query}",
            cookie_payload={"hermes_session_pkce": f"state={state};verifier={verifier}"},
        )

    def complete_login(self, *, code, state, code_verifier, redirect_uri):
        del state, redirect_uri
        try:
            response = httpx.post(
                f"{self.issuer}/token",
                data={
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": self.redirect_uri, "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"}, timeout=10.0,
                verify=self.ssl_context,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Manager authentication bridge is unavailable") from exc
        if response.status_code != 200:
            raise InvalidCodeError("Manager authentication bridge rejected the code")
        try:
            token = response.json()["access_token"]
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidCodeError("Manager authentication bridge returned an invalid response") from exc
        session = self._session(token)
        if session is None:
            raise InvalidCodeError("Manager authentication bridge returned an invalid token")
        return session

    def verify_session(self, *, access_token):
        try:
            return self._session(access_token)
        except ProviderError:
            raise

    def _session(self, token):
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["EdDSA"], audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["iss", "aud", "sub", "instance_id", "provider", "iat", "exp", "jti"]},
            )
            user_id = str(uuid.UUID(claims["sub"]))
            if (
                claims["instance_id"] != self.instance_id
                or claims["provider"] != "campus-uis"
                or claims["iat"] > int(time.time()) + 30
            ):
                return None
            return Session(
                user_id=user_id, email="", display_name=user_id, org_id="",
                provider=self.name, expires_at=int(claims["exp"]),
                access_token=token, refresh_token="",
            )
        except jwt.PyJWKClientError as exc:
            raise ProviderError("Manager authentication bridge keys are unavailable") from exc
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
            return None

    def refresh_session(self, *, refresh_token):
        del refresh_token
        raise RefreshExpiredError("Manager bridge sessions do not refresh")

    def revoke_session(self, *, refresh_token):
        del refresh_token


def register(ctx):
    values = [os.environ.get(name, "").strip() for name in (
        "HERMES_UIS_BRIDGE_ISSUER", "HERMES_UIS_BRIDGE_CLIENT_ID",
        "HERMES_UIS_BRIDGE_CLIENT_SECRET", "HERMES_UIS_BRIDGE_INSTANCE_ID",
        "HERMES_UIS_BRIDGE_REDIRECT_URI", "HERMES_UIS_BRIDGE_CA_FILE",
    )]
    if all(values):
        ctx.register_dashboard_auth_provider(CampusUISBridgeProvider(*values))
