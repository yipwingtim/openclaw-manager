#!/usr/bin/env python3

import importlib.util
import json
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch


ROOT_DIR = Path(__file__).resolve().parents[1]
EXECUTOR_FILE = ROOT_DIR / "services" / "manager-executor" / "executor.py"
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"


def load_executor():
    sys.path.insert(0, str(MANAGER_WEB_DIR))
    spec = importlib.util.spec_from_file_location("manager_executor", EXECUTOR_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManagerExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executor = load_executor()

    def test_run_once_retries_serial_job_and_records_success(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.start"},
            "instance": {
                "public_id": "instance-1",
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "STOPPED"
        adapter.start.side_effect = [(1, "temporary failure"), (0, "started")]

        worked = self.executor.run_once(
            control,
            lambda product: adapter,
            max_attempts=2,
        )

        self.assertTrue(worked)
        adapter.start.assert_called_with(control.claim.return_value["instance"])
        self.assertEqual(adapter.start.call_count, 2)
        self.assertEqual(
            control.update.call_args_list[-1].args,
            ("request-1", "succeeded"),
        )
        self.assertEqual(control.update.call_args_list[-1].kwargs["output"], "started")

    def test_registry_returns_hermes_adapter(self):
        adapter = self.executor.get_adapter("hermes")

        self.assertIsInstance(adapter, self.executor.HermesDockerAdapter)
        self.assertTrue(adapter.supports("restart"))
        self.assertTrue(adapter.supports("delete"))
        self.assertTrue(adapter.supports("restore"))
        self.assertTrue(adapter.supports("update_version"))

    def test_run_once_creates_instance_once_and_consumes_secret(self):
        control = Mock()
        instance = {
            "public_id": "instance-1", "product": "openclaw",
            "legacy_user_id": "alice", "runtime_identifier": "openclaw_alice",
            "basic_auth_enabled": True, "status": "provisioning",
        }
        adapter = Mock()
        adapter.manager_dir = Path("/manager")
        adapter.nginx_container_name = "nginx"
        adapter.supports.return_value = True
        adapter.create.return_value = (0, "password and token must be discarded")
        adapter.run_command.return_value = (0, "nginx compose updated")
        adapter.reload_nginx.return_value = (0, "nginx reloaded")
        with tempfile.TemporaryDirectory() as directory:
            public_dir = Path(directory)
            secret_dir = public_dir / ".manager-secrets"
            secret_dir.mkdir()
            secret_path = secret_dir / "secret"
            secret_path.write_text("secret-password", encoding="utf-8")
            config_dir = public_dir / "users" / "alice" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "openclaw.json").write_text(
                json.dumps({"gateway": {"auth": {"token": "runtime-token"}}}),
                encoding="utf-8",
            )
            nginx_dir = public_dir / "nginx"
            nginx_dir.mkdir()
            (nginx_dir / "alice.conf").write_text("listen 41001 ssl;\n", encoding="utf-8")
            control.claim.return_value = {
                "job": {"request_id": "create-1", "action": "instance.create",
                        "params": {"secret_path": str(secret_path)}},
                "instance": instance,
            }
            with patch.object(self.executor, "PUBLIC_DIR", public_dir), patch.object(
                self.executor, "PROVISIONING_SECRET_DIR", secret_dir
            ), patch.dict(
                self.executor.os.environ,
                {"NGINX_USERS_CONF_DIR": str(nginx_dir), "PUBLIC_HOST": "example.test",
                 "OPENCLAW_VERSION": "2026.6.6",
                 "NGINX_HTPASSWD_FILE_IN_CONTAINER": "/etc/nginx/auth/.htpasswd"},
            ):
                self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.create.assert_called_once_with(
            instance, "true", "secret-password", skip_nginx_reload=True,
            skip_metadata_write=True,
        )
        adapter.reload_nginx.assert_called_once()
        connect = adapter.run_command.call_args_list[1]
        self.assertIn("connect_shared_services_to_tenant_networks", connect.args[0][2])
        self.assertEqual(connect.args[0][-2:], ["nginx", "openclaw-model-proxy"])
        self.assertFalse(secret_path.exists())
        self.assertEqual(control.update.call_args.args, ("create-1", "succeeded"))
        self.assertEqual(control.update.call_args.kwargs["output"], "instance created")
        self.assertEqual(control.update.call_args.kwargs["result"]["openclaw_token"], "runtime-token")

    def test_run_once_passes_requested_creation_version(self):
        control = Mock()
        instance = {
            "public_id": "instance-1", "product": "openclaw",
            "legacy_user_id": "alice", "runtime_identifier": "openclaw_alice",
            "basic_auth_enabled": True, "status": "provisioning",
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.create.return_value = (1, "not used")
        with tempfile.TemporaryDirectory() as directory:
            secret_dir = Path(directory)
            secret_path = secret_dir / "secret"
            secret_path.write_text("secret-password", encoding="utf-8")
            control.claim.return_value = {
                "job": {"request_id": "create-version", "action": "instance.create",
                        "params": {"secret_path": str(secret_path), "version": "2026.7.28"}},
                "instance": instance,
            }
            with patch.object(self.executor, "PROVISIONING_SECRET_DIR", secret_dir):
                self.executor.run_once(control, lambda product: adapter)

        self.assertEqual(adapter.create.call_args.kwargs["version"], "2026.7.28")

    def test_openclaw_creation_result_rejects_symlinked_config(self):
        with tempfile.TemporaryDirectory() as directory:
            public_dir = Path(directory)
            config_dir = public_dir / "users" / "alice" / "config"
            config_dir.mkdir(parents=True)
            outside = public_dir / "outside.json"
            outside.write_text(json.dumps({"gateway": {"auth": {"token": "outside"}}}))
            (config_dir / "openclaw.json").symlink_to(outside)
            nginx_dir = public_dir / "nginx"
            nginx_dir.mkdir()
            (nginx_dir / "alice.conf").write_text("listen 41001 ssl;\n")
            instance = {"legacy_user_id": "alice", "_creation_version": "2026.6.6"}

            with patch.object(self.executor, "PUBLIC_DIR", public_dir), patch.dict(
                self.executor.os.environ,
                {"NGINX_USERS_CONF_DIR": str(nginx_dir), "PUBLIC_HOST": "example.test",
                 "NGINX_HTPASSWD_FILE_IN_CONTAINER": "/etc/nginx/auth/.htpasswd"},
            ):
                with self.assertRaises(OSError):
                    self.executor.openclaw_creation_result(instance)

    def test_openclaw_creation_result_includes_control_ui_base_path(self):
        with tempfile.TemporaryDirectory() as directory:
            public_dir = Path(directory)
            config_dir = public_dir / "users" / "alice" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "openclaw.json").write_text(json.dumps({
                "gateway": {"auth": {"token": "runtime-token"},
                             "controlUi": {"basePath": "/openclaw/alice"}}
            }))
            nginx_dir = public_dir / "nginx"
            nginx_dir.mkdir()
            (nginx_dir / "alice.conf").write_text("listen 41001 ssl;\n")
            with patch.object(self.executor, "PUBLIC_DIR", public_dir), patch.dict(
                self.executor.os.environ,
                {"NGINX_USERS_CONF_DIR": str(nginx_dir), "PUBLIC_HOST": "example.test",
                 "NGINX_HTPASSWD_FILE_IN_CONTAINER": "/etc/nginx/auth/.htpasswd"},
            ):
                result = self.executor.openclaw_creation_result({
                    "legacy_user_id": "alice", "_creation_version": "2026.6.6"
                })
            self.assertEqual(result["access_url"], "https://example.test:41001/openclaw/alice")

    def test_run_once_creates_hermes_and_configures_ingress(self):
        control = Mock()
        instance = {
            "public_id": "instance-1", "product": "hermes",
            "legacy_user_id": "alice", "runtime_identifier": "hermes_alice",
            "data_path": "/data/hermes/alice", "basic_auth_enabled": True,
            "status": "provisioning",
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.create.side_effect = lambda *args, **kwargs: (
            instance.__setitem__("_created_port", 39119) or (0, "created")
        )
        adapter.configure_ingress.return_value = (0, "published")
        with tempfile.TemporaryDirectory() as directory:
            secret_dir = Path(directory)
            secret_path = secret_dir / "secret"
            secret_path.write_text("password", encoding="utf-8")
            control.claim.return_value = {
                "job": {"request_id": "create-1", "action": "instance.create",
                        "params": {"secret_path": str(secret_path)}},
                "instance": instance,
            }
            with patch.object(self.executor, "PROVISIONING_SECRET_DIR", secret_dir), patch.dict(
                self.executor.os.environ, {"PUBLIC_HOST": "example.test"}
            ):
                self.executor.run_once(control, lambda product: adapter)

        adapter.configure_ingress.assert_called_once_with(instance)
        result = control.update.call_args.kwargs["result"]
        self.assertEqual(result["access_url"], "https://example.test:39119")
        self.assertEqual(result["openclaw_token"], "")

    def test_run_once_rolls_back_created_resources_when_nginx_update_fails(self):
        control = Mock()
        instance = {
            "product": "openclaw", "legacy_user_id": "alice",
            "runtime_identifier": "openclaw_alice", "status": "provisioning",
            "basic_auth_enabled": True,
        }
        adapter = Mock()
        adapter.manager_dir = Path("/manager")
        adapter.supports.return_value = True
        adapter.create.return_value = (0, "created")
        adapter.run_command.side_effect = [(1, "nginx failed"), (0, "deleted")]
        with tempfile.TemporaryDirectory() as directory:
            secret_dir = Path(directory)
            secret_path = secret_dir / "secret"
            secret_path.write_text("password", encoding="utf-8")
            control.claim.return_value = {
                "job": {"request_id": "create-1", "action": "instance.create",
                        "params": {"secret_path": str(secret_path)}},
                "instance": instance,
            }
            with patch.object(self.executor, "PROVISIONING_SECRET_DIR", secret_dir):
                self.executor.run_once(control, lambda product: adapter)

        self.assertEqual(adapter.run_command.call_count, 2)
        rollback = adapter.run_command.call_args_list[1]
        self.assertTrue(str(rollback.args[0][0]).endswith("scripts/delete_user.sh"))
        self.assertEqual(rollback.kwargs["env"]["OPENCLAW_SKIP_METADATA_WRITE"], "1")
        self.assertEqual(control.update.call_args.args, ("create-1", "failed"))
        self.assertIn("recycle bin", control.update.call_args.kwargs["output"])
        self.assertIn("nginx failed", control.update.call_args.kwargs["output"])

    def test_run_once_rolls_back_when_shared_network_reconnect_fails(self):
        control = Mock()
        instance = {
            "product": "openclaw", "legacy_user_id": "alice",
            "runtime_identifier": "openclaw_alice", "status": "provisioning",
            "basic_auth_enabled": True,
        }
        adapter = Mock()
        adapter.manager_dir = Path("/manager")
        adapter.nginx_container_name = "nginx"
        adapter.supports.return_value = True
        adapter.create.return_value = (0, "created")
        adapter.run_command.side_effect = [
            (0, "nginx recreated"),
            (1, "network reconnect failed"),
            (0, "deleted"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            secret_dir = Path(directory)
            secret_path = secret_dir / "secret"
            secret_path.write_text("password", encoding="utf-8")
            control.claim.return_value = {
                "job": {"request_id": "create-1", "action": "instance.create",
                        "params": {"secret_path": str(secret_path)}},
                "instance": instance,
            }
            with patch.object(self.executor, "PROVISIONING_SECRET_DIR", secret_dir):
                self.executor.run_once(control, lambda product: adapter)

        adapter.reload_nginx.assert_not_called()
        self.assertEqual(control.update.call_args.args, ("create-1", "failed"))
        self.assertIn("network reconnect failed", control.update.call_args.kwargs["output"])

    def test_run_once_redacts_secrets_from_creation_failure(self):
        control = Mock()
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.create.return_value = (
            1,
            "allocator failed\npassword: secret-password\n"
            '"token": "quoted-token-value"\n'
            "Login Token:\n👉 runtime-token-value",
        )
        with tempfile.TemporaryDirectory() as directory:
            secret_dir = Path(directory)
            secret_path = secret_dir / "secret"
            secret_path.write_text("secret-password", encoding="utf-8")
            control.claim.return_value = {
                "job": {"request_id": "create-1", "action": "instance.create",
                        "params": {"secret_path": str(secret_path)}},
                "instance": {
                    "product": "openclaw", "legacy_user_id": "alice",
                    "runtime_identifier": "openclaw_alice", "status": "provisioning",
                    "basic_auth_enabled": True,
                },
            }
            with patch.object(self.executor, "PROVISIONING_SECRET_DIR", secret_dir):
                self.executor.run_once(control, lambda product: adapter)

        output = control.update.call_args.kwargs["output"]
        self.assertIn("allocator failed", output)
        self.assertIn("[REDACTED]", output)
        self.assertNotIn("secret-password", output)
        self.assertNotIn("quoted-token-value", output)
        self.assertNotIn("runtime-token-value", output)
        self.assertLessEqual(len(self.executor.sanitize_creation_error("x" * 5000)), 4096)

    def test_run_once_skips_start_when_instance_is_already_up(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.start"},
            "instance": {"product": "evoscientist", "runtime_identifier": "evosci"},
        }
        adapter = Mock()
        adapter.status.return_value = "Up (evosci=Up)"

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.start.assert_not_called()
        self.assertEqual(control.update.call_args.args, ("request-1", "succeeded"))
        self.assertIn("already running", control.update.call_args.kwargs["output"])

    def test_run_once_records_failure_after_limited_attempts(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.stop"},
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.stop.return_value = (1, "docker failed")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        self.assertEqual(adapter.stop.call_count, 2)
        self.assertEqual(control.update.call_args.args, ("request-1", "failed"))
        self.assertEqual(control.update.call_args.kwargs["output"], "docker failed")

    def test_run_once_retries_adapter_exceptions(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.start"},
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "STOPPED"
        adapter.start.side_effect = [TimeoutError("timed out"), (0, "started")]

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        self.assertEqual(adapter.start.call_count, 2)
        self.assertEqual(control.update.call_args.args, ("request-1", "succeeded"))

    def test_run_once_does_not_blindly_retry_restart(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.restart"},
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.restart.return_value = (1, "ambiguous failure")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.restart.assert_called_once()
        self.assertEqual(control.update.call_args.args, ("request-1", "failed"))

    def test_run_once_rejects_action_not_supported_by_adapter(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "request-1", "action": "instance.restart"},
            "instance": {
                "product": "evoscientist",
                "runtime_identifier": "evosci",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = False

        self.executor.run_once(control, lambda product: adapter)

        adapter.status.assert_not_called()
        adapter.restart.assert_not_called()
        self.assertEqual(control.update.call_args.args, ("request-1", "failed"))
        self.assertEqual(
            control.update.call_args.kwargs["error_summary"],
            "instance product does not support restart",
        )

    def test_run_once_sets_basic_auth_without_retry(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "basic-auth-1",
                "action": "instance.set_basic_auth",
                "params": {"enabled": False},
            },
            "instance": {
                "product": "openclaw",
                "legacy_user_id": "alice",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.set_basic_auth.return_value = (0, "disabled")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.status.assert_not_called()
        adapter.set_basic_auth.assert_called_once_with(
            control.claim.return_value["instance"], False
        )
        self.assertEqual(control.update.call_args.args, ("basic-auth-1", "succeeded"))
        self.assertEqual(control.update.call_args.kwargs["output"], "disabled")

    def test_run_once_updates_version_without_retry(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "version-1",
                "action": "instance.update_version",
                "params": {
                    "version": "2026.7.28",
                    "restore_model_provider": True,
                },
            },
            "instance": {
                "product": "openclaw",
                "legacy_user_id": "alice",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.update_version.return_value = (0, "updated")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.status.assert_not_called()
        adapter.update_version.assert_called_once_with(
            control.claim.return_value["instance"],
            "2026.7.28",
            restore_model_provider=True,
        )
        self.assertEqual(control.update.call_args.args, ("version-1", "succeeded"))

    def test_run_once_installs_skill_without_retry(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "skill-1",
                "action": "instance.install_skill",
                "params": {"skill_id": "weather@1.0"},
            },
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.status.return_value = "Up"
        adapter.install_skill.return_value = (0, "installed")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.install_skill.assert_called_once_with(
            control.claim.return_value["instance"], "weather@1.0", request_id="skill-1"
        )
        self.assertEqual(control.update.call_args.args, ("skill-1", "succeeded"))

    def test_run_once_sets_model_provider_without_sensitive_params(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "model-provider-1",
                "action": "instance.set_model_provider",
                "params": {
                    "model_provider_id": "openai",
                    "model_id": "openai/gpt-5",
                    "model_base_url": "https://models.example/v1",
                    "model_alias": "GPT-5",
                },
            },
            "instance": {
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.status.return_value = "Up"
        adapter.set_model_provider.return_value = (0, "updated")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.set_model_provider.assert_called_once_with(
            control.claim.return_value["instance"], "openai", "openai/gpt-5",
            "https://models.example/v1", "GPT-5",
        )
        self.assertEqual(control.update.call_args.args, ("model-provider-1", "succeeded"))

    def test_run_once_refreshes_devices_without_retry(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "devices-1",
                "action": "instance.refresh_devices",
                "params": {},
            },
            "instance": {
                "product": "openclaw",
                "legacy_user_id": "alice",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.status.return_value = "Up"
        adapter.refresh_devices.return_value = (0, "refreshed")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.refresh_devices.assert_called_once_with(
            control.claim.return_value["instance"]
        )
        self.assertEqual(control.update.call_args.args, ("devices-1", "succeeded"))

    def test_run_once_approves_latest_device_with_stable_request_id(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "device-approval-1",
                "action": "instance.approve_latest_device",
                "params": {},
            },
            "instance": {
                "product": "openclaw",
                "legacy_user_id": "alice",
                "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.status.return_value = "Up"
        adapter.approve_latest_device.return_value = (0, "approved")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.approve_latest_device.assert_called_once_with(
            control.claim.return_value["instance"], request_id="device-approval-1"
        )

    def test_run_once_deletes_instance_without_retry(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "delete-1", "action": "instance.delete", "params": {}},
            "instance": {
                "product": "openclaw", "status": "active",
                "legacy_user_id": "alice", "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.delete.return_value = (1, "ambiguous failure")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.delete.assert_called_once_with(control.claim.return_value["instance"])
        self.assertEqual(control.update.call_args.args, ("delete-1", "failed"))

    def test_run_once_restores_only_restorable_deleted_instance(self):
        control = Mock()
        control.claim.return_value = {
            "job": {"request_id": "restore-1", "action": "instance.restore", "params": {}},
            "instance": {
                "product": "openclaw", "status": "deleted", "restore_state": "restorable",
                "legacy_user_id": "alice", "runtime_identifier": "openclaw_alice",
            },
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.restore.return_value = (0, "restored")

        self.executor.run_once(control, lambda product: adapter, max_attempts=2)

        adapter.restore.assert_called_once_with(control.claim.return_value["instance"])

    def test_run_once_purges_only_restorable_deleted_instance(self):
        control = Mock()
        instance = {
            "product": "openclaw", "status": "deleted", "restore_state": "restorable",
            "legacy_user_id": "alice", "runtime_identifier": "openclaw_alice",
        }
        control.claim.return_value = {
            "job": {"request_id": "purge-1", "action": "instance.purge_deleted", "params": {}},
            "instance": instance,
        }
        adapter = Mock()
        adapter.supports.return_value = True
        adapter.purge_deleted.return_value = (0, "purged")

        self.executor.run_once(control, lambda product: adapter)

        adapter.purge_deleted.assert_called_once_with(instance)
        self.assertEqual(control.update.call_args.args, ("purge-1", "succeeded"))

    def test_run_once_executes_wechat_bind_with_resolved_runtime_target(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw", "runtime_identifier": "openclaw_alice", "access_role": "owner"},
        }
        process = Mock()
        process.stdout.readline.return_value = "https://liteapp.weixin.qq.com/q/example\n"
        process.stdout.read.return_value = ""
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        control.get_job.return_value = {"status": "running"}
        control.get_runtime_instance.return_value = control.claim.return_value["instance"]
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        selector = Mock()
        selector.select.return_value = [object()]
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ) as popen, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=selector
        ):
            self.executor.run_once(control, lambda product: adapter)

        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "exec", "openclaw_alice"])
        self.assertIn("@tencent-weixin/openclaw-weixin-cli", command)
        self.assertEqual(control.update.call_args_list[-1].args, ("request-wechat", "succeeded"))

    def test_run_once_stops_wechat_bind_after_cancellation(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw", "runtime_identifier": "openclaw_alice", "access_role": "owner"},
        }
        control.get_job.return_value = {"status": "cancelled"}
        control.get_runtime_instance.return_value = control.claim.return_value["instance"]
        process = Mock()
        process.poll.return_value = None
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ), patch.object(
            self.executor.subprocess, "run"
        ) as run, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=Mock()
        ):
            self.executor.run_once(control, lambda product: adapter)

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "openclaw_alice"])

    def test_run_once_does_not_start_wechat_bind_for_stopped_instance(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw"},
        }
        control.get_runtime_instance.return_value = {
            "product": "openclaw",
            "runtime_identifier": "openclaw_alice",
            "access_role": "owner",
        }
        adapter = Mock()
        adapter.status.return_value = "STOPPED"
        with patch.object(self.executor.subprocess, "Popen") as popen:
            self.executor.run_once(control, lambda product: adapter)

        popen.assert_not_called()
        self.assertEqual(control.update.call_args.args, ("request-wechat", "failed"))
        self.assertEqual(control.update.call_args.kwargs["error_summary"], "instance is not running")

    def test_run_once_cleans_up_when_cancel_races_with_output_update(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw"},
        }
        control.get_runtime_instance.return_value = {
            "product": "openclaw",
            "runtime_identifier": "openclaw_alice",
            "access_role": "owner",
        }
        control.get_job.side_effect = [
            {"status": "running"},
            {"status": "cancelled"},
            {"status": "cancelled"},
        ]
        control.update.side_effect = [None, RuntimeError("invalid transition")]
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        process = Mock()
        process.poll.return_value = None
        process.stdout.readline.return_value = "working\n"
        selector = Mock()
        selector.select.return_value = [object()]
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ), patch.object(
            self.executor.subprocess, "run"
        ) as run, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=selector
        ):
            self.executor.run_once(control, lambda product: adapter)

        selector.close.assert_called_once()
        process.terminate.assert_called_once()
        process.stdout.close.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "openclaw_alice"])

    def test_run_once_cleans_up_container_command_after_update_error(self):
        control = Mock()
        control.claim.return_value = {
            "job": {
                "request_id": "request-wechat",
                "action": "instance.wechat_bind",
                "actor_user_public_id": "user-1",
                "instance_public_id": "instance-1",
            },
            "instance": {"product": "openclaw"},
        }
        control.get_runtime_instance.return_value = {
            "product": "openclaw",
            "runtime_identifier": "openclaw_alice",
            "access_role": "owner",
        }
        control.get_job.return_value = {"status": "running"}
        control.update.side_effect = [None, RuntimeError("control unavailable")]
        adapter = Mock()
        adapter.status.return_value = "Up"
        adapter.get_runtime_target.return_value = "openclaw_alice"
        process = Mock()
        process.poll.return_value = None
        process.stdout.readline.return_value = "working\n"
        selector = Mock()
        selector.select.return_value = [object()]
        with patch.object(
            self.executor.subprocess, "Popen", return_value=process
        ), patch.object(
            self.executor.subprocess, "run"
        ) as run, patch.object(
            self.executor.selectors, "DefaultSelector", return_value=selector
        ):
            self.executor.run_once(control, lambda product: adapter)

        process.terminate.assert_called_once()
        process.stdout.close.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "openclaw_alice"])

    def test_runtime_file_path_cannot_escape_instance_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            instance = {"data_path": temp_dir}
            inside = self.executor.resolve_instance_file(
                instance, "workspace", "notes.txt"
            )
            escaped = self.executor.resolve_instance_file(
                instance, "workspace", "../../etc/passwd"
            )

        self.assertEqual(inside, Path(temp_dir) / "workspace" / "notes.txt")
        self.assertIsNone(escaped)

    def test_runtime_file_root_symlink_cannot_escape_instance_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "instance"
            data_path.mkdir()
            (data_path / "workspace").symlink_to(Path(temp_dir))

            target = self.executor.resolve_instance_file(
                {"data_path": str(data_path)}, "workspace", "secret.txt"
            )

        self.assertIsNone(target)


if __name__ == "__main__":
    unittest.main()
