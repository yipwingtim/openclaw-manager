#!/usr/bin/env python3

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


class LifecycleActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = load_app_module()

    def test_delete_runs_script_when_user_dir_is_missing(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)

            with patch.object(self.app_module, "run_command", return_value=(0, "deleted")) as run_command:
                code, output = self.app_module.run_instance_lifecycle_action("missing-user", "delete")

            self.assertEqual(code, 0)
            self.assertEqual(output, "deleted")
            command = run_command.call_args.args[0]
            self.assertTrue(str(command[0]).endswith("scripts/delete_user.sh"))
            self.assertEqual(command[1], "missing-user")

    def test_start_still_fails_when_user_dir_is_missing(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)

            with patch.object(self.app_module, "run_command") as run_command:
                code, output = self.app_module.run_instance_lifecycle_action("missing-user", "start")

            self.assertEqual(code, 1)
            self.assertEqual(output, "User not found: missing-user")
            run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
