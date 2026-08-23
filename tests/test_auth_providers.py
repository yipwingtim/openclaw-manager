import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError


ROOT_DIR = Path(__file__).resolve().parents[1]
MODULE_FILE = ROOT_DIR / "services" / "manager-web" / "auth_providers.py"
SPEC = importlib.util.spec_from_file_location("auth_providers", MODULE_FILE)
auth_providers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth_providers)


class AuthProviderTests(unittest.TestCase):
    def external_values(self, **overrides):
        values = {
            "MANAGER_AUTH_PROVIDER": "company-sso",
            "MANAGER_AUTH_TYPE": "oauth2",
            "MANAGER_OAUTH_CLIENT_ID": "client",
            "MANAGER_OAUTH_CLIENT_SECRET": "secret",
            "MANAGER_OAUTH_AUTHORIZE_URL": "https://login.example.test/authorize",
            "MANAGER_OAUTH_TOKEN_URL": "https://login.example.test/token",
            "MANAGER_OAUTH_USERINFO_URL": "https://login.example.test/userinfo",
            "MANAGER_OAUTH_SUBJECT_CLAIM": "uid",
            "MANAGER_OAUTH_REDIRECT_URI": "https://manager.example.test/auth/callback",
            "MANAGER_EMERGENCY_USERS": "breakglass",
            "MANAGER_SESSION_SECRET": "session-secret",
            "OPENCLAW_INTERNAL_TOKEN": "internal-token",
        }
        values.update(overrides)
        return values

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

    def test_oauth2_uses_client_secret_basic_and_configures_logout(self):
        config = auth_providers.external_auth_config(
            {
                "MANAGER_AUTH_PROVIDER": "campus-uis",
                "MANAGER_AUTH_TYPE": "oauth2",
                "MANAGER_OAUTH_CLIENT_ID": "client",
                "MANAGER_OAUTH_CLIENT_SECRET": "secret",
                "MANAGER_OAUTH_AUTHORIZE_URL": "https://login.example.test/authorize",
                "MANAGER_OAUTH_TOKEN_URL": "https://login.example.test/token",
                "MANAGER_OAUTH_USERINFO_URL": "https://login.example.test/userinfo",
                "MANAGER_OAUTH_SUBJECT_CLAIM": "user_id",
                "MANAGER_OAUTH_REDIRECT_URI": "https://manager.example.test/auth/callback",
                "MANAGER_OAUTH_LOGOUT_URL": "https://login.example.test/logout",
                "MANAGER_OAUTH_POST_LOGOUT_REDIRECT_URI": "https://manager.example.test/login",
            }
        )

        self.assertEqual(config["subject_claim"], "user_id")
        self.assertEqual(config["logout_url"], "https://login.example.test/logout")
        self.assertEqual(
            config["post_logout_redirect_uri"],
            "https://manager.example.test/login",
        )

        oauth = Mock()
        oauth.register.return_value = "client"
        with unittest.mock.patch.dict(
            "sys.modules",
            {"authlib.integrations.flask_client": Mock(OAuth=Mock(return_value=oauth))},
        ):
            client = auth_providers.register_external_client(Mock(), config)

        self.assertEqual(client, "client")
        kwargs = oauth.register.call_args.kwargs
        self.assertEqual(kwargs["token_endpoint_auth_method"], "client_secret_basic")

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

    def test_external_config_rejects_non_https_endpoints(self):
        with self.assertRaisesRegex(
            auth_providers.AuthConfigurationError, "MANAGER_OAUTH_TOKEN_URL must be an HTTPS URL"
        ):
            auth_providers.external_auth_config(
                self.external_values(MANAGER_OAUTH_TOKEN_URL="http://login.example.test/token")
            )

    def test_local_health_does_not_probe_network(self):
        with patch.object(auth_providers, "urlopen") as urlopen:
            health = auth_providers.provider_health(
                {"MANAGER_AUTH_PROVIDER": "local"}, probe=True
            )

        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["local_login_enabled"])
        urlopen.assert_not_called()

    def test_external_health_requires_fallback_or_emergency_user(self):
        health = auth_providers.provider_health(
            self.external_values(MANAGER_EMERGENCY_USERS="")
        )

        self.assertEqual(health["status"], "error")
        self.assertEqual(health["checks"][-1]["name"], "fallback_or_emergency")

    def test_external_health_accepts_local_fallback(self):
        health = auth_providers.provider_health(
            self.external_values(
                MANAGER_EMERGENCY_USERS="", MANAGER_LOCAL_AUTH_ENABLED="true"
            )
        )

        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["local_login_enabled"])

    def test_health_output_removes_endpoint_query_and_fragment(self):
        health = auth_providers.provider_health(self.external_values(
            MANAGER_OAUTH_AUTHORIZE_URL="https://login.example.test/authorize?secret=value#fragment"
        ))

        authorize = next(check for check in health["checks"] if check["name"] == "authorize")
        self.assertEqual(authorize["url"], "https://login.example.test/authorize")
        self.assertNotIn("secret=value", str(health))

    def test_external_health_requires_session_secret(self):
        health = auth_providers.provider_health(
            self.external_values(MANAGER_SESSION_SECRET="")
        )

        self.assertEqual(health["status"], "error")
        self.assertEqual(health["checks"][0]["name"], "session_secret")

    def test_emergency_fallback_requires_internal_token(self):
        health = auth_providers.provider_health(
            self.external_values(OPENCLAW_INTERNAL_TOKEN="")
        )

        self.assertEqual(health["status"], "error")
        self.assertFalse(health["emergency_ready"])

    def test_external_health_fails_closed_for_invalid_configuration(self):
        health = auth_providers.provider_health(
            self.external_values(
                MANAGER_OAUTH_CLIENT_ID="", MANAGER_OAUTH_CLIENT_SECRET="top-secret-value"
            )
        )

        self.assertEqual(health["status"], "error")
        self.assertFalse(health["configured"])
        self.assertNotIn("top-secret-value", str(health))

    def test_endpoint_probe_treats_http_4xx_as_reachable(self):
        error = HTTPError("https://login.example.test", 401, "Unauthorized", {}, None)
        with patch.object(auth_providers, "urlopen", side_effect=error):
            health = auth_providers.provider_health(self.external_values(), probe=True)

        self.assertEqual(health["status"], "ok")
        self.assertTrue(all(
            check.get("http_status") == 401
            for check in health["checks"]
            if check["name"] in {"authorize", "token", "userinfo"}
        ))

    def test_endpoint_probe_reports_network_failure(self):
        with patch.object(auth_providers, "urlopen", side_effect=URLError("offline")):
            health = auth_providers.provider_health(self.external_values(), probe=True)

        self.assertEqual(health["status"], "error")
        self.assertTrue(any(check["status"] == "error" for check in health["checks"]))

    def test_endpoint_probe_reports_http_5xx_failure(self):
        error = HTTPError("https://login.example.test", 503, "Unavailable", {}, None)
        with patch.object(auth_providers, "urlopen", side_effect=error):
            health = auth_providers.provider_health(self.external_values(), probe=True)

        self.assertEqual(health["status"], "error")
        self.assertTrue(any(check.get("http_status") == 503 for check in health["checks"]))

    def test_user_id_profile_excludes_session_and_mobile(self):
        config = {
            "provider": "campus-uis",
            "auth_type": "oauth2",
            "subject_claim": "user_id",
            "userinfo_endpoint": "https://login.example.test/userinfo",
        }
        client = Mock()
        client.get.return_value.json.return_value = {
            "user_id": "12345",
            "user_name": "Alice",
            "email": "alice@example.test",
            "mobile": "13800000000",
            "sessionId": "uis-session",
        }

        identity = auth_providers.external_identity(client, {}, config)

        self.assertEqual(
            identity["profile"],
            {
                "user_id": "12345",
                "user_name": "Alice",
                "email": "alice@example.test",
            },
        )


if __name__ == "__main__":
    unittest.main()
