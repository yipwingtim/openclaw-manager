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
    flask.Response = lambda body, **kwargs: (body, kwargs)
    flask.redirect = lambda value: value
    flask.render_template = lambda template, **context: (template, context)
    flask.request = types.SimpleNamespace(headers={}, args={}, form={}, files={})
    flask.url_for = lambda endpoint, **kwargs: endpoint

    control_client = types.ModuleType("control_client")

    class ControlError(Exception):
        pass

    control_client.ControlError = ControlError
    control_client.get_admin_metadata = lambda: {}
    control_client.list_admin_instances = lambda: []
    control_client.create_device_batch = lambda payload: payload
    control_client.get_device_batch = lambda request_id: {}
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

    def test_admin_version_queues_structured_action(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {
            "version": "2026.7.28",
            "restore_model_provider": "true",
        }
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "create_execution_job"
        ) as create_job:
            response = self.admin.update_version("instance-1")

        payload = create_job.call_args.args[0]
        self.assertEqual(payload["instance_public_id"], "instance-1")
        self.assertEqual(payload["action"], "instance.update_version")
        self.assertEqual(
            payload["params"],
            {"version": "2026.7.28", "restore_model_provider": True},
        )
        self.assertNotIn("legacy_user_id", payload)
        self.assertEqual(response, "instances")

    def test_admin_skill_queues_structured_action(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {"skill_id": "weather@1.0"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "create_execution_job"
        ) as create_job:
            response = self.admin.install_skill("instance-1")

        payload = create_job.call_args.args[0]
        self.assertEqual(payload["action"], "instance.install_skill")
        self.assertEqual(payload["params"], {"skill_id": "weather@1.0"})
        self.assertNotIn("legacy_user_id", payload)
        self.assertEqual(response, "instances")

    def test_admin_device_action_queues_structured_action(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {"action": "approve_latest"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "create_execution_job"
        ) as create_job:
            response = self.admin.devices("instance-1")

        payload = create_job.call_args.args[0]
        self.assertEqual(payload["instance_public_id"], "instance-1")
        self.assertEqual(payload["action"], "instance.approve_latest_device")
        self.assertEqual(payload["params"], {})
        self.assertNotIn("legacy_user_id", payload)
        self.assertEqual(response, "instances")

    def test_admin_retention_queues_structured_instance_action(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {"action": "delete"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "create_execution_job"
        ) as create_job:
            response = self.admin.retention("instance-1")

        payload = create_job.call_args.args[0]
        self.assertEqual(payload["instance_public_id"], "instance-1")
        self.assertEqual(payload["action"], "instance.delete")
        self.assertEqual(payload["params"], {})
        self.assertNotIn("legacy_user_id", payload)
        self.assertEqual(response, "instances")

    def test_admin_device_batch_resolves_legacy_ids_to_instance_uuids(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(read=lambda: b"user_id\nalice\nalice\n")
        self.admin.request.form = {"action": "approve"}
        self.admin.request.files = {"input_csv": upload}
        instances = [{"public_id": "instance-1", "legacy_user_id": "alice"}]
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=instances
        ), patch.object(
            self.admin.control_client, "create_device_batch",
            return_value={"parent": {"request_id": "batch-1"}, "children": []},
        ) as create_batch, patch.object(
            self.admin, "url_for", return_value="batch-url"
        ):
            response = self.admin.run_device_approvals()

        payload = create_batch.call_args.args[0]
        self.assertEqual(payload["actor_user_public_id"], "admin-1")
        self.assertEqual(payload["action"], "approve")
        self.assertEqual(payload["instance_public_ids"], ["instance-1"])
        self.assertEqual(response, "batch-url")

    def test_admin_device_batch_rejects_unknown_instance_without_submission(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(read=lambda: b"instance_public_id\nunknown\n")
        self.admin.request.form = {"action": "preview"}
        self.admin.request.files = {"input_csv": upload}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=[]
        ), patch.object(self.admin.control_client, "create_device_batch") as create_batch:
            response, status = self.admin.run_device_approvals()

        self.assertEqual(status, 400)
        self.assertIn("实例不存在", response[1]["error"])
        create_batch.assert_not_called()

    def test_admin_device_batch_rejects_explicit_request_id(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(
            read=lambda: b"user_id,request_id\nalice,device-request-1\n"
        )
        self.admin.request.form = {"action": "approve"}
        self.admin.request.files = {"input_csv": upload}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances"
        ) as list_instances, patch.object(
            self.admin.control_client, "create_device_batch"
        ) as create_batch:
            response, status = self.admin.run_device_approvals()

        self.assertEqual(status, 400)
        self.assertIn("仅支持审批最新请求", response[1]["error"])
        list_instances.assert_not_called()
        create_batch.assert_not_called()

    def test_admin_device_batch_downloads_structured_csv(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        result = {
            "children": [{
                "instance_public_id": "instance-1", "request_id": "batch-1:1",
                "status": "succeeded", "summary": "No pending request",
            }]
        }
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "get_device_batch", return_value=result
        ):
            body, options = self.admin.download_device_batch("batch-1")

        self.assertIn("instance_public_id,request_id,status,message", body)
        self.assertIn("instance-1,batch-1:1,succeeded,No pending request", body)
        self.assertEqual(options["mimetype"], "text/csv")


if __name__ == "__main__":
    unittest.main()
