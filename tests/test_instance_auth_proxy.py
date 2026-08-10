#!/usr/bin/env python3

import importlib.util
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
APP_FILE = ROOT_DIR / "services" / "manager-web" / "instance_auth_app.py"


def load_app():
    flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda function: function

        def run(self, *args, **kwargs):
            pass

    flask.Flask = FakeFlask
    flask.request = types.SimpleNamespace(cookies={})
    spec = importlib.util.spec_from_file_location("instance_auth_app", APP_FILE)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("flask")
    sys.modules["flask"] = flask
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            del sys.modules["flask"]
        else:
            sys.modules["flask"] = previous
    return module


class InstanceAuthProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_app()

    def test_missing_cookie_is_unauthorized(self):
        with patch.object(self.module.request, "cookies", {}):
            self.assertEqual(self.module.authorize("instance-1"), ("", 401))

    def test_cookie_is_hashed_and_forwarded_with_dedicated_token(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        response = Response()
        with patch.object(
            self.module.request, "cookies", {"openclaw_manager_session": "secret"}
        ), patch.object(self.module, "CONTROL_TOKEN", "proxy-token"), patch.object(
            self.module.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(self.module.authorize("instance-1"), ("", 204))

        upstream = urlopen.call_args.args[0]
        self.assertIn(
            "token_hash=2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b",
            upstream.full_url,
        )
        self.assertNotIn("secret", upstream.full_url)
        self.assertEqual(upstream.get_header("Authorization"), "Bearer proxy-token")

    def test_control_denial_is_preserved_and_failure_is_closed(self):
        forbidden = urllib.error.HTTPError("url", 403, "", {}, None)
        unavailable = urllib.error.URLError("down")
        with patch.object(
            self.module.request, "cookies", {"openclaw_manager_session": "secret"}
        ), patch.object(self.module.urllib.request, "urlopen", side_effect=forbidden):
            self.assertEqual(self.module.authorize("instance-1"), ("", 403))
        with patch.object(
            self.module.request, "cookies", {"openclaw_manager_session": "secret"}
        ), patch.object(self.module.urllib.request, "urlopen", side_effect=unavailable):
            self.assertEqual(self.module.authorize("instance-1"), ("", 503))


if __name__ == "__main__":
    unittest.main()
