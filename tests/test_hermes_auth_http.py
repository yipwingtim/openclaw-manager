#!/usr/bin/env python3

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(WEB_DIR))


class FakeResponse:
    def __init__(self, payload=None, status=200, location=None):
        self.payload = payload
        self.status_code = status
        self.location = location
        self.headers = {}

    def get_json(self):
        return self.payload

    def set_cookie(self, *args, **kwargs):
        pass

    def delete_cookie(self, *args, **kwargs):
        pass


def load_user_app():
    flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def get(self, *args, **kwargs):
            return lambda function: function

        post = get

        def before_request(self, function):
            return function

        def context_processor(self, function):
            return function

        def make_response(self, value):
            return value if isinstance(value, FakeResponse) else FakeResponse(value)

    flask.Flask = FakeFlask
    flask.Response = FakeResponse
    flask.jsonify = lambda payload: FakeResponse(payload)
    flask.redirect = lambda location: FakeResponse(location=location, status=302)
    flask.render_template = lambda *args, **kwargs: FakeResponse()
    flask.request = types.SimpleNamespace(
        args={}, cookies={}, headers={}, form={}, mimetype="", full_path="", path="",
        method="GET", files={},
    )
    flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}"
    flask.send_file = lambda *args, **kwargs: FakeResponse()

    auth_providers = types.ModuleType("auth_providers")
    auth_providers.AuthConfigurationError = ValueError
    auth_providers.external_auth_config = lambda: {}
    auth_providers.register_external_client = lambda *args: None

    previous = {name: sys.modules.get(name) for name in ("flask", "auth_providers")}
    sys.modules["flask"] = flask
    sys.modules["auth_providers"] = auth_providers
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_auth_user_app", WEB_DIR / "user_app.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, value in previous.items():
            if value is None:
                del sys.modules[name]
            else:
                sys.modules[name] = value
    return module


class HermesAuthHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user_app = load_user_app()

    def authorize_args(self, **overrides):
        values = {
            "response_type": "code", "client_id": "client-1",
            "redirect_uri": "https://manager.example.test:39119/auth/callback",
            "state": "state-1", "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
        }
        values.update(overrides)
        return values

    def test_authorize_uses_manager_session_and_preserves_state(self):
        app = self.user_app
        request = types.SimpleNamespace(
            args=self.authorize_args(), cookies={app.web_common.COOKIE_NAME: "manager-session"},
            headers={"X-Forwarded-Proto": "https"}, full_path="/auth/hermes/authorize?...",
        )
        with patch.object(app, "request", request), patch.object(
            app.web_common, "actor", return_value={"public_id": "user-1"}
        ), patch.object(
            app.control_client, "authorize_hermes", return_value={"code": "one-time-code"}
        ) as authorize:
            response = app.hermes_authorize()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.location,
            "https://manager.example.test:39119/auth/callback?code=one-time-code&state=state-1",
        )
        payload = authorize.call_args.args[0]
        self.assertNotIn("instance_id", payload)
        self.assertNotIn("user_id", payload)
        self.assertEqual(payload["session_hash"], app.web_common.token_hash("manager-session"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_authorize_rejects_http_and_non_s256(self):
        app = self.user_app
        for headers, overrides in (({}, {}), ({"X-Forwarded-Proto": "https"}, {"code_challenge_method": "plain"})):
            request = types.SimpleNamespace(
                args=self.authorize_args(**overrides), cookies={}, headers=headers,
                full_path="/auth/hermes/authorize",
            )
            with self.subTest(overrides=overrides), patch.object(app, "request", request):
                response, status = app.hermes_authorize()
            self.assertEqual(status, 400)
            self.assertEqual(response.get_json(), {"error": "invalid_request"})

    def test_token_returns_generic_error_and_requires_https_form(self):
        app = self.user_app
        form = {
            "grant_type": "authorization_code", "code": "code", "client_id": "client",
            "client_secret": "secret", "redirect_uri": "https://manager.example/auth/callback",
            "code_verifier": "v" * 43,
        }
        request = types.SimpleNamespace(
            headers={"X-Forwarded-Proto": "https"}, mimetype="application/x-www-form-urlencoded",
            form=form,
        )
        with patch.object(app, "request", request), patch.object(
            app.control_client, "redeem_hermes",
            side_effect=app.control_client.ControlError(400, "database detail"),
        ):
            response, status = app.hermes_token()
        self.assertEqual(status, 400)
        self.assertEqual(response.get_json(), {"error": "invalid_grant"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        request.headers = {}
        with patch.object(app, "request", request):
            _, status = app.hermes_token()
        self.assertEqual(status, 400)

    def test_jwks_is_cacheable(self):
        app = self.user_app
        keys = {"keys": [{"kid": "current", "kty": "OKP"}]}
        with patch.object(app.control_client, "hermes_jwks", return_value=keys):
            response = app.hermes_jwks()
        self.assertEqual(response.get_json(), keys)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=300")


if __name__ == "__main__":
    unittest.main()
