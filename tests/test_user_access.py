#!/usr/bin/env python3

"""Access-control tests for manager-web user endpoints.

Regression coverage for username enumeration: every ``/users/<user_id>`` route
must run the permission check (``require_instance_access``) *before* revealing
whether the target user exists. An unauthorized request must therefore return
403 regardless of existence, so attackers cannot probe user ids via response
code differences (403 vs 404).
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))


def load_app_module():
    flask_stub = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}
            self.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)

        def route(self, *args, **kwargs):
            return lambda func: func

        def before_request(self, func):
            return func

        def context_processor(self, func):
            return func

        get = route
        post = route

    flask_stub.Flask = FakeFlask
    flask_stub.Response = object
    flask_stub.redirect = lambda *args, **kwargs: None
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.request = types.SimpleNamespace(
        headers={},
        args={},
        form={},
        files={},
        host="localhost",
        path="/",
    )
    flask_stub.send_file = lambda *args, **kwargs: None
    flask_stub.url_for = lambda endpoint, **kwargs: endpoint

    werkzeug_stub = types.ModuleType("werkzeug")
    werkzeug_utils_stub = types.ModuleType("werkzeug.utils")
    werkzeug_utils_stub.secure_filename = lambda value: value

    sys.modules.setdefault("flask", flask_stub)
    sys.modules.setdefault("werkzeug", werkzeug_stub)
    sys.modules.setdefault("werkzeug.utils", werkzeug_utils_stub)

    spec = importlib.util.spec_from_file_location("manager_web_app", MANAGER_WEB_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Every /users/<user_id> route handler plus any extra positional args the
# function expects beyond user_id. Extra args are never used because the
# permission check short-circuits before they are consulted.
USER_ENDPOINTS = [
    ("user_detail", ()),
    ("approve_latest", ()),
    ("refresh_devices", ()),
    ("upload_file", ()),
    ("user_wechat_bind_url", ()),
    ("user_wechat_bind_cancel", ()),
    ("download_workspace_file", ("workspace", "some/file.md")),
    ("delete_workspace_file", ("workspace", "some/file.md")),
]


class UserDetailAccessTests(unittest.TestCase):
    """Permission checks must run before user existence checks."""

    @classmethod
    def setUpClass(cls):
        cls.app_module = load_app_module()

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.public_dir = Path(self._tmp.name)
        self.app_module.PUBLIC_DIR = self.public_dir
        self.app_module.MANAGER_DIR = ROOT_DIR
        self.app_module.MANAGER_AUTH_PROVIDER = "nginx-basic"
        self.app_module.metadata_store.DB_FILE = self.public_dir / "manager.db"
        self.app_module.metadata_store.initialize(
            schema_file=ROOT_DIR / "db" / "schema.sql"
        )
        self.app_module.metadata_store.upsert_instance(
            user_id="alice",
            container_name="openclaw_alice",
            data_path=str(self.public_dir / "users" / "alice"),
        )
        alice = self.app_module.metadata_store.get_user_by_username("alice")
        self.app_module.metadata_store.upsert_identity(
            alice["id"], "nginx-basic", "alice", "alice"
        )
        for username, role in (("attacker", "user"), ("openclaw", "admin")):
            user = self.app_module.metadata_store.create_user(username)
            self.app_module.metadata_store.upsert_identity(
                user["id"], "nginx-basic", username, username
            )
            self.app_module.metadata_store.set_user_role(user["id"], role)
        # Keep the admin set predictable so our "attacker" actor is never admin.
        self.app_module.ADMIN_USERS = {"openclaw"}

    def tearDown(self):
        self._tmp.cleanup()

    def _with_actor(self, actor):
        headers = {"X-Remote-User": actor} if actor else {}
        return patch.object(self.app_module.request, "headers", headers)

    def test_user_routes_require_internal_proxy_token_when_configured(self):
        self.app_module.OPENCLAW_INTERNAL_TOKEN = "internal-secret"

        with patch.object(self.app_module.request, "path", "/users/alice"):
            with patch.object(self.app_module.request, "headers", {"X-Remote-User": "alice"}):
                response = self.app_module.require_internal_proxy_token()

        self.assertEqual(response[1], 403)

    def test_user_routes_accept_matching_internal_proxy_token(self):
        self.app_module.OPENCLAW_INTERNAL_TOKEN = "internal-secret"

        with patch.object(self.app_module.request, "path", "/users/alice"):
            with patch.object(
                self.app_module.request,
                "headers",
                {"X-Remote-User": "alice", "X-OpenClaw-Internal-Token": "internal-secret"},
            ):
                response = self.app_module.require_internal_proxy_token()

        self.assertIsNone(response)

    def test_all_user_endpoints_return_403_for_unauthorized_missing_user(self):
        # Core enumeration-prevention property: for every /users/<user_id>
        # route, an unauthorized actor hitting a non-existent user must get 403
        # (not 404), proving the permission gate fires before existence check.
        for func_name, extra_args in USER_ENDPOINTS:
            with self.subTest(endpoint=func_name):
                with self._with_actor("attacker"):
                    response = getattr(self.app_module, func_name)("victim", *extra_args)

                self.assertEqual(
                    response[1],
                    403,
                    f"{func_name} should return 403 for unauthorized access to a missing user",
                )

    def test_unauthorized_access_returns_403_for_existing_user(self):
        (self.public_dir / "users" / "victim").mkdir(parents=True)

        with self._with_actor("attacker"):
            response = self.app_module.user_detail("victim")

        self.assertEqual(response[1], 403)

    def test_missing_actor_returns_403_for_missing_user(self):
        with self._with_actor(None):
            response = self.app_module.user_detail("victim")

        self.assertEqual(response[1], 403)

    def test_self_access_passes_permission_then_reveals_missing_user(self):
        # Ownership is established through instances.owner_user_id rather than
        # by comparing the proxy username with a URL segment.
        with self._with_actor("alice"):
            response = self.app_module.user_detail("alice")

        self.assertEqual(response[1], 404)

    def test_admin_access_passes_permission_then_reveals_missing_user(self):
        with self._with_actor("openclaw"):
            response = self.app_module.user_detail("missing-admin-target")

        self.assertEqual(response[1], 404)

    def test_provider_switch_invalidates_local_session(self):
        alice = self.app_module.metadata_store.get_user_by_username("alice")
        self.app_module.metadata_store.upsert_identity(
            alice["id"], "local", "alice", "alice"
        )
        token = "local-session-token"
        self.app_module.metadata_store.create_session(
            self.app_module.token_hash(token),
            alice["id"],
            "local",
            "csrf-token",
            "2999-01-01T00:00:00+00:00",
        )
        self.app_module.MANAGER_AUTH_PROVIDER = "local"
        with patch.object(self.app_module.request, "cookies", {self.app_module.MANAGER_SESSION_COOKIE: token}, create=True):
            self.assertEqual(self.app_module.get_actor_user_record()["id"], alice["id"])

            self.app_module.MANAGER_AUTH_PROVIDER = "nginx-basic"
            with self._with_actor("alice"):
                self.assertEqual(self.app_module.get_actor_user_record()["id"], alice["id"])

            self.app_module.MANAGER_AUTH_PROVIDER = "local"
            self.assertIsNone(self.app_module.get_actor_user_record())


if __name__ == "__main__":
    unittest.main()
