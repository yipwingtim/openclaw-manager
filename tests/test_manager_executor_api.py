#!/usr/bin/env python3

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
EXECUTOR_DIR = ROOT_DIR / "services" / "manager-executor"
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"


def load_executor_api():
    flask_stub = types.ModuleType("flask")

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def get_json(self):
            return self.payload

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def get(self, *args, **kwargs):
            return lambda func: func

        post = get
        delete = get

    flask_stub.Flask = FakeFlask
    flask_stub.jsonify = lambda payload: FakeResponse(payload)
    flask_stub.request = types.SimpleNamespace(headers={}, files={})
    flask_stub.send_file = lambda *args, **kwargs: None
    werkzeug_stub = types.ModuleType("werkzeug")
    werkzeug_utils_stub = types.ModuleType("werkzeug.utils")
    werkzeug_utils_stub.secure_filename = lambda value: value

    sys.path.insert(0, str(MANAGER_WEB_DIR))
    sys.path.insert(0, str(EXECUTOR_DIR))
    spec = importlib.util.spec_from_file_location(
        "manager_executor_api",
        EXECUTOR_DIR / "api.py",
    )
    module = importlib.util.module_from_spec(spec)
    previous_flask = sys.modules.get("flask")
    previous_werkzeug = sys.modules.get("werkzeug")
    previous_werkzeug_utils = sys.modules.get("werkzeug.utils")
    sys.modules["flask"] = flask_stub
    sys.modules["werkzeug"] = werkzeug_stub
    sys.modules["werkzeug.utils"] = werkzeug_utils_stub
    try:
        spec.loader.exec_module(module)
    finally:
        for name, previous in (
            ("flask", previous_flask),
            ("werkzeug", previous_werkzeug),
            ("werkzeug.utils", previous_werkzeug_utils),
        ):
            if previous is None:
                del sys.modules[name]
            else:
                sys.modules[name] = previous
    return module


class ManagerExecutorApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load_executor_api()

    def test_upload_rejects_product_without_file_capability_before_reading_file(self):
        instance = {
            "product": "evoscientist",
            "access_role": "owner",
            "data_path": "/should/not/be/used",
        }
        with patch.object(
            self.api,
            "runtime_instance",
            return_value=(instance, None),
        ), patch.object(self.api, "resolve_instance_file") as resolve_file:
            response, status = self.api.upload_file("instance-1")

        self.assertEqual(status, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "instance product does not support file upload"},
        )
        resolve_file.assert_not_called()

    def test_snapshot_rejects_unknown_product_without_constructing_adapter(self):
        instance = {"product": "unknown", "access_role": "owner"}
        with patch.object(
            self.api,
            "runtime_instance",
            return_value=(instance, None),
        ), patch.object(self.api, "get_adapter") as get_adapter:
            response, status = self.api.snapshot("instance-1")

        self.assertEqual(status, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "instance product does not support status"},
        )
        get_adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
