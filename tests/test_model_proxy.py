import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
APP_FILE = ROOT_DIR / "services" / "model-proxy" / "app.py"
sys.path.insert(0, str(ROOT_DIR / "services"))


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
    def test_observed_content_closes_observation_after_stream(self):
        app = load_app()

        class Upstream:
            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield b"a"
                yield b"b"

        class Observation:
            def __init__(self):
                self.exit_args = None

            def __exit__(self, *args):
                self.exit_args = args

            def set_output(self, value):
                pass

        observation = Observation()
        self.assertEqual(list(app.observed_content(Upstream(), observation)), [b"a", b"b"])
        self.assertEqual(observation.exit_args, (None, None, None))

    def test_observed_content_records_stream_error(self):
        app = load_app()

        class Upstream:
            def iter_content(self, chunk_size):
                yield b"a"
                raise RuntimeError("stream failed")

        class Observation:
            def __init__(self):
                self.exit_args = None

            def __exit__(self, *args):
                self.exit_args = args

            def set_output(self, value):
                pass

        observation = Observation()
        with self.assertRaisesRegex(RuntimeError, "stream failed"):
            list(app.observed_content(Upstream(), observation))
        self.assertIs(observation.exit_args[0], RuntimeError)
        self.assertIsInstance(observation.exit_args[1], RuntimeError)

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

    def test_sse_response_is_summarized_for_observability(self):
        app = load_app()
        body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\ndata: [DONE]\n'
        output, usage = app.summarize_response(body, "text/event-stream")
        self.assertEqual(output["content"], "hi")
        self.assertEqual(output["finish_reason"], "stop")
        self.assertEqual(usage["total_tokens"], 3)
        self.assertEqual(app.normalize_usage(usage)["input"], 2)
        self.assertEqual(app.normalize_usage(usage)["output"], 1)


if __name__ == "__main__":
    unittest.main()
