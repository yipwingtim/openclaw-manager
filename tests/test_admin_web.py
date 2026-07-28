import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
ADMIN_APP = ROOT_DIR / "services" / "manager-web" / "admin_app.py"


def load_admin_app():
    flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def get(self, *args, **kwargs):
            return lambda func: func

        post = get

        def before_request(self, func):
            return func

        def context_processor(self, func):
            return func

    flask.Flask = FakeFlask
    flask.redirect = lambda value: value
    flask.render_template = lambda template, **context: (template, context)
    flask.request = types.SimpleNamespace(headers={}, args={}, form={})
    flask.url_for = lambda endpoint, **kwargs: endpoint

    control_client = types.ModuleType("control_client")

    class ControlError(Exception):
        pass

    control_client.ControlError = ControlError
    control_client.get_admin_metadata = lambda: {}
    control_client.create_execution_job = lambda payload: payload
    web_common = types.ModuleType("web_common")
    web_common.SESSION_SECRET = "test"
    web_common.require_internal_token = lambda: None
    web_common.require_csrf = lambda: None
    web_common.context = lambda: {}
    web_common.actor = lambda: None

    previous = {name: sys.modules.get(name) for name in ("flask", "control_client", "web_common")}
    sys.modules.update(flask=flask, control_client=control_client, web_common=web_common)
    spec = importlib.util.spec_from_file_location("manager_admin_web_app", ADMIN_APP)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        for name, value in previous.items():
            if value is None:
                del sys.modules[name]
            else:
                sys.modules[name] = value
    return module


class AdminWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = load_admin_app()

    def test_admin_metadata_renders_empty_summary_when_control_fails(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client,
            "get_admin_metadata",
            side_effect=self.admin.control_client.ControlError("control unavailable"),
        ):
            template, context = self.admin.metadata()

        self.assertEqual(template, "admin_metadata.html")
        self.assertEqual(context["error"], "control unavailable")
        self.assertEqual(context["counts"], {})
        self.assertEqual(context["instances"], [])
        self.assertEqual(context["operations"], [])

    def test_admin_basic_auth_queues_structured_boolean_action(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {"enabled": "false"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "create_execution_job"
        ) as create_job:
            response = self.admin.basic_auth("instance-1")

        payload = create_job.call_args.args[0]
        self.assertEqual(payload["instance_public_id"], "instance-1")
        self.assertEqual(payload["action"], "instance.set_basic_auth")
        self.assertEqual(payload["params"], {"enabled": False})
        self.assertNotIn("legacy_user_id", payload)
        self.assertEqual(response, "instances")

    def test_admin_basic_auth_redirects_control_error(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {"enabled": "true"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client,
            "create_execution_job",
            side_effect=self.admin.control_client.ControlError("control unavailable"),
        ), patch.object(self.admin, "url_for", return_value="error-url") as url_for:
            response = self.admin.basic_auth("instance-1")

        self.assertEqual(response, "error-url")
        url_for.assert_called_once_with("instances", error="control unavailable")


if __name__ == "__main__":
    unittest.main()
