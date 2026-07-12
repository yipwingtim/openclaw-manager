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


class FakeUpload:
    def __init__(self, filename, content="content"):
        self.filename = filename
        self.content = content

    def save(self, target):
        Path(target).write_text(self.content, encoding="utf-8")


class FakeThread:
    calls = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        FakeThread.calls.append(self)

    def start(self):
        self.started = True


class FakeForm(dict):
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


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


class LifecycleActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = load_app_module()

    def test_save_account_record_restricts_secret_file_permissions(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)

            self.app_module.save_account_record(
                {
                    "user_id": "alice",
                    "basic_auth_password": "secret",
                    "openclaw_token": "token",
                }
            )

            records_dir = Path(public_dir) / "accounts"
            record_path = records_dir / "alice_account.csv"
            self.assertEqual(records_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)

    def test_delete_runs_script_when_user_dir_is_missing(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            adapter = types.SimpleNamespace(delete=lambda user_id: (0, "deleted"))

            with patch.object(self.app_module, "get_instance_adapter", return_value=adapter):
                code, output = self.app_module.run_instance_lifecycle_action("missing-user", "delete")

            self.assertEqual(code, 0)
            self.assertEqual(output, "deleted")

    def test_start_still_fails_when_user_dir_is_missing(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)

            with patch.object(self.app_module, "run_command") as run_command:
                code, output = self.app_module.run_instance_lifecycle_action("missing-user", "start")

            self.assertEqual(code, 1)
            self.assertEqual(output, "User not found: missing-user")
            run_command.assert_not_called()

    def test_restore_runs_script_when_user_dir_is_missing(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            public_dir = root / "public"
            manager_dir = root / "manager"
            (manager_dir / "scripts").mkdir(parents=True)
            self.app_module.PUBLIC_DIR = public_dir
            self.app_module.MANAGER_DIR = manager_dir
            adapter = types.SimpleNamespace(restore=lambda user_id: (0, "restored"))

            with patch.object(self.app_module, "get_instance_adapter", return_value=adapter):
                code, output = self.app_module.run_instance_lifecycle_action("deleted-user", "restore")

            self.assertEqual(code, 0)
            self.assertEqual(output, "restored")

    def test_restore_rejects_existing_user_dir(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            (Path(public_dir) / "users" / "alice").mkdir(parents=True)

            with patch.object(self.app_module, "run_command") as run_command:
                code, output = self.app_module.run_instance_lifecycle_action("alice", "restore")

            self.assertEqual(code, 1)
            self.assertEqual(output, "User already exists: alice")
            run_command.assert_not_called()

    def test_list_deleted_users_reads_metadata_instances(self):
        instances = [
            {"user_id": "alice", "product": "openclaw", "port": 41001, "openclaw_version": "1.2.3"},
            {"user_id": "bad/user", "port": 41002, "openclaw_version": "1.2.3"},
        ]

        with patch.object(self.app_module.metadata_store, "initialize") as initialize:
            with patch.object(self.app_module.metadata_store, "list_instances", return_value=instances) as list_instances:
                users = self.app_module.list_active_users("deleted")

        initialize.assert_called_once()
        list_instances.assert_called_once_with(status="deleted")
        self.assertEqual(
            users,
            [
                {
                    "user_id": "alice",
                    "product": "openclaw",
                    "capabilities": self.app_module.get_instance_capabilities("openclaw"),
                    "status": "DELETED",
                    "port": 41001,
                    "openclaw_version": "1.2.3",
                    "access_url": "",
                    "basic_auth_enabled": None,
                }
            ],
        )

    def test_lifecycle_start_uses_instance_adapter(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            (Path(public_dir) / "users" / "alice").mkdir(parents=True)
            adapter = types.SimpleNamespace(start=lambda user_id: (0, f"started {user_id}"))

            with patch.object(self.app_module, "get_instance_adapter", return_value=adapter):
                code, output = self.app_module.run_instance_lifecycle_action("alice", "start")

            self.assertEqual(code, 0)
            self.assertEqual(output, "started alice")

    def test_lifecycle_uses_metadata_product_for_adapter_selection(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            (Path(public_dir) / "users" / "alice").mkdir(parents=True)
            adapter = types.SimpleNamespace(start=lambda user_id: (0, f"started {user_id}"))

            with patch.object(self.app_module, "get_instance_product", return_value="openclaw"):
                with patch.object(self.app_module, "get_instance_adapter", return_value=adapter) as get_adapter:
                    code, output = self.app_module.run_instance_lifecycle_action("alice", "start")

            get_adapter.assert_called_once_with("openclaw")
            self.assertEqual(code, 0)
            self.assertEqual(output, "started alice")

    def test_lifecycle_rejects_unsupported_product(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            (Path(public_dir) / "users" / "alice").mkdir(parents=True)

            with patch.object(self.app_module, "get_instance_product", return_value="hermes"):
                code, output = self.app_module.run_instance_lifecycle_action("alice", "start")

            self.assertEqual(code, 1)
            self.assertEqual(output, "Unsupported instance product: hermes")

    def test_registry_returns_evoscientist_adapter(self):
        adapter = self.app_module.get_instance_adapter("evoscientist")

        self.assertIsInstance(
            adapter,
            self.app_module.EvoScientistDockerAdapter,
        )
        self.assertTrue(adapter.supports("restart"))
        self.assertFalse(adapter.supports("delete"))

    def test_parse_bulk_user_ids_accepts_whitespace_commas_and_dedupes(self):
        user_ids = self.app_module.parse_bulk_user_ids(["alice bob", "alice,bad/user,carol"])

        self.assertEqual(user_ids, ["alice", "bob", "carol"])

    def test_filter_users_by_user_id_matches_case_insensitive_substring(self):
        users = [
            {"user_id": "testcreate001"},
            {"user_id": "TestCreate002"},
            {"user_id": "alice"},
        ]

        filtered = self.app_module.filter_users_by_user_id(users, "CREATE")

        self.assertEqual([user["user_id"] for user in filtered], ["testcreate001", "TestCreate002"])

    def test_paginate_items_slices_and_clamps_page(self):
        items = list(range(25))

        page_items, pagination = self.app_module.paginate_items(items, page="3", per_page="10")

        self.assertEqual(page_items, [20, 21, 22, 23, 24])
        self.assertEqual(pagination["page"], 3)
        self.assertEqual(pagination["total_pages"], 3)
        self.assertEqual(pagination["start"], 21)
        self.assertEqual(pagination["end"], 25)
        self.assertFalse(pagination["has_next"])

    def test_bulk_lifecycle_skips_instances_already_in_target_state(self):
        statuses = {"alice": "Up 2 hours", "bob": "STOPPED", "carol": "STOPPED"}

        with patch.object(self.app_module, "get_container_status", side_effect=lambda user_id: statuses[user_id]):
            with patch.object(
                self.app_module,
                "run_instance_lifecycle_action",
                side_effect=[(0, "started"), (1, "failed")],
            ) as run_action:
                with patch.object(self.app_module, "persist_lifecycle_metadata", return_value="") as persist_metadata:
                    summaries, errors = self.app_module.run_bulk_instance_lifecycle_action(
                        ["alice", "bob", "carol"],
                        "start",
                    )

        self.assertEqual(
            summaries,
            ["[SKIP] alice: already running", "[OK] bob: Start completed"],
        )
        self.assertEqual(errors, ["[ERROR] carol: Start failed: failed"])
        self.assertEqual(run_action.call_args_list[0].args, ("bob", "start"))
        self.assertEqual(run_action.call_args_list[1].args, ("carol", "start"))
        persist_metadata.assert_called_once_with("bob", "start", "started")

    def test_bulk_stop_skips_stopped_instances(self):
        statuses = {"alice": "STOPPED", "bob": "Up 5 minutes"}

        with patch.object(self.app_module, "get_container_status", side_effect=lambda user_id: statuses[user_id]):
            with patch.object(self.app_module, "run_instance_lifecycle_action", return_value=(0, "stopped")) as run_action:
                with patch.object(self.app_module, "persist_lifecycle_metadata", return_value="") as persist_metadata:
                    summaries, errors = self.app_module.run_bulk_instance_lifecycle_action(["alice", "bob"], "stop")

        self.assertEqual(summaries, ["[SKIP] alice: already stopped", "[OK] bob: Stop completed"])
        self.assertEqual(errors, [])
        run_action.assert_called_once_with("bob", "stop")
        persist_metadata.assert_called_once_with("bob", "stop", "stopped")

    def test_admin_lifecycle_starts_background_job(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            FakeThread.calls = []

            with patch.object(self.app_module, "require_admin", return_value=None):
                with patch.object(self.app_module.request, "form", {"action": "delete"}):
                    with patch.object(self.app_module, "get_actor_user", return_value="openclaw"):
                        with patch.object(self.app_module.threading, "Thread", FakeThread):
                            with patch.object(self.app_module, "redirect", lambda value: value):
                                response = self.app_module.admin_instance_lifecycle("alice")

            self.assertEqual(response, "admin_users")
            self.assertEqual(len(FakeThread.calls), 1)
            self.assertTrue(FakeThread.calls[0].started)
            self.assertEqual(FakeThread.calls[0].target, self.app_module.run_lifecycle_action_job)
            self.assertEqual(FakeThread.calls[0].args, ("alice", "delete", "openclaw"))

    def test_admin_bulk_lifecycle_starts_background_job(self):
        FakeThread.calls = []

        with patch.object(self.app_module, "require_admin", return_value=None):
            with patch.object(
                self.app_module.request,
                "form",
                FakeForm({
                    "action": "start",
                    "status": "all",
                    "page": "1",
                    "per_page": "20",
                    "user_id": "",
                    "user_ids": ["alice", "bob"],
                }),
            ):
                with patch.object(self.app_module, "get_actor_user", return_value="openclaw"):
                    with patch.object(self.app_module.threading, "Thread", FakeThread):
                        with patch.object(self.app_module, "redirect", lambda value: value):
                            response = self.app_module.admin_bulk_instance_lifecycle()

        self.assertEqual(response, "admin_users")
        self.assertEqual(len(FakeThread.calls), 1)
        self.assertTrue(FakeThread.calls[0].started)
        self.assertEqual(FakeThread.calls[0].target, self.app_module.run_bulk_lifecycle_action_job)
        self.assertEqual(FakeThread.calls[0].args, (["alice", "bob"], "start", "openclaw"))

    def test_admin_version_update_starts_background_job(self):
        FakeThread.calls = []

        with patch.object(self.app_module, "require_admin", return_value=None):
            with patch.object(
                self.app_module.request,
                "form",
                {"version": "2026.5.26", "restore_model_provider": "true"},
            ):
                with patch.object(self.app_module, "get_actor_user", return_value="openclaw"):
                    with patch.object(self.app_module.threading, "Thread", FakeThread):
                        with patch.object(self.app_module, "redirect", lambda value: value):
                            response = self.app_module.admin_update_instance_version("alice")

        self.assertEqual(response, "admin_users")
        self.assertEqual(len(FakeThread.calls), 1)
        self.assertTrue(FakeThread.calls[0].started)
        self.assertEqual(FakeThread.calls[0].target, self.app_module.run_instance_version_update_job)
        self.assertEqual(FakeThread.calls[0].args, ("alice", "2026.5.26", True, "openclaw"))

    def test_upload_file_rejects_unsupported_extension(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            upload = FakeUpload("script.sh")

            with patch.object(self.app_module.request, "files", {"file": upload}):
                with patch.object(self.app_module, "redirect_to_user_dashboard", return_value="redirected") as redirect_dashboard:
                    response = self.app_module.upload_file_for_user("alice")

            self.assertEqual(response, "redirected")
            self.assertIn("Unsupported file type", redirect_dashboard.call_args.kwargs["error"])
            self.assertFalse((Path(public_dir) / "users" / "alice" / "uploads" / "script.sh").exists())

    def test_upload_file_accepts_supported_extension(self):
        with TemporaryDirectory() as public_dir:
            self.app_module.PUBLIC_DIR = Path(public_dir)
            upload = FakeUpload("notes.txt")

            with patch.object(self.app_module.request, "files", {"file": upload}):
                with patch.object(self.app_module, "redirect_to_user_dashboard", return_value="redirected"):
                    with patch.object(self.app_module, "persist_operation_metadata", return_value=""):
                        response = self.app_module.upload_file_for_user("alice")

            target = Path(public_dir) / "users" / "alice" / "uploads" / "notes.txt"
            self.assertEqual(response, "redirected")
            self.assertEqual(target.read_text(encoding="utf-8"), "content")


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

    def test_parse_batch_model_provider_csv_accepts_expected_header(self):
        with TemporaryDirectory() as public_dir:
            input_csv = Path(public_dir) / "set_model_provider.csv"
            input_csv.write_text(
                "user_id,model_provider_id,model_id,model_base_url,model_api_key,model_alias\n"
                "alice,openai,openai/gpt-4.1,,,GPT 4.1\n",
                encoding="utf-8",
            )

            rows, errors = self.app_module.parse_batch_model_provider_csv(input_csv)

            self.assertEqual(errors, [])
            self.assertEqual(rows[0]["user_id"], "alice")
            self.assertEqual(rows[0]["model_provider_id"], "openai")
            self.assertEqual(rows[0]["model_id"], "openai/gpt-4.1")
            self.assertEqual(rows[0]["model_alias"], "GPT 4.1")

    def test_preflight_batch_model_provider_blocks_missing_user_and_stopped_container(self):
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            public_dir = root / "public"
            manager_dir = root / "manager"
            (public_dir / "users" / "stopped").mkdir(parents=True)
            (manager_dir / "scripts").mkdir(parents=True)
            (manager_dir / "scripts" / "batch_set_model_provider.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            input_csv = public_dir / "batches" / "set_model_provider.csv"
            input_csv.parent.mkdir(parents=True)
            input_csv.write_text(
                "user_id,model_provider_id,model_id,model_base_url,model_api_key,model_alias\n"
                "missing,openai,openai/gpt-4.1,,,GPT 4.1\n"
                "stopped,openai,openai/gpt-4.1,,,GPT 4.1\n"
                "badconfig,,openai/gpt-4.1,,,GPT 4.1\n",
                encoding="utf-8",
            )

            self.app_module.PUBLIC_DIR = public_dir
            self.app_module.MANAGER_DIR = manager_dir

            with patch.object(self.app_module, "get_container_status", return_value="STOPPED"):
                rows, errors = self.app_module.preflight_batch_model_provider(input_csv)

            self.assertEqual(rows[0]["status"], "missing_user")
            self.assertEqual(rows[1]["status"], "container_stopped")
            self.assertEqual(rows[2]["status"], "invalid")
            self.assertIn("missing: user not found", errors)
            self.assertIn("stopped: container is not running", errors)
            self.assertTrue(any("missing required model config" in error for error in errors))

    def test_batch_model_provider_adapter_writes_result(self):
        with TemporaryDirectory() as public_dir:
            input_csv = Path(public_dir) / "input.csv"
            output_csv = Path(public_dir) / "results.csv"
            input_csv.write_text("input", encoding="utf-8")
            output_csv.write_text("result", encoding="utf-8")
            adapter = self.app_module.OpenClawDockerAdapter(
                manager_dir=Path(public_dir),
                public_dir=Path(public_dir),
                nginx_users_conf_dir=Path(public_dir) / "nginx",
                nginx_compose_dir=Path(public_dir) / "compose",
                nginx_container_name="nginx",
            )

            with patch.object(adapter, "run_command", return_value=(0, "updated")) as run_command:
                code, output = adapter.batch_set_model_provider(input_csv, output_csv, timeout=123)

            self.assertEqual(code, 0)
            self.assertEqual(output, "updated")
            command = run_command.call_args.args[0]
            self.assertTrue(str(command[0]).endswith("scripts/batch_set_model_provider.sh"))
            self.assertEqual(command[1:], [str(input_csv), str(output_csv)])

    def test_adapter_create_runs_create_script(self):
        with TemporaryDirectory() as public_dir:
            adapter = self.app_module.OpenClawDockerAdapter(
                manager_dir=Path(public_dir),
                public_dir=Path(public_dir),
                nginx_users_conf_dir=Path(public_dir) / "nginx",
                nginx_compose_dir=Path(public_dir) / "compose",
                nginx_container_name="nginx",
            )

            with patch.object(adapter, "run_command", return_value=(0, "created")) as run_command:
                code, output = adapter.create("alice", "true", "secret", timeout=123)

            self.assertEqual(code, 0)
            self.assertEqual(output, "created")
            command = run_command.call_args.args[0]
            self.assertTrue(str(command[0]).endswith("scripts/create_user.sh"))
            self.assertEqual(command[1:], ["alice", "--basic-auth-enabled", "true", "--skip-nginx-reload", "--password", "secret"])

    def test_adapter_batch_create_runs_batch_create_script(self):
        with TemporaryDirectory() as public_dir:
            input_csv = Path(public_dir) / "input.csv"
            output_csv = Path(public_dir) / "results.csv"
            adapter = self.app_module.OpenClawDockerAdapter(
                manager_dir=Path(public_dir),
                public_dir=Path(public_dir),
                nginx_users_conf_dir=Path(public_dir) / "nginx",
                nginx_compose_dir=Path(public_dir) / "compose",
                nginx_container_name="nginx",
            )

            with patch.object(adapter, "run_command", return_value=(0, "created")) as run_command:
                code, output = adapter.batch_create(input_csv, output_csv, timeout=123, skip_nginx_refresh=True)

            self.assertEqual(code, 0)
            self.assertEqual(output, "created")
            command = run_command.call_args.args[0]
            self.assertTrue(str(command[0]).endswith("scripts/batch_create_users.sh"))
            self.assertEqual(command[1:], [str(input_csv), str(output_csv), "--skip-nginx-refresh"])

    def test_adapter_update_version_runs_update_script(self):
        with TemporaryDirectory() as public_dir:
            adapter = self.app_module.OpenClawDockerAdapter(
                manager_dir=Path(public_dir),
                public_dir=Path(public_dir),
                nginx_users_conf_dir=Path(public_dir) / "nginx",
                nginx_compose_dir=Path(public_dir) / "compose",
                nginx_container_name="nginx",
            )

            with patch.object(adapter, "run_command", return_value=(0, "updated")) as run_command:
                code, output = adapter.update_version("alice", "2026.5.26", restore_model_provider=True, timeout=123)

            self.assertEqual(code, 0)
            self.assertEqual(output, "updated")
            command = run_command.call_args.args[0]
            self.assertTrue(str(command[0]).endswith("scripts/update_instance_version.sh"))
            self.assertEqual(command[1:], ["alice", "2026.5.26", "--restore-model-provider"])
            self.assertEqual(run_command.call_args.kwargs["timeout"], 123)


if __name__ == "__main__":
    unittest.main()
