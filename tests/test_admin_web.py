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
    control_client.get_activity_snapshots = lambda actor: []
    control_client.list_admin_instances = lambda: []
    control_client.list_admin_users = lambda: []
    control_client.list_platform_users = lambda **kwargs: {
        "users": [],
        "pagination": {"page": 1, "per_page": 20, "total": 0, "total_pages": 1},
    }
    control_client.update_admin_user_status = lambda *args: {}
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
    executor_client.collect_activity_snapshots = lambda actor, instance_ids: []
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

    def test_activity_page_filters_snapshots_without_exposing_details(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        snapshots = [{
            "instance_public_id": "instance-1", "instance_name": "Alice Lab",
            "owner_username": "alice", "owner_display_name": "张三",
            "product": "hermes", "metrics": {"sessions": 2}, "status": "success",
        }, {
            "instance_public_id": "instance-2", "instance_name": "Bob Lab",
            "owner_username": "bob", "owner_display_name": None,
            "product": "openclaw", "metrics": {}, "status": None,
        }]
        self.admin.request.args = {"product": "hermes", "q": "张三"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "get_activity_snapshots", return_value=snapshots
        ) as get_snapshots:
            template, context = self.admin.activity_page()
        self.assertEqual(template, "admin_activity.html")
        self.assertEqual([item["instance_public_id"] for item in context["snapshots"]], ["instance-1"])
        get_snapshots.assert_called_once_with("admin-1")
        source = (ROOT_DIR / "services" / "manager-web" / "templates" / "admin_activity.html").read_text(encoding="utf-8")
        for sensitive in ("prompt", "reasoning", "tool_name", "filename", "source_cursor"):
            self.assertNotIn(sensitive, source.lower())

    def test_activity_collection_batches_instance_ids(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        instances = [
            {"public_id": f"instance-{index}", "status": "active"}
            for index in range(101)
        ]
        self.admin.request.form = {}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=instances
        ), patch.object(
            self.admin.executor_client, "collect_activity_snapshots",
            side_effect=lambda _, ids: [
                {"instance_public_id": value, "status": "success"} for value in ids
            ],
        ) as collect, patch.object(self.admin, "url_for", return_value="activity-url"):
            response = self.admin.collect_activity()
        self.assertEqual(response, "activity-url")
        self.assertEqual([len(call.args[1]) for call in collect.call_args_list], [100, 1])

    def test_activity_collection_can_target_one_instance(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {"instance_public_id": "instance-2"}
        instances = [
            {"public_id": "instance-1", "status": "active"},
            {"public_id": "instance-2", "status": "active"},
        ]
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_instances", return_value=instances
        ), patch.object(
            self.admin.executor_client, "collect_activity_snapshots",
            return_value=[{"instance_public_id": "instance-2", "status": "success"}],
        ) as collect, patch.object(self.admin, "url_for", return_value="activity-url"):
            response = self.admin.collect_activity()
        self.assertEqual(response, "activity-url")
        collect.assert_called_once_with("admin-1", ["instance-2"])

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

    def test_admin_instances_filters_by_product(self):
        instances = [
            {"public_id": "openclaw-1", "instance_name": "OpenClaw", "product": "openclaw", "status": "active", "runtime_status": "running"},
            {"public_id": "hermes-1", "instance_name": "Hermes", "product": "hermes", "status": "active", "runtime_status": "running"},
        ]
        self.admin.request.args = {"status": "all", "product": "hermes"}

        context = self.admin.instance_list_context(instances)

        self.assertEqual(context["product_filter"], "hermes")
        self.assertEqual([item["public_id"] for item in context["instances"]], ["hermes-1"])

    def test_admin_instances_template_preserves_table_and_collapses_extra_actions(self):
        template = (
            ROOT_DIR / "services" / "manager-web" / "templates" / "admin_instances.html"
        ).read_text(encoding="utf-8")

        self.assertIn("data-instance-table", template)
        self.assertIn("select-current-page", template)
        self.assertIn("data-instance-actions", template)
        self.assertIn('(\"hermes\", \"Hermes\"), (\"evoscientist\", \"EvoScientist\")', template)
        self.assertIn("product={{ product_filter }}", template)
        self.assertIn("显示 {{ pagination.start }}-{{ pagination.end }}", template)
        self.assertIn("<th>访问认证</th><th>操作</th>", template)

    def test_admin_instances_shortens_evoscientist_digest_in_version_column(self):
        template = (
            ROOT_DIR / "services" / "manager-web" / "templates" / "admin_instances.html"
        ).read_text(encoding="utf-8")

        self.assertIn('instance.product == "evoscientist"', template)
        self.assertIn('instance.version.startswith("sha256:")', template)
        self.assertIn('title="{{ instance.version }}"', template)
        self.assertIn('{{ instance.version[:21] }}…', template)

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

    def test_authenticated_sidebar_always_shows_logout_button(self):
        template = (ROOT_DIR / "services" / "manager-web" / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('{% if current_user %}', template)
        self.assertNotIn('current_user and auth_provider == "local"', template)
        self.assertIn("退出登录", template)

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

    def test_platform_users_filters_by_provider_status_and_name(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        users = [
            {
                "public_id": "user-1", "username": "uis-alice", "display_name": "张三",
                "role": "user", "status": "active",
                "uis_user_id": "uis-123",
                "identity_providers": ["campus-uis"], "provisioning_source": "uis-import",
            },
            {
                "public_id": "user-2", "username": "local-bob", "display_name": "李四",
                "role": "user", "status": "disabled",
                "uis_user_id": None,
                "identity_providers": ["local"], "provisioning_source": "local",
            },
        ]
        pagination = {"page": 2, "per_page": 10, "total": 12, "total_pages": 2}
        self.admin.request.args = {
            "provider": "campus-uis", "status": "active", "q": "uis-123",
            "page": "2", "per_page": "10",
        }
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_platform_users",
            return_value={"users": users[:1], "pagination": pagination},
        ) as list_users:
            template, context = self.admin.platform_users()

        self.assertEqual(template, "admin_platform_users.html")
        self.assertEqual([user["public_id"] for user in context["users"]], ["user-1"])
        self.assertEqual(context["actor_public_id"], "admin-1")
        self.assertEqual(context["pagination"]["start"], 11)
        list_users.assert_called_once_with(
            provider="campus-uis", status="active", query="uis-123", page=2, per_page=10
        )

    def test_platform_user_status_update_includes_admin_actor(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {"status": "disabled"}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "update_admin_user_status"
        ) as update_status, patch.object(
            self.admin, "url_for", return_value="platform-users-url"
        ):
            response = self.admin.update_platform_user_status("user-1")

        self.assertEqual(response, "platform-users-url")
        update_status.assert_called_once_with("admin-1", "user-1", "disabled")

    def test_platform_users_navigation_and_template_hide_identity_details(self):
        base = (ROOT_DIR / "services" / "manager-web" / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        template = (
            ROOT_DIR / "services" / "manager-web" / "templates" / "admin_platform_users.html"
        ).read_text(encoding="utf-8")

        self.assertIn('href="/admin/platform-users"', base)
        self.assertIn("UIS user_id", template)
        self.assertIn("pagination.next_page", template)
        for sensitive_name in ("user.subject", "profile_json", "password_hash", "access_token"):
            self.assertNotIn(sensitive_name, template.lower())

    def test_admin_create_instance_does_not_load_all_users(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.args = {}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_users"
        ) as list_users:
            template, context = self.admin.create_instance_page()

        self.assertEqual(template, "admin_create_instance.html")
        self.assertNotIn("users", context)
        list_users.assert_not_called()

    def test_create_status_refresh_stays_on_page_during_proxy_restart(self):
        template = (
            ROOT_DIR
            / "services"
            / "manager-web"
            / "templates"
            / "admin_create_instance.html"
        ).read_text(encoding="utf-8")

        self.assertEqual(
            template.count('<a class="btn btn-oc" data-status-refresh'), 2
        )
        self.assertIn("event.preventDefault()", template)
        self.assertIn("fetch(link.href", template)

    def test_create_status_refresh_polls_running_jobs_every_two_seconds(self):
        template = (
            ROOT_DIR
            / "services"
            / "manager-web"
            / "templates"
            / "admin_create_instance.html"
        ).read_text(encoding="utf-8")

        self.assertIn('data-auto-refresh="true"', template)
        self.assertIn('selectattr("status", "in", ["queued", "running"])', template)
        self.assertIn("scheduleRefresh(2000)", template)
        self.assertIn("new AbortController()", template)
        self.assertIn("controller.abort()", template)
        self.assertIn("bindStatusRefresh()", template)

    def test_admin_create_instance_submits_structured_payload(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        self.admin.request.form = {
            "owner_identity_type": "campus-uis",
            "owner_identity": "12345",
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
                "owner_identity_type": "campus-uis",
                "owner_identity": "12345",
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

    def test_admin_batch_create_submits_hermes_and_evoscientist_rows(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(read=lambda: (
            b"owner_username,legacy_user_id,instance_name,product,version,confirm_latest,basic_auth_password,basic_auth_enabled\n"
            b"alice,alice-hermes,Alice Hermes,hermes,v2026.7.20,false,h-secret,true\n"
            b"alice,alice-evo,Alice Evo,evoscientist,latest,true,e-secret,true\n"
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
        ):
            response = self.admin.create_instance_batch()

        self.assertEqual(response, "batch-url")
        self.assertEqual(
            create_batch.call_args.args[0]["instances"],
            [
                {
                    "owner_user_public_id": "user-1",
                    "legacy_user_id": "alice-hermes",
                    "instance_name": "Alice Hermes",
                    "product": "hermes",
                    "version": "v2026.7.20",
                    "confirm_latest": False,
                    "basic_auth_enabled": True,
                    "basic_auth_password": "h-secret",
                },
                {
                    "owner_user_public_id": "user-1",
                    "legacy_user_id": "alice-evo",
                    "instance_name": "Alice Evo",
                    "product": "evoscientist",
                    "version": "latest",
                    "confirm_latest": True,
                    "basic_auth_enabled": True,
                    "basic_auth_password": "e-secret",
                },
            ],
        )

    def test_admin_batch_create_submits_owner_identities_without_listing_users(self):
        actor = {"public_id": "admin-1", "username": "admin", "role": "admin"}
        upload = types.SimpleNamespace(read=lambda: (
            b"owner_identity_type,owner_identity,legacy_user_id,instance_name,product,version,confirm_latest,basic_auth_password,basic_auth_enabled\n"
            b"local,alice,alice-hermes,Alice Hermes,hermes,,false,h-secret,true\n"
            b"campus-uis,12345,alice-evo,Alice Evo,evoscientist,latest,true,e-secret,true\n"
        ))
        self.admin.request.files = {"input_csv": upload}
        result = {"parent": {"request_id": "batch-1"}, "children": []}
        with patch.object(self.admin.web_common, "actor", return_value=actor), patch.object(
            self.admin.control_client, "list_admin_users"
        ) as list_users, patch.object(
            self.admin.control_client, "create_instance_batch", return_value=result
        ) as create_batch, patch.object(
            self.admin, "url_for", return_value="batch-url"
        ):
            response = self.admin.create_instance_batch()

        self.assertEqual(response, "batch-url")
        list_users.assert_not_called()
        self.assertEqual(create_batch.call_args.args[0]["instances"], [
            {
                "owner_identity_type": "local", "owner_identity": "alice",
                "legacy_user_id": "alice-hermes", "instance_name": "Alice Hermes",
                "product": "hermes", "basic_auth_enabled": True,
                "basic_auth_password": "h-secret", "confirm_latest": False,
            },
            {
                "owner_identity_type": "campus-uis", "owner_identity": "12345",
                "legacy_user_id": "alice-evo", "instance_name": "Alice Evo",
                "product": "evoscientist", "version": "latest",
                "basic_auth_enabled": True, "basic_auth_password": "e-secret",
                "confirm_latest": True,
            },
        ])

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
