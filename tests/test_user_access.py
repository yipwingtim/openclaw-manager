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

        before_request = route
        context_processor = route
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
        # Keep the admin set predictable so our "attacker" actor is never admin.
        self.app_module.ADMIN_USERS = {"openclaw"}

    def tearDown(self):
        self._tmp.cleanup()

    def _with_actor(self, actor):
        headers = {"X-Remote-User": actor} if actor else {}
        return patch.object(self.app_module.request, "headers", headers)

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
        # Sanity check: when permission passes (actor == target), the existence
        # check runs and a missing user yields 404 rather than 403.
        with self._with_actor("alice"):
            response = self.app_module.user_detail("alice")

        self.assertEqual(response[1], 404)

    def test_admin_access_passes_permission_then_reveals_missing_user(self):
        with self._with_actor("openclaw"):
            response = self.app_module.user_detail("missing-admin-target")

        self.assertEqual(response[1], 404)


if __name__ == "__main__":
    unittest.main()
