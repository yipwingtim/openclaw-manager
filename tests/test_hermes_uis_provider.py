#!/usr/bin/env python3

import importlib.util
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import jwt as real_jwt
from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "manager-control"))
from hermes_auth_bridge import BridgePrincipal, SigningKeys
from tests.tls_fixtures import write_test_ca


ROOT_DIR = Path(__file__).resolve().parents[1]
PLUGIN = ROOT_DIR / "templates" / "hermes" / "plugins" / "campus-uis-bridge" / "__init__.py"


@dataclass(frozen=True)
class Session:
    user_id: str
    email: str
    display_name: str
    org_id: str
    provider: str
    expires_at: int
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class LoginStart:
    redirect_url: str
    cookie_payload: dict


def load_plugin():
    auth = types.ModuleType("hermes_cli.dashboard_auth")
    auth.DashboardAuthProvider = object
    auth.InvalidCodeError = type("InvalidCodeError", (Exception,), {})
    auth.ProviderError = type("ProviderError", (Exception,), {})
    auth.RefreshExpiredError = type("RefreshExpiredError", (Exception,), {})
    auth.Session = Session
    auth.LoginStart = LoginStart

    jwt = types.ModuleType("jwt")
    jwt.InvalidTokenError = type("InvalidTokenError", (Exception,), {})
    jwt.PyJWKClientError = type("PyJWKClientError", (Exception,), {})
    jwt.PyJWKClient = lambda *args, **kwargs: types.SimpleNamespace(
        get_signing_key_from_jwt=lambda token: types.SimpleNamespace(key="key")
    )
    jwt.decode = lambda *args, **kwargs: {}

    httpx = types.ModuleType("httpx")
    httpx.HTTPError = type("HTTPError", (Exception,), {})
    httpx.post = lambda *args, **kwargs: None

    modules = {
        "hermes_cli": types.ModuleType("hermes_cli"),
        "hermes_cli.dashboard_auth": auth, "jwt": jwt, "httpx": httpx,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        spec = importlib.util.spec_from_file_location("campus_uis_bridge", PLUGIN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, value in previous.items():
            if value is None:
                del sys.modules[name]
            else:
                sys.modules[name] = value
    return module


class HermesUISProviderTests(unittest.TestCase):
    INSTANCE_ID = "11111111-1111-1111-1111-111111111111"

    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin()
        cls.temp = tempfile.TemporaryDirectory()
        cls.ca_file = Path(cls.temp.name) / "manager-ca.crt"
        write_test_ca(cls.ca_file)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def provider(self):
        return self.plugin.CampusUISBridgeProvider(
            "https://manager.example.test:30015/auth/hermes",
            "client-1", "secret-1", self.INSTANCE_ID,
            "https://manager.example.test:39119/auth/callback",
            str(self.ca_file),
        )

    def test_provider_uses_one_ca_context_for_token_and_jwks(self):
        context = object()
        with patch.object(
            self.plugin.ssl, "create_default_context", return_value=context
        ) as create_context, patch.object(
            self.plugin.jwt, "PyJWKClient"
        ) as jwks:
            provider = self.provider()

        create_context.assert_called_once_with(cafile=str(self.ca_file))
        self.assertIs(provider.ssl_context, context)
        self.assertIs(jwks.call_args.kwargs["ssl_context"], context)

        response = types.SimpleNamespace(status_code=400, json=lambda: {})
        with patch.object(self.plugin.httpx, "post", return_value=response) as post:
            with self.assertRaises(self.plugin.InvalidCodeError):
                provider.complete_login(
                    code="one-time-code", state="state", code_verifier="verifier",
                    redirect_uri="http://manager.example.test/auth/callback",
                )
        self.assertIs(post.call_args.kwargs["verify"], context)

    def valid_claims(self, **overrides):
        claims = {
            "iss": "https://manager.example.test:30015/auth/hermes",
            "aud": "client-1", "sub": "22222222-2222-2222-2222-222222222222",
            "instance_id": self.INSTANCE_ID, "provider": "campus-uis",
            "iat": 100, "exp": 1000, "jti": "token-id",
        }
        claims.update(overrides)
        return claims

    def test_start_login_creates_s256_pkce_and_state_cookie(self):
        start = self.provider().start_login(
            redirect_uri="http://manager.example.test/auth/callback"
        )
        self.assertIn("code_challenge_method=S256", start.redirect_url)
        self.assertIn("client_id=client-1", start.redirect_url)
        self.assertIn(
            "redirect_uri=https%3A%2F%2Fmanager.example.test%3A39119%2Fauth%2Fcallback",
            start.redirect_url,
        )
        self.assertRegex(start.cookie_payload["hermes_session_pkce"], r"^state=.+;verifier=.{43,128}$")

    def test_verify_session_pins_algorithm_claims_and_instance(self):
        provider = self.provider()
        with patch.object(self.plugin.jwt, "decode", return_value=self.valid_claims()) as decode, patch.object(
            self.plugin.time, "time", return_value=200
        ):
            session = provider.verify_session(access_token="signed-token")
        self.assertEqual(session.user_id, "22222222-2222-2222-2222-222222222222")
        self.assertEqual(session.provider, "campus-uis-bridge")
        self.assertEqual(session.refresh_token, "")
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["EdDSA"])
        self.assertEqual(decode.call_args.kwargs["audience"], "client-1")

        for claims in (
            self.valid_claims(instance_id="33333333-3333-3333-3333-333333333333"),
            self.valid_claims(provider="attacker"),
            self.valid_claims(sub="not-a-uuid"),
            self.valid_claims(iat=999),
        ):
            with self.subTest(claims=claims), patch.object(
                self.plugin.jwt, "decode", return_value=claims
            ), patch.object(self.plugin.time, "time", return_value=200):
                self.assertIsNone(provider.verify_session(access_token="bad-token"))

    def test_complete_login_uses_confidential_client_and_rejects_error(self):
        provider = self.provider()
        response = types.SimpleNamespace(status_code=400, json=lambda: {"error": "invalid_grant"})
        with patch.object(self.plugin.httpx, "post", return_value=response) as post:
            with self.assertRaises(self.plugin.InvalidCodeError):
                provider.complete_login(
                    code="one-time-code", state="state", code_verifier="verifier",
                    redirect_uri="http://manager.example.test/auth/callback",
                )
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["client_secret"], "secret-1")
        self.assertEqual(data["code_verifier"], "verifier")
        self.assertEqual(
            data["redirect_uri"],
            "https://manager.example.test:39119/auth/callback",
        )

    def test_bridge_token_establishes_official_hermes_session(self):
        private_key = serialization.load_pem_private_key(
            SigningKeys.generate_private_key_pem(), password=None
        )
        keys = SigningKeys({"integration": private_key}, "integration")
        token = keys.issue_access_token(
            BridgePrincipal(
                "22222222-2222-2222-2222-222222222222",
                self.INSTANCE_ID,
                "client-1",
            ),
            "https://manager.example.test:30015/auth/hermes",
            now=int(__import__("time").time()),
        )
        provider = self.provider()
        provider.jwks = types.SimpleNamespace(
            get_signing_key_from_jwt=lambda value: types.SimpleNamespace(
                key=private_key.public_key()
            )
        )
        with patch.object(self.plugin.jwt, "decode", side_effect=real_jwt.decode), patch.object(
            self.plugin.time, "time", return_value=int(__import__("time").time())
        ):
            session = provider.verify_session(access_token=token)

        self.assertEqual(session.user_id, "22222222-2222-2222-2222-222222222222")
        self.assertEqual(session.provider, "campus-uis-bridge")
        self.assertEqual(session.refresh_token, "")

    def test_register_requires_all_instance_credentials(self):
        ctx = types.SimpleNamespace(register_dashboard_auth_provider=lambda provider: None)
        with patch.dict(self.plugin.os.environ, {}, clear=True), patch.object(
            ctx, "register_dashboard_auth_provider"
        ) as register:
            self.plugin.register(ctx)
        register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
