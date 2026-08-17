#!/usr/bin/env python3

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))

from instance_adapters import (
    HermesDockerAdapter,
    stage_hermes_bridge_ca,
    stage_hermes_plugin,
)
from tests.tls_fixtures import write_test_ca


class HermesAdapterTests(unittest.TestCase):
    INSTANCE = {
        "public_id": "11111111-1111-1111-1111-111111111111",
        "legacy_user_id": "alice",
        "runtime_identifier": "hermes-alice",
    }

    def setUp(self):
        self.public_host = patch.dict(os.environ, {"PUBLIC_HOST": "manager.example.test"})
        self.public_host.start()

    def tearDown(self):
        self.public_host.stop()

    def make_adapter(self, root):
        return HermesDockerAdapter(
            manager_dir=root,
            public_dir=root / "public",
            nginx_users_conf_dir=root / "nginx" / "conf",
            nginx_compose_dir=root / "nginx" / "compose",
            nginx_container_name="openclaw-nginx",
        )

    def test_start_and_stop_manage_registered_container_without_ingress(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            with patch.object(
                adapter, "run_command", return_value=(0, "ok")
            ) as run_command, patch.object(
                adapter, "enable_nginx_conf"
            ) as enable_nginx, patch.object(
                adapter, "disable_nginx_conf"
            ) as disable_nginx:
                self.assertEqual(adapter.start(instance)[0], 0)
                self.assertEqual(adapter.stop(instance), (0, "ok"))

            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertEqual(commands[0], ["docker", "start", "hermes-alice"])
            self.assertEqual(commands[-1], ["docker", "stop", "hermes-alice"])
            acl_commands = [command for command in commands if command[0] == "find"]
            self.assertEqual(len(acl_commands), 2)
            self.assertEqual([command[4] for command in acl_commands[:2]], ["d", "f"])
            self.assertIn("d:u:", acl_commands[0][8])
            self.assertNotIn("d:u:", acl_commands[1][8])
            enable_nginx.assert_not_called()
            disable_nginx.assert_not_called()

    def test_host_access_retries_when_acl_changes_after_initial_stability(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            checks = 0

            def run(command, **kwargs):
                nonlocal checks
                if command[:2] == ["bash", "-lc"]:
                    checks += 1
                    return (1, "ACL changed") if checks == 6 else (0, "stable")
                return 0, "applied"

            with patch.object(adapter, "run_command", side_effect=run) as run_command:
                code, output = adapter._grant_host_manager_access(instance)

            self.assertEqual(code, 0)
            self.assertIn("stable", output)
            self.assertEqual(checks, 21)
            self.assertEqual(
                len([call for call in run_command.call_args_list if call.args[0][0] == "find"]),
                4,
            )

    def test_host_access_retries_when_a_dynamic_file_disappears(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            calls = 0

            def run(command, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return 1, "file disappeared"
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run):
                code, output = adapter._grant_host_manager_access(instance)

            self.assertEqual(code, 0)
            self.assertIn("ok", output)

    def test_host_access_fails_when_acl_never_stabilizes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            instance = {**self.INSTANCE, "data_path": str(data_path)}

            def run(command, **kwargs):
                return (1, "ACL changed") if command[:2] == ["bash", "-lc"] else (0, "applied")

            with patch.object(adapter, "run_command", side_effect=run):
                code, output = adapter._grant_host_manager_access(instance)

            self.assertEqual(code, 1)
            self.assertIn("did not become stable", output)

    def test_nginx_candidates_do_not_include_openclaw_legacy_config(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))

            self.assertEqual(
                adapter.nginx_conf_candidates(self.INSTANCE),
                [
                    adapter.ingress_conf(self.INSTANCE),
                    adapter.ingress_conf(self.INSTANCE, disabled=True),
                ],
            )

    def test_update_version_requires_target_image_to_be_pulled(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            instance = {**self.INSTANCE, "data_path": str(Path(temp_dir) / "public" / "hermes" / "alice")}
            with patch.object(
                adapter, "run_command",
                side_effect=[
                    (0, f"nousresearch/hermes-agent:v2026.7.20|true|{adapter.tenant_network(instance)}"),
                    (1, "missing"),
                ],
            ) as run_command:
                code, output = adapter.update_version(instance, "v2026.8.1")

            self.assertEqual(code, 1)
            self.assertIn("docker pull", output)
            self.assertEqual(run_command.call_count, 2)

    def test_delete_moves_data_to_recycle_and_restore_recreates_container(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            (data_path / "config.yaml").write_text("security: {}\n", encoding="utf-8")
            (data_path / ".env").write_text(
                "HERMES_UIS_BRIDGE_CLIENT_ID=old-client\n"
                "HERMES_UIS_BRIDGE_CLIENT_SECRET=old-secret\n",
                encoding="utf-8",
            )
            instance = {
                **self.INSTANCE, "data_path": str(data_path), "port": 39119,
                "access_url": "https://manager.example.test:39119",
            }
            compose = root / "nginx" / "compose" / "docker-compose.yml"
            compose.parent.mkdir(parents=True)
            compose.write_text(
                "services:\n  nginx:\n    ports:\n      - \"443:443\"\n      - \"39119:39119\"\n"
                "    networks:\n      - manager-net\n      - hermes-net\n"
                "networks:\n  manager-net:\n    external: true\n  hermes-net:\n    external: true\n",
                encoding="utf-8",
            )
            conf = adapter.ingress_conf(instance)
            conf.parent.mkdir(parents=True)
            conf.write_text("server {}\n", encoding="utf-8")
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "inspect", "--format"]:
                    return 0, f"nousresearch/hermes-agent:v2026.7.20|true|{adapter.tenant_network(instance)}"
                return 0, "ok"

            clients = []
            ca_source = write_test_ca(root / "manager-ca.crt")
            with patch.dict(os.environ, {
                "HERMES_AUTH_BRIDGE_ISSUER": "https://manager.example.test:30015/auth/hermes",
                "HERMES_AUTH_BRIDGE_CA_FILE": str(ca_source),
            }), patch.object(adapter, "run_command", side_effect=run), patch.object(
                adapter, "apply_nginx_compose", return_value=(0, "applied")
            ), patch.object(adapter, "reload_nginx", return_value=(0, "reloaded")), patch.object(
                adapter, "configure_ingress", return_value=(0, "published")
            ):
                self.assertEqual(adapter.delete(instance)[0], 0)
                self.assertFalse(data_path.exists())
                self.assertTrue((adapter.hermes_recycle_dir(instance) / "data" / "config.yaml").is_file())
                self.assertEqual(adapter.restore(
                    instance, hermes_auth_client_callback=clients.append
                )[0], 0)

            self.assertTrue((data_path / "config.yaml").is_file())
            self.assertFalse(adapter.hermes_recycle_dir(instance).exists())
            self.assertEqual(clients[0]["redirect_uri"], "https://manager.example.test:39119/auth/callback")
            self.assertNotEqual(clients[0]["client_id"], "old-client")
            env_text = (data_path / ".env").read_text(encoding="utf-8")
            self.assertIn(
                "HERMES_UIS_BRIDGE_CA_FILE=/opt/data/manager-auth/bridge-ca.crt",
                env_text,
            )
            self.assertEqual(
                (data_path / "manager-auth" / "bridge-ca.crt").read_bytes(),
                ca_source.read_bytes(),
            )
            self.assertIn(
                ["docker", "network", "connect", adapter.tenant_network(instance),
                 "openclaw-model-proxy"],
                calls,
            )

    def test_update_version_recreates_with_local_target_and_rolls_back_on_failure(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "inspect", "--format"]:
                    return 0, "nousresearch/hermes-agent:v2026.7.20|true|hermes-net"
                if command[:3] == ["docker", "image", "inspect"]:
                    return 0, "present"
                if command[:2] == ["docker", "run"] and any(
                    value.endswith(":v2026.8.1") for value in command
                ):
                    return 1, "new image failed"
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run):
                code, output = adapter.update_version(instance, "v2026.8.1")

            self.assertEqual(code, 1)
            self.assertIn("rolled back", output)
            runs = [command for command in calls if command[:2] == ["docker", "run"]]
            self.assertTrue(any(any(value.endswith(":v2026.8.1") for value in command) for command in runs))
            self.assertTrue(any(any(value.endswith(":v2026.7.20") for value in command) for command in runs))

    def test_compose_ingress_is_persistent_and_idempotent(self):
        text = (
            "services:\n  nginx:\n    ports:\n      - \"443:443\"\n"
            "    networks:\n      - manager-net\n"
            "networks:\n  manager-net:\n    external: true\n"
        )

        updated = HermesDockerAdapter._add_ingress_to_nginx_compose(
            text, 39119, "hermes-net"
        )
        repeated = HermesDockerAdapter._add_ingress_to_nginx_compose(
            updated, 39119, "hermes-net"
        )

        self.assertEqual(updated, repeated)
        self.assertEqual(updated.count('"39119:39119"'), 1)
        self.assertEqual(updated.count("      - hermes-net"), 1)
        self.assertEqual(updated.count("  hermes-net:\n"), 1)

    def test_configure_ingress_targets_dashboard_and_rolls_back_on_reload_failure(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            compose = root / "nginx" / "compose" / "docker-compose.yml"
            compose.parent.mkdir(parents=True)
            original = (
                "services:\n  nginx:\n    ports:\n      - \"443:443\"\n"
                "    networks:\n      - manager-net\n"
                "networks:\n  manager-net:\n    external: true\n"
            )
            compose.write_text(original, encoding="utf-8")
            inspected = type(
                "Result", (), {"returncode": 0, "stdout": "hermes-net\n", "stderr": ""}
            )()
            with patch("instance_adapters.subprocess.run", return_value=inspected), patch.object(
                adapter, "run_command", return_value=(0, "applied")
            ), patch.object(
                adapter,
                "reload_nginx",
                side_effect=[(1, "invalid"), (0, "restored")],
            ):
                code, output = adapter.configure_ingress(
                    {**self.INSTANCE, "port": 39119}
                )

            self.assertEqual(code, 1)
            self.assertIn("rolled back", output)
            self.assertEqual(compose.read_text(encoding="utf-8"), original)
            self.assertFalse(adapter.ingress_conf(self.INSTANCE).exists())

    def test_configure_ingress_reports_failed_nginx_recovery(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            compose = root / "nginx" / "compose" / "docker-compose.yml"
            compose.parent.mkdir(parents=True)
            compose.write_text(
                "services:\n  nginx:\n    ports:\n      - \"443:443\"\n"
                "    networks:\n      - manager-net\n"
                "networks:\n  manager-net:\n    external: true\n",
                encoding="utf-8",
            )
            inspected = type(
                "Result", (), {"returncode": 0, "stdout": "hermes-net\n", "stderr": ""}
            )()
            with patch("instance_adapters.subprocess.run", return_value=inspected), patch.object(
                adapter, "run_command", return_value=(0, "applied")
            ), patch.object(
                adapter,
                "reload_nginx",
                side_effect=[(1, "invalid"), (1, "still invalid")],
            ):
                code, output = adapter.configure_ingress(
                    {**self.INSTANCE, "port": 39119}
                )

            self.assertEqual(code, 1)
            self.assertIn("Nginx recovery failed: still invalid", output)

    def test_configure_ingress_publishes_dashboard_and_persists_network(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            compose = root / "nginx" / "compose" / "docker-compose.yml"
            compose.parent.mkdir(parents=True)
            compose.write_text(
                "services:\n  nginx:\n    ports:\n      - \"443:443\"\n"
                "    networks:\n      - manager-net\n"
                "networks:\n  manager-net:\n    external: true\n",
                encoding="utf-8",
            )
            inspected = type(
                "Result", (), {"returncode": 0, "stdout": "hermes-net\n\n", "stderr": ""}
            )()
            with patch("instance_adapters.subprocess.run", return_value=inspected), patch.object(
                adapter, "run_command", return_value=(0, "applied")
            ) as run_command, patch.object(
                adapter, "reload_nginx", return_value=(0, "reloaded")
            ):
                code, output = adapter.configure_ingress(
                    {**self.INSTANCE, "port": 39119}
                )

            self.assertEqual((code, output), (0, "applied\napplied\nreloaded"))
            nginx = adapter.ingress_conf(self.INSTANCE).read_text(encoding="utf-8")
            self.assertIn("server hermes-alice:9119 resolve;", nginx)
            self.assertIn("listen 39119 ssl;", nginx)
            compose_text = compose.read_text(encoding="utf-8")
            self.assertIn('      - "39119:39119"', compose_text)
            self.assertIn("      - hermes-net", compose_text)
            self.assertIn("      - instance-auth-net", compose_text)
            self.assertIn("auth_request /_instance_auth;", nginx)
            callback = nginx.split("location = /auth/callback {", 1)[1].split("}", 1)[0]
            self.assertNotIn("auth_request", callback)
            self.assertIn("access_log off", callback)
            self.assertIn("openclaw-instance-auth-proxy:8084 resolve;", nginx)
            self.assertEqual(run_command.call_count, 2)
            reconnect = run_command.call_args_list[1].args[0]
            self.assertIn("connect_shared_services_to_tenant_networks", reconnect[2])
            self.assertEqual(
                reconnect[-2:], ["openclaw-nginx", "openclaw-model-proxy"]
            )

    def test_stop_disables_ingress_and_start_restores_it(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            active = adapter.ingress_conf(instance)
            active.parent.mkdir(parents=True)
            active.write_text("server {}\n", encoding="utf-8")
            with patch.object(adapter, "run_command", return_value=(0, "ok")), patch.object(
                adapter, "reload_nginx", return_value=(0, "reloaded")
            ):
                self.assertEqual(adapter.stop(instance), (0, "ok"))
                self.assertFalse(active.exists())
                self.assertTrue(adapter.ingress_conf(instance, disabled=True).exists())
                code, output = adapter.start(instance)

            self.assertEqual(code, 0)
            self.assertIn("reloaded", output)
            self.assertTrue(active.exists())
            self.assertFalse(adapter.ingress_conf(instance, disabled=True).exists())

    def test_create_uses_pinned_container_and_stages_uis_provider_before_start(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            instance = {
                **self.INSTANCE,
                "data_path": str(root / "public" / "hermes" / "alice"),
            }
            calls = []
            template = root / "templates" / "hermes" / "plugins" / "campus-uis-bridge"
            template.mkdir(parents=True)
            (template / "plugin.yaml").write_text("name: campus-uis-bridge\n", encoding="utf-8")
            (template / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
            template.chmod(0o770)
            (template / "plugin.yaml").chmod(0o660)
            (template / "__init__.py").chmod(0o660)
            ca_source = root / "manager-ca.crt"
            ca_source.write_text("test CA certificate\n", encoding="utf-8")

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "network", "inspect"]:
                    return 1, "missing"
                if any("allocate_port" in part for part in command):
                    return 0, "39119\n[INFO] Port 39118 is already in use, skip"
                return 0, "ok"

            created_clients = []
            with patch.object(adapter, "run_command", side_effect=run), patch.dict(
                os.environ,
                {
                    "HERMES_AUTH_BRIDGE_ISSUER": "https://manager.example.test:30015/auth/hermes",
                    "HERMES_AUTH_BRIDGE_CA_FILE": str(ca_source),
                    "PUBLIC_HOST": "manager.example.test",
                },
            ), patch("instance_adapters.ssl.create_default_context"), patch(
                "instance_adapters.os.chown"
            ) as chown:
                code, _ = adapter.create(
                    instance, "false", "",
                    hermes_auth_client_callback=created_clients.append,
                )

            self.assertEqual(code, 0)
            self.assertEqual(instance["_created_port"], 39119)
            docker_run = next(command for command in calls if command[:2] == ["docker", "run"])
            self.assertIn("nousresearch/hermes-agent:v2026.7.20", docker_run)
            self.assertIn("HERMES_DASHBOARD=1", docker_run)
            self.assertNotIn("-p", docker_run)
            self.assertTrue(any(command[:3] == ["docker", "exec", "hermes-alice"] for command in calls))
            env_text = (Path(instance["data_path"]) / ".env").read_text(encoding="utf-8")
            self.assertNotIn("HERMES_DASHBOARD_BASIC_AUTH", env_text)
            self.assertIn("HERMES_UIS_BRIDGE_CLIENT_ID=", env_text)
            self.assertIn("HERMES_UIS_BRIDGE_CLIENT_SECRET=", env_text)
            self.assertIn(
                "HERMES_UIS_BRIDGE_REDIRECT_URI=https://manager.example.test:39119/auth/callback",
                env_text,
            )
            self.assertIn(
                "HERMES_UIS_BRIDGE_CA_FILE=/opt/data/manager-auth/bridge-ca.crt",
                env_text,
            )
            ca_target = Path(instance["data_path"]) / "manager-auth" / "bridge-ca.crt"
            self.assertEqual(ca_target.read_text(encoding="utf-8"), "test CA certificate\n")
            self.assertEqual(ca_target.parent.stat().st_mode & 0o777, 0o750)
            self.assertEqual(ca_target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(created_clients[0]["redirect_uri"], "https://manager.example.test:39119/auth/callback")
            self.assertTrue(
                (Path(instance["data_path"]) / "plugins" / "campus-uis-bridge" / "plugin.yaml").is_file()
            )
            plugin = Path(instance["data_path"]) / "plugins" / "campus-uis-bridge"
            self.assertEqual(plugin.parent.stat().st_mode & 0o777, 0o750)
            self.assertEqual(plugin.stat().st_mode & 0o777, 0o750)
            self.assertEqual((plugin / "plugin.yaml").stat().st_mode & 0o777, 0o640)
            self.assertEqual((plugin / "__init__.py").stat().st_mode & 0o777, 0o640)
            self.assertTrue(any(call.args[1:] == (10000, 10000) for call in chown.call_args_list))
            self.assertEqual(
                (Path(instance["data_path"]) / "config.yaml").read_text(encoding="utf-8"),
                "security:\n  allow_lazy_installs: false\nplugins:\n  enabled:\n    - campus-uis-bridge\n",
            )

    def test_stage_hermes_plugin_rejects_symlinks_before_copying(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            external = root / "external.py"
            external.write_text("secret\n", encoding="utf-8")
            (source / "__init__.py").symlink_to(external)

            with patch("instance_adapters.os.chown"):
                with self.assertRaisesRegex(RuntimeError, "cannot contain symlinks"):
                    stage_hermes_plugin(source, target, 10000, 10000)

            self.assertFalse(target.exists())

    def test_stage_hermes_bridge_ca_rejects_symlink_source(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "manager-ca.crt"
            source.symlink_to(root / "outside.crt")

            with self.assertRaisesRegex(RuntimeError, "cannot be a symlink"):
                stage_hermes_bridge_ca(
                    source, root / "data" / "manager-auth" / "bridge-ca.crt",
                    10000, 10000,
                )

    def test_stage_hermes_bridge_ca_rejects_private_key_material(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_test_ca(
                root / "manager-ca.crt", include_private_key=True
            )

            with patch("instance_adapters.os.chown"), self.assertRaisesRegex(
                RuntimeError, "must not contain a private key"):
                stage_hermes_bridge_ca(
                    source, root / "data" / "manager-auth" / "bridge-ca.crt",
                    10000, 10000,
                )

    def test_create_failure_removes_container_data_and_new_network(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            instance = {
                **self.INSTANCE,
                "data_path": str(root / "public" / "hermes" / "alice"),
            }
            calls = []
            ca_source = write_test_ca(root / "manager-ca.crt")

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "network", "inspect"]:
                    return 1, "missing"
                if any("allocate_port" in part for part in command):
                    return 0, "39119"
                if command[:2] == ["docker", "run"]:
                    return 1, "start failed"
                return 0, "ok"

            with patch.dict(os.environ, {
                "HERMES_AUTH_BRIDGE_ISSUER": "https://manager.example.test:30015/auth/hermes",
                "HERMES_AUTH_BRIDGE_CA_FILE": str(ca_source),
            }), patch.object(adapter, "run_command", side_effect=run), patch(
                "instance_adapters.os.chown"
            ):
                code, output = adapter.create(instance, "false", "")

            self.assertEqual(code, 1)
            self.assertIn("rolled back", output)
            self.assertFalse(Path(instance["data_path"]).exists())
            self.assertNotIn("_created_port", instance)
            self.assertTrue(any(command[:3] == ["docker", "rm", "-f"] for command in calls))
            self.assertTrue(any(command[:3] == ["docker", "network", "rm"] for command in calls))

    def test_set_model_provider_uses_instance_proxy_token(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            (data_path / "config.yaml").write_text("security: {}\n", encoding="utf-8")
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "inspect", "--format"]:
                    return 0, "manager-net"
                return 0, "ok"

            token_dir = root / "tokens"
            with patch.object(adapter, "run_command", side_effect=run), patch.dict(
                os.environ,
                {
                    "MODEL_PROXY_TOKEN_DIR": str(token_dir),
                    "MODEL_PROXY_PUBLIC_BASE_URL": "http://openclaw-model-proxy:8081/v1",
                },
            ):
                code, output = adapter.set_model_provider(
                    instance, "gpustack", "gpustack/qwen3.6-35b", "", "Qwen"
                )

            self.assertEqual(code, 0)
            self.assertEqual(output, "Hermes model provider updated.")
            token = (token_dir / "alice.token").read_text(encoding="utf-8").strip()
            self.assertTrue(token.startswith("ocm_"))
            self.assertNotIn(token, output)
            self.assertEqual((token_dir / "alice.token").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (token_dir / "alice.models").read_text(encoding="utf-8"),
                "qwen3.6-35b\n",
            )
            self.assertEqual((token_dir / "alice.models").stat().st_mode & 0o777, 0o600)
            self.assertIn(
                ["docker", "network", "connect", adapter.tenant_network(instance),
                 "openclaw-model-proxy"],
                calls,
            )
            config_commands = [
                command for command in calls
                if command[:4] == ["docker", "exec", "hermes-alice", "hermes"]
            ]
            self.assertEqual(
                [(command[6], command[7]) for command in config_commands],
                [
                    ("model.default", "qwen3.6-35b"),
                    ("model.provider", "custom"),
                    ("model.base_url", "http://openclaw-model-proxy:8081/v1"),
                    ("model.api_key", token),
                    ("model.api_mode", "chat_completions"),
                ],
            )

    def test_set_model_provider_rolls_back_files_and_new_network(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            config = data_path / "config.yaml"
            config.write_text("original: true\n", encoding="utf-8")
            token_dir = root / "tokens"
            token_dir.mkdir()
            (token_dir / "alice.token").write_text("old-token\n", encoding="utf-8")
            (token_dir / "alice.models").write_text("old-model\n", encoding="utf-8")
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "inspect", "--format"]:
                    return 0, "manager-net"
                if command[:6] == [
                    "docker", "exec", "hermes-alice", "hermes", "config", "set"
                ]:
                    config.write_text("partial: true\n", encoding="utf-8")
                    if command[6] == "model.provider":
                        return 1, "failed with old-token"
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run), patch.dict(
                os.environ,
                {
                    "MODEL_PROXY_TOKEN_DIR": str(token_dir),
                    "MODEL_PROXY_PUBLIC_BASE_URL": "http://openclaw-model-proxy:8081/v1",
                },
            ):
                code, output = adapter.set_model_provider(
                    instance, "gpustack", "qwen3.6-35b", "", "Qwen"
                )

            self.assertEqual(code, 1)
            self.assertIn("rolled back", output)
            self.assertNotIn("old-token", output)
            self.assertEqual(config.read_text(encoding="utf-8"), "original: true\n")
            self.assertEqual(
                (token_dir / "alice.token").read_text(encoding="utf-8"), "old-token\n"
            )
            self.assertEqual(
                (token_dir / "alice.models").read_text(encoding="utf-8"), "old-model\n"
            )
            self.assertIn(
                ["docker", "network", "disconnect", adapter.tenant_network(instance),
                 "openclaw-model-proxy"],
                calls,
            )

    def test_set_model_provider_replaces_empty_token_file(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            (data_path / "config.yaml").write_text("security: {}\n", encoding="utf-8")
            token_dir = root / "tokens"
            token_dir.mkdir()
            (token_dir / "alice.token").write_text("", encoding="utf-8")
            instance = {**self.INSTANCE, "data_path": str(data_path)}

            def run(command, **kwargs):
                if command[:3] == ["docker", "inspect", "--format"]:
                    return 0, adapter.tenant_network(instance)
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run), patch.dict(
                os.environ,
                {"MODEL_PROXY_TOKEN_DIR": str(token_dir)},
            ):
                code, _ = adapter.set_model_provider(
                    instance, "gpustack", "qwen3.6-35b", "", "Qwen"
                )

            self.assertEqual(code, 0)
            self.assertTrue(
                (token_dir / "alice.token").read_text(encoding="utf-8").startswith("ocm_")
            )

    def test_set_model_provider_stages_old_and_new_models_before_config(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            (data_path / "config.yaml").write_text("security: {}\n", encoding="utf-8")
            token_dir = root / "tokens"
            token_dir.mkdir()
            (token_dir / "alice.token").write_text("old-token\n", encoding="utf-8")
            models = token_dir / "alice.models"
            models.write_text("old-model\n", encoding="utf-8")
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            observed_allowlists = []

            def run(command, **kwargs):
                if command[:3] == ["docker", "inspect", "--format"]:
                    return 0, adapter.tenant_network(instance)
                if command[:4] == ["docker", "exec", "hermes-alice", "hermes"]:
                    observed_allowlists.append(models.read_text(encoding="utf-8"))
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run), patch.dict(
                os.environ,
                {"MODEL_PROXY_TOKEN_DIR": str(token_dir)},
            ):
                code, _ = adapter.set_model_provider(
                    instance, "gpustack", "new-model", "", "New"
                )

            self.assertEqual(code, 0)
            self.assertTrue(observed_allowlists)
            self.assertTrue(all(value == "new-model\nold-model\n" for value in observed_allowlists))
            self.assertEqual(models.read_text(encoding="utf-8"), "new-model\n")

    def test_set_model_provider_reports_incomplete_rollback(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            data_path = root / "public" / "hermes" / "alice"
            data_path.mkdir(parents=True)
            (data_path / "config.yaml").write_text("original: true\n", encoding="utf-8")
            instance = {**self.INSTANCE, "data_path": str(data_path)}
            token_dir = root / "tokens"

            def run(command, **kwargs):
                if command[:3] == ["docker", "inspect", "--format"]:
                    return 0, "manager-net"
                if command[:3] == ["docker", "network", "disconnect"]:
                    return 1, "disconnect failed"
                if command[:6] == [
                    "docker", "exec", "hermes-alice", "hermes", "config", "set"
                ]:
                    return 1, "config failed"
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run), patch.dict(
                os.environ,
                {"MODEL_PROXY_TOKEN_DIR": str(token_dir)},
            ):
                code, output = adapter.set_model_provider(
                    instance, "gpustack", "qwen3.6-35b", "", "Qwen"
                )

            self.assertEqual(code, 1)
            self.assertIn("manual recovery is required", output)
            self.assertIn("disconnect failed", output)

if __name__ == "__main__":
    unittest.main()
