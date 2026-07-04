#!/usr/bin/env python3

import importlib.util
import sys
import types
import unittest
from pathlib import Path


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


class FormatBytesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = load_app_module()

    def test_bytes_below_1024_return_integer_with_b_unit(self):
        cases = {
            0: "0 B",
            1: "1 B",
            512: "512 B",
            1023: "1023 B",
        }
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertEqual(self.app_module.format_bytes(size), expected)

    def test_kb_values_use_one_decimal(self):
        cases = {
            1024: "1.0 KB",
            1536: "1.5 KB",
            2560: "2.5 KB",
            1024 * 1023: "1023.0 KB",
        }
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertEqual(self.app_module.format_bytes(size), expected)

    def test_mb_values_use_one_decimal(self):
        cases = {
            1024 * 1024: "1.0 MB",
            1024 * 1024 * 2.5: "2.5 MB",
            1024 * 1024 * 1023: "1023.0 MB",
        }
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertEqual(self.app_module.format_bytes(size), expected)

    def test_gb_values_use_one_decimal(self):
        cases = {
            1024 ** 3: "1.0 GB",
            1024 ** 3 * 2.5: "2.5 GB",
        }
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertEqual(self.app_module.format_bytes(size), expected)

    def test_values_above_gb_fall_back_to_gb_unit(self):
        # 单位列表以 GB 结尾，超过 GB 仍以 GB 返回
        result = self.app_module.format_bytes(1024 ** 4)
        self.assertTrue(result.endswith("GB"))
        self.assertNotIn("TB", result)


if __name__ == "__main__":
    unittest.main()
