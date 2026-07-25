import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT_DIR = Path(__file__).resolve().parents[1]
MODULE_FILE = ROOT_DIR / "services" / "manager-web" / "auth_providers.py"
SPEC = importlib.util.spec_from_file_location("auth_providers", MODULE_FILE)
auth_providers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth_providers)


class AuthProviderTests(unittest.TestCase):
    def test_oidc_requires_discovery_and_uses_stable_subject(self):
        config = auth_providers.external_auth_config(
            {
                "MANAGER_AUTH_PROVIDER": "campus-uis",
                "MANAGER_AUTH_TYPE": "oidc",
                "MANAGER_OAUTH_CLIENT_ID": "client",
                "MANAGER_OAUTH_CLIENT_SECRET": "secret",
                "MANAGER_OIDC_DISCOVERY_URL": "https://login.example.test/.well-known/openid-configuration",
                "MANAGER_OAUTH_REDIRECT_URI": "https://manager.example.test/auth/callback",
            }
        )
        identity = auth_providers.external_identity(
            Mock(),
            {"userinfo": {"sub": "stable-123", "preferred_username": "alice"}},
            config,
        )

        self.assertEqual(identity["provider"], "campus-uis")
        self.assertEqual(identity["subject"], "stable-123")
        self.assertEqual(identity["external_username"], "alice")

    def test_oauth2_reads_configured_subject_from_userinfo(self):
        config = auth_providers.external_auth_config(
            {
                "MANAGER_AUTH_PROVIDER": "company-sso",
                "MANAGER_AUTH_TYPE": "oauth2",
                "MANAGER_OAUTH_CLIENT_ID": "client",
                "MANAGER_OAUTH_CLIENT_SECRET": "secret",
                "MANAGER_OAUTH_AUTHORIZE_URL": "https://login.example.test/authorize",
                "MANAGER_OAUTH_TOKEN_URL": "https://login.example.test/token",
                "MANAGER_OAUTH_USERINFO_URL": "https://login.example.test/userinfo",
                "MANAGER_OAUTH_SUBJECT_CLAIM": "uid",
                "MANAGER_OAUTH_REDIRECT_URI": "https://manager.example.test/auth/callback",
            }
        )
        client = Mock()
        client.get.return_value.json.return_value = {"uid": "immutable-42", "username": "alice"}

        identity = auth_providers.external_identity(client, {"access_token": "unused"}, config)

        self.assertEqual(identity["subject"], "immutable-42")
        client.get.assert_called_once_with(
            "https://login.example.test/userinfo", token={"access_token": "unused"}
        )
        client.get.return_value.raise_for_status.assert_called_once()

    def test_external_identity_rejects_missing_subject(self):
        config = {
            "provider": "company-sso",
            "auth_type": "oauth2",
            "subject_claim": "uid",
            "userinfo_endpoint": "https://login.example.test/userinfo",
        }
        client = Mock()
        client.get.return_value.json.return_value = {"username": "alice"}

        with self.assertRaisesRegex(ValueError, "stable subject"):
            auth_providers.external_identity(client, {}, config)


if __name__ == "__main__":
    unittest.main()
