import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
APP_FILE = ROOT_DIR / "services" / "model-proxy" / "app.py"


def load_app():
    flask = types.ModuleType("flask")
    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
        def route(self, *args, **kwargs):
            return lambda function: function
        def get(self, *args, **kwargs):
            return lambda function: function
    flask.Flask = FakeFlask
    flask.Response = object
    flask.jsonify = lambda *args, **kwargs: None
    flask.request = types.SimpleNamespace()
    requests = types.ModuleType("requests")
    requests.request = lambda *args, **kwargs: None
    requests.RequestException = Exception
    sys.modules["flask"] = flask
    sys.modules["requests"] = requests
    spec = importlib.util.spec_from_file_location("model_proxy_app", APP_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelProxyTests(unittest.TestCase):
    def test_response_headers_drop_body_encoding_and_transport_headers(self):
        app = load_app()

        class Upstream:
            headers = {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Content-Length": "42",
                "Transfer-Encoding": "chunked",
                "Connection": "keep-alive",
                "X-Upstream": "ok",
            }

        self.assertEqual(
            app.response_headers(Upstream()),
            [("Content-Type", "application/json"), ("X-Upstream", "ok")],
        )

    def test_response_headers_preserve_content_type_for_sse(self):
        app = load_app()

        class Upstream:
            headers = {
                "Content-Type": "text/event-stream",
                "Content-Encoding": "gzip",
                "Content-Length": "999",
            }

        self.assertEqual(
            app.response_headers(Upstream()),
            [("Content-Type", "text/event-stream")],
        )


if __name__ == "__main__":
    unittest.main()
