import importlib
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch


class UISAuthFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manager_web = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "services", "manager-web"
        )
        sys.path.insert(0, manager_web)
        flask_stub = types.ModuleType("flask")

        class Response:
            def __init__(self, location):
                self.location = location
                self.status_code = 302

            def set_cookie(self, *args, **kwargs):
                pass

            def delete_cookie(self, *args, **kwargs):
                pass

        cls.request = types.SimpleNamespace(
            cookies={}, headers={}, form={}, method="GET", path="/"
        )
        flask_stub.redirect = Response
        flask_stub.render_template = lambda *args, **kwargs: ""
        flask_stub.request = cls.request
        flask_stub.url_for = lambda endpoint, **kwargs: "/"
        previous = sys.modules.get("flask")
        sys.modules["flask"] = flask_stub
        try:
            cls.web_common = importlib.import_module("web_common")
        finally:
            if previous is None:
                del sys.modules["flask"]
            else:
                sys.modules["flask"] = previous
        cls.app = types.SimpleNamespace(make_response=lambda response: response)

    def test_callback_hashes_access_token_before_control_request(self):
        client = Mock()
        client.authorize_access_token.return_value = {
            "access_token": "uis-access-token"
        }
        config = {
            "provider": "campus-uis",
            "auth_type": "oauth2",
            "subject_claim": "user_id",
            "userinfo_endpoint": "https://login.example.test/userinfo",
        }
        identity = {
            "provider": "campus-uis",
            "subject": "12345",
            "external_username": "Alice",
            "profile": {"user_id": "12345"},
        }

        with patch.object(
            self.web_common, "external_client", return_value=(client, config)
        ), patch.object(
            self.web_common.auth_providers,
            "external_identity",
            return_value=identity,
        ), patch.object(
            self.web_common.control_client,
            "external_login",
            return_value={"role": "user"},
        ) as external_login:
            response = self.web_common.external_callback(self.app)

        payload = external_login.call_args.args[0]
        self.assertEqual(
            payload["external_token_hash"],
            self.web_common.token_hash("uis-access-token"),
        )
        self.assertNotIn("uis-access-token", repr(payload))
        self.assertEqual(response.status_code, 302)

    def test_passive_logout_hashes_internal_header_token(self):
        with patch.object(
            self.request, "headers", {"X-UIS-Logout-Token": "uis-access-token"}
        ), patch.object(
            self.web_common, "AUTH_PROVIDER", "campus-uis"
        ), patch.object(
            self.web_common.control_client, "delete_external_session"
        ) as delete_session:
            response = self.web_common.external_logout_callback()

        self.assertEqual(response, ("", 204))
        delete_session.assert_called_once_with(
            self.web_common.token_hash("uis-access-token")
        )

    def test_logout_redirects_to_provider_after_deleting_local_session(self):
        config = {
            "logout_url": "https://login.example.test/logout",
            "post_logout_redirect_uri": "https://manager.example.test/login",
        }
        with patch.object(
            self.request,
            "cookies",
            {"openclaw_manager_session": "manager-token"},
        ), patch.object(
            self.web_common, "AUTH_PROVIDER", "campus-uis"
        ), patch.object(
            self.web_common.auth_providers,
            "external_auth_config",
            return_value=config,
        ), patch.object(
            self.web_common.control_client, "delete_session"
        ) as delete_session, patch.object(
            self.web_common, "actor", return_value={"provider": "campus-uis"}
        ):
            response = self.web_common.logout()

        delete_session.assert_called_once_with(
            self.web_common.token_hash("manager-token")
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.startswith(config["logout_url"] + "?"))
        self.assertIn("redirectToLogin=true", response.location)

    def test_mixed_auth_exposes_local_form_and_uis_link(self):
        rendered = Mock()
        with patch.object(self.web_common, "AUTH_PROVIDER", "campus-uis"), patch.object(
            self.web_common, "LOCAL_AUTH_ENABLED", True
        ), patch.object(self.web_common, "render_template", return_value=rendered) as render:
            response = self.web_common.login_page(self.app)

        self.assertIs(response, rendered)
        self.assertEqual(render.call_args.kwargs["external_login_url"], "/auth/uis/login")

    def test_local_session_logout_does_not_redirect_to_uis(self):
        with patch.object(
            self.request, "cookies", {"openclaw_manager_session": "manager-token"}
        ), patch.object(self.web_common, "AUTH_PROVIDER", "campus-uis"), patch.object(
            self.web_common, "actor", return_value={"provider": "local"}
        ), patch.object(
            self.web_common.auth_providers,
            "external_auth_config",
            return_value={
                "logout_url": "https://login.example.test/logout",
                "post_logout_redirect_uri": "https://manager.example.test/login",
            },
        ), patch.object(self.web_common.control_client, "delete_session"):
            response = self.web_common.logout()

        self.assertEqual(response.location, "/")


if __name__ == "__main__":
    unittest.main()
