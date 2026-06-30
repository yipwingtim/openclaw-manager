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


class BatchCreatePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = load_app_module()

    def test_parse_batch_create_csv_accepts_optional_password_and_auth_flag(self):
        with TemporaryDirectory() as public_dir:
            input_csv = Path(public_dir) / "input.csv"
            input_csv.write_text(
                "user_id,basic_auth_password,basic_auth_enabled\n"
                "alice,,false\n"
                "bob,secret,true\n",
                encoding="utf-8",
            )

            rows, errors = self.app_module.parse_batch_create_csv(input_csv)

            self.assertEqual(errors, [])
            self.assertEqual([row["user_id"] for row in rows], ["alice", "bob"])
            self.assertFalse(rows[0]["password_provided"])
            self.assertTrue(rows[1]["password_provided"])
            self.assertEqual(rows[0]["basic_auth_enabled"], "false")

    def test_parse_batch_create_csv_rejects_duplicate_and_invalid_user_id(self):
        with TemporaryDirectory() as public_dir:
            input_csv = Path(public_dir) / "input.csv"
            input_csv.write_text(
                "user_id,basic_auth_password,basic_auth_enabled\n"
                "alice,,true\n"
                "bad user,,true\n"
                "alice,,true\n",
                encoding="utf-8",
            )

            rows, errors = self.app_module.parse_batch_create_csv(input_csv)

            self.assertEqual(rows[1]["status"], "invalid")
            self.assertEqual(rows[2]["status"], "duplicate")
            self.assertEqual(len(errors), 2)

    def test_preflight_batch_create_blocks_existing_user_and_low_port_capacity(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            public_dir = root / "public"
            manager_dir = root / "manager"
            nginx_conf_dir = root / "nginx-conf"
            (public_dir / "users" / "existing").mkdir(parents=True)
            (manager_dir / "scripts").mkdir(parents=True)
            (manager_dir / "config").mkdir(parents=True)
            nginx_conf_dir.mkdir(parents=True)
            (manager_dir / "scripts" / "batch_create_users.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (manager_dir / "config" / "openclaw-manager.env").write_text(
                f"PORT_START=40001\nPORT_END=40001\nPORT_FILE={public_dir / 'ports.txt'}\n",
                encoding="utf-8",
            )
            (public_dir / "ports.txt").write_text("40001\n", encoding="utf-8")
            (nginx_conf_dir / "used.conf").write_text("server {\n  listen 40001 ssl;\n}\n", encoding="utf-8")
            input_csv = public_dir / "batches" / "input.csv"
            input_csv.parent.mkdir(parents=True)
            input_csv.write_text(
                "user_id,basic_auth_password,basic_auth_enabled\n"
                "existing,,true\n"
                "newuser,,true\n",
                encoding="utf-8",
            )

            self.app_module.PUBLIC_DIR = public_dir
            self.app_module.MANAGER_DIR = manager_dir
            self.app_module.NGINX_USERS_CONF_DIR = nginx_conf_dir

            rows, errors, capacity = self.app_module.preflight_batch_create(input_csv)

            self.assertEqual(rows[0]["status"], "exists")
            self.assertIn("existing: user already exists", errors)
            self.assertTrue(any("Not enough available ports" in error for error in errors))
            self.assertEqual(capacity["available"], 0)
            self.assertEqual(capacity["next"], 40001)


if __name__ == "__main__":
    unittest.main()
