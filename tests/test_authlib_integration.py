import importlib.util
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))


try:
    from flask import Flask, session
    import authlib  # noqa: F401
    import requests  # noqa: F401
except ImportError:
    Flask = None

from auth_providers import register_external_client


@unittest.skipIf(Flask is None, "manager-web runtime dependencies are not installed")
class AuthlibIntegrationTests(unittest.TestCase):
    def test_authorization_redirect_stores_state_nonce_and_pkce_verifier(self):
        app = Flask(__name__)
        app.secret_key = "test-only-secret"
        client = register_external_client(
            app,
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scope": "openid profile",
                "auth_type": "oauth2",
                "authorize_url": "https://sso.example.test/authorize",
                "access_token_url": "https://sso.example.test/token",
                "userinfo_endpoint": "https://sso.example.test/userinfo",
            },
        )

        with app.test_request_context("/"):
            response = client.authorize_redirect("https://manager.example.test/auth/callback")
            query = parse_qs(urlparse(response.location).query)
            state_data = next(value for key, value in session.items() if key.startswith("_state_manager_external_"))

        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])
        self.assertTrue(query["nonce"][0])
        self.assertEqual(state_data["data"]["nonce"], query["nonce"][0])
        self.assertTrue(state_data["data"]["code_verifier"])


if __name__ == "__main__":
    unittest.main()
