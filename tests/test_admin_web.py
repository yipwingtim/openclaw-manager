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
    control_client.list_admin_users = lambda: []
    control_client.create_admin_instance = lambda payload: payload
    control_client.create_instance_batch = lambda payload: payload
    control_client.get_instance_batch = lambda request_id: {}
    control_client.create_model_provider_batch = lambda payload: payload
    control_client.get_model_provider_batch = lambda request_id: {}
    control_client.create_device_batch = lambda payload: payload
    control_client.get_device_batch = lambda request_id: {}
    control_client.create_action_batch = lambda payload: payload
    control_client.get_action_batch = lambda request_id: {}
    control_client.create_execution_job = lambda payload: payload
    executor_client = types.ModuleType("executor_client")

    class ExecutorError(Exception):
        pass

    executor_client.ExecutorError = ExecutorError
    executor_client.admin_instance_statuses = lambda actor, instance_ids: []
    web_common = types.ModuleType("web_common")
    web_common.SESSION_SECRET = "test"
    web_common.require_internal_token = lambda: None
    web_common.require_csrf = lambda: None
    web_common.context = lambda: {}
    web_common.actor = lambda: None

    previous = {name: sys.modules.get(name) for name in ("flask", "control_client", "executor_client", "web_common")}
    sys.modules.update(
        flask=flask, control_client=control_client,
        executor_client=executor_client, web_common=web_common,
    )
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

    def test_admin_instances_filters_searches_and_paginates(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        instances = [
            {
                "public_id": f"instance-{index}", "legacy_user_id": f"user-{index}",
                "instance_name": f"Instance {index:02d}", "product": "openclaw",
                "status": "deleted" if index == 25 else "active",
            }
            for index in range(1, 26)
        ]
        self.admin.request.args = {
            "status": "running", "q": "instance", "page": "2", "per_page": "10",
        }
        statuses = [
            {"instance_public_id": item["public_id"], "status": "running"}
            for item in instances if item["status"] != "deleted"
        ]
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=instances
        ), patch.object(
            self.admin.executor_client, "admin_instance_statuses", return_value=statuses
        ):
            template, context = self.admin.instances()

        self.assertEqual(template, "admin_instances.html")
        self.assertEqual(len(context["instances"]), 10)
        self.assertEqual(context["pagination"]["page"], 2)
        self.assertEqual(context["pagination"]["total"], 24)
        self.assertEqual(context["status_filter"], "running")
        self.assertEqual(context["query"], "instance")

    def test_admin_instances_template_preserves_table_and_collapses_extra_actions(self):
        template = (
            ROOT_DIR / "services" / "manager-web" / "templates" / "admin_instances.html"
        ).read_text(encoding="utf-8")

        self.assertIn("data-instance-table", template)
        self.assertIn("select-current-page", template)
        self.assertIn("data-instance-actions", template)
        self.assertIn("显示 {{ pagination.start }}-{{ pagination.end }}", template)
        self.assertIn("<th>访问认证</th><th>操作</th>", template)

    def test_admin_instances_fetches_runtime_statuses_in_bounded_batches(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        instances = [
            {"public_id": f"instance-{index}", "instance_name": str(index),
             "product": "openclaw", "status": "active"}
            for index in range(101)
        ]
        self.admin.request.args = {"status": "all"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=instances
        ), patch.object(
            self.admin.executor_client, "admin_instance_statuses", return_value=[]
        ) as statuses:
            self.admin.instances()

        self.assertEqual([len(call.args[1]) for call in statuses.call_args_list], [100, 1])

    def test_admin_sidebar_calls_model_provider_page_model_settings(self):
        template = (ROOT_DIR / "services" / "manager-web" / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("<span>模型设置</span>", template)
        self.assertNotIn("<span>模型供应商</span>", template)

    def test_admin_metadata_is_in_the_admin_sidebar(self):
        template = (ROOT_DIR / "services" / "manager-web" / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        admin_nav = template.split('{% if show_admin_instance_nav %}', 1)[1].split(
            '{% if show_global_admin_nav %}', 1
        )[0]
        self.assertIn('href="/admin/metadata"', admin_nav)
        self.assertNotIn('元数据与操作记录', (
            ROOT_DIR / "services" / "manager-web" / "templates" / "admin_instances.html"
        ).read_text(encoding="utf-8"))

    def test_admin_create_instance_lists_only_active_users(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        users = [
            {"public_id": "user-1", "username": "alice", "status": "active"},
            {"public_id": "user-2", "username": "bob", "status": "disabled"},
        ]
        self.admin.request.args = {}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_users", return_value=users
        ):
            template, context = self.admin.create_instance_page()

        self.assertEqual(template, "admin_create_instance.html")
        self.assertEqual(context["users"], [users[0]])

    def test_admin_create_instance_submits_structured_payload(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {
            "owner_user_public_id": "user-1",
            "legacy_user_id": "alice-instance",
            "instance_name": "Alice instance",
            "basic_auth_enabled": "true",
            "basic_auth_password": "secret",
        }
        result = {
            "instance": {"public_id": "instance-1"},
            "job": {"request_id": "create-1"},
        }
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "create_admin_instance", return_value=result
        ) as create_instance, patch.object(
            self.admin, "url_for", return_value="job-url"
        ) as url_for:
            response = self.admin.create_instance()

        self.assertEqual(response, "job-url")
        self.assertEqual(
            create_instance.call_args.args[0],
            {
                "request_id": create_instance.call_args.args[0]["request_id"],
                "actor_user_public_id": "admin-1",
                "owner_user_public_id": "user-1",
                "legacy_user_id": "alice-instance",
                "instance_name": "Alice instance",
                "product": "openclaw",
                "basic_auth_enabled": True,
                "basic_auth_password": "secret",
            },
        )
        self.assertTrue(create_instance.call_args.args[0]["request_id"].startswith("instance-create-"))
        url_for.assert_called_once_with("create_instance_job", request_id="create-1")

    def test_admin_batch_create_resolves_owner_and_submits_rows(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(read=lambda: (
            b"owner_username,legacy_user_id,instance_name,basic_auth_password,basic_auth_enabled\n"
            b"alice,alice-one,Alice One,secret,true\n"
        ))
        self.admin.request.files = {"input_csv": upload}
        users = [{"public_id": "user-1", "username": "alice", "status": "active"}]
        result = {"parent": {"request_id": "batch-1"}, "children": []}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_users", return_value=users
        ), patch.object(
            self.admin.control_client, "create_instance_batch", return_value=result
        ) as create_batch, patch.object(
            self.admin, "url_for", return_value="batch-url"
        ) as url_for:
            response = self.admin.create_instance_batch()

        payload = create_batch.call_args.args[0]
        self.assertEqual(payload["actor_user_public_id"], "admin-1")
        self.assertTrue(payload["request_id"].startswith("instance-batch-"))
        self.assertEqual(payload["instances"], [{
            "owner_user_public_id": "user-1",
            "legacy_user_id": "alice-one",
            "instance_name": "Alice One",
            "product": "openclaw",
            "basic_auth_enabled": True,
            "basic_auth_password": "secret",
        }])
        self.assertEqual(response, "batch-url")
        url_for.assert_called_once_with(
            "create_instance_batch_job", request_id="batch-1"
        )

    def test_admin_model_provider_batch_discards_legacy_api_key(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(read=lambda: (
            b"user_id,model_provider_id,model_id,model_base_url,model_api_key,model_alias\n"
            b"alice,openai,openai/gpt-5,https://models.example/v1,legacy-secret,GPT-5\n"
        ))
        self.admin.request.files = {"input_csv": upload}
        instances = [{
            "public_id": "instance-1", "legacy_user_id": "alice", "status": "active",
            "capabilities": ["batch_set_model_provider"],
        }]
        result = {"parent": {"request_id": "batch-1"}, "children": []}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=instances
        ), patch.object(
            self.admin.control_client, "create_model_provider_batch", return_value=result
        ) as create_batch, patch.object(
            self.admin, "url_for", return_value="batch-url"
        ):
            response = self.admin.create_model_provider_batch()

        payload = create_batch.call_args.args[0]
        self.assertEqual(response, "batch-url")
        self.assertEqual(payload["instances"], [{
            "instance_public_id": "instance-1",
            "model_provider_id": "openai",
            "model_id": "openai/gpt-5",
            "model_base_url": "https://models.example/v1",
            "model_alias": "GPT-5",
        }])
        self.assertNotIn("legacy-secret", repr(payload))

    def test_admin_model_provider_batch_rejects_instance_without_capability(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(read=lambda: (
            b"user_id,model_provider_id,model_id,model_base_url,model_api_key,model_alias\n"
            b"alice,gpustack,qwen3.6-35b,,,Qwen\n"
        ))
        self.admin.request.files = {"input_csv": upload}
        instances = [{
            "public_id": "instance-1", "legacy_user_id": "alice", "status": "active",
            "capabilities": [],
        }]
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=instances
        ), patch.object(
            self.admin.control_client, "create_model_provider_batch"
        ) as create_batch, patch.object(
            self.admin, "url_for", return_value="error-url"
        ):
            response = self.admin.create_model_provider_batch()

        self.assertEqual(response, "error-url")
        create_batch.assert_not_called()

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

    def test_admin_batch_action_submits_instance_uuids_and_configured_skill(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        values = {
            "action": "install_skill", "skill_id": "weather@1.0",
            "instance_public_ids": ["instance-1", "instance-2"],
        }
        self.admin.request.form = types.SimpleNamespace(
            get=lambda key, default="": values.get(key, default),
            getlist=lambda key: values.get(key, []),
        )
        with patch.dict(self.admin.os.environ, {"MANAGER_SKILL_PRESETS": "weather@1.0"}), \
             patch.object(self.admin.web_common, "actor", return_value=actor), \
             patch.object(self.admin.control_client, "create_action_batch",
                          return_value={"parent": {"request_id": "batch-1"}}) as create_batch, \
             patch.object(self.admin, "url_for", return_value="batch-url"):
            response = self.admin.run_action_batch()

        payload = create_batch.call_args.args[0]
        self.assertEqual(payload["actor_user_public_id"], "admin-1")
        self.assertEqual(payload["action"], "install_skill")
        self.assertEqual(payload["skill_id"], "weather@1.0")
        self.assertEqual(payload["instance_public_ids"], ["instance-1", "instance-2"])
        self.assertEqual(response, "batch-url")

    def test_admin_batch_action_rejects_unconfigured_skill(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        values = {
            "action": "install_skill", "skill_id": "unknown",
            "instance_public_ids": ["instance-1"],
        }
        self.admin.request.form = types.SimpleNamespace(
            get=lambda key, default="": values.get(key, default),
            getlist=lambda key: values.get(key, []),
        )
        with patch.dict(self.admin.os.environ, {"MANAGER_SKILL_PRESETS": "weather@1.0"}), \
             patch.object(self.admin.web_common, "actor", return_value=actor), \
             patch.object(self.admin.control_client, "create_action_batch") as create_batch:
            response, status = self.admin.run_action_batch()

        self.assertEqual(status, 400)
        self.assertIn("Skill", response[1]["error"])
        create_batch.assert_not_called()

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
