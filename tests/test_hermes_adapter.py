#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))

from instance_adapters import HermesDockerAdapter


class HermesAdapterTests(unittest.TestCase):
    INSTANCE = {
        "public_id": "11111111-1111-1111-1111-111111111111",
        "legacy_user_id": "alice",
        "runtime_identifier": "hermes-alice",
    }

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
            adapter = self.make_adapter(Path(temp_dir))
            with patch.object(
                adapter, "run_command", return_value=(0, "ok")
            ) as run_command, patch.object(
                adapter, "enable_nginx_user_conf"
            ) as enable_nginx, patch.object(
                adapter, "disable_nginx_user_conf"
            ) as disable_nginx:
                self.assertEqual(adapter.start(self.INSTANCE), (0, "ok"))
                self.assertEqual(adapter.stop(self.INSTANCE), (0, "ok"))

            self.assertEqual(
                [call.args[0] for call in run_command.call_args_list],
                [
                    ["docker", "start", "hermes-alice"],
                    ["docker", "stop", "hermes-alice"],
                ],
            )
            enable_nginx.assert_not_called()
            disable_nginx.assert_not_called()

    def test_openclaw_only_actions_are_unsupported(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))

            code, output = adapter.update_version(self.INSTANCE, "v0.20.0")

            self.assertNotEqual(code, 0)
            self.assertIn("not supported", output)

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

            self.assertEqual((code, output), (0, "applied\nreloaded"))
            nginx = adapter.ingress_conf(self.INSTANCE).read_text(encoding="utf-8")
            self.assertIn("server hermes-alice:9119 resolve;", nginx)
            self.assertIn("listen 39119 ssl;", nginx)
            compose_text = compose.read_text(encoding="utf-8")
            self.assertIn('      - "39119:39119"', compose_text)
            self.assertIn("      - hermes-net", compose_text)
            self.assertEqual(run_command.call_count, 1)

    def test_stop_disables_ingress_and_start_restores_it(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            active = adapter.ingress_conf(self.INSTANCE)
            active.parent.mkdir(parents=True)
            active.write_text("server {}\n", encoding="utf-8")
            with patch.object(adapter, "run_command", return_value=(0, "ok")), patch.object(
                adapter, "reload_nginx", return_value=(0, "reloaded")
            ):
                self.assertEqual(adapter.stop(self.INSTANCE), (0, "ok"))
                self.assertFalse(active.exists())
                self.assertTrue(adapter.ingress_conf(self.INSTANCE, disabled=True).exists())
                code, output = adapter.start(self.INSTANCE)

            self.assertEqual(code, 0)
            self.assertIn("reloaded", output)
            self.assertTrue(active.exists())
            self.assertFalse(adapter.ingress_conf(self.INSTANCE, disabled=True).exists())

    def test_create_uses_pinned_single_container_and_hashes_password(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            instance = {
                **self.INSTANCE,
                "data_path": str(root / "public" / "hermes" / "alice"),
            }
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "network", "inspect"]:
                    return 1, "missing"
                if any("allocate_port" in part for part in command):
                    return 0, "39119\n[INFO] Port 39118 is already in use, skip"
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run):
                code, _ = adapter.create(instance, "true", "line1\nINJECTED=value")

            self.assertEqual(code, 0)
            self.assertEqual(instance["_created_port"], 39119)
            docker_run = next(command for command in calls if command[:2] == ["docker", "run"])
            self.assertIn("nousresearch/hermes-agent:v2026.7.20", docker_run)
            self.assertIn("HERMES_DASHBOARD=1", docker_run)
            self.assertNotIn("-p", docker_run)
            self.assertTrue(any(command[:3] == ["docker", "exec", "hermes-alice"] for command in calls))
            env_text = (Path(instance["data_path"]) / ".env").read_text(encoding="utf-8")
            self.assertIn("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$", env_text)
            self.assertNotIn("line1", env_text)
            self.assertNotIn("INJECTED", env_text)
            self.assertEqual(
                (Path(instance["data_path"]) / "config.yaml").read_text(encoding="utf-8"),
                "security:\n  allow_lazy_installs: false\n",
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

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ["docker", "network", "inspect"]:
                    return 1, "missing"
                if any("allocate_port" in part for part in command):
                    return 0, "39119"
                if command[:2] == ["docker", "run"]:
                    return 1, "start failed"
                return 0, "ok"

            with patch.object(adapter, "run_command", side_effect=run):
                code, output = adapter.create(instance, "true", "password")

            self.assertEqual(code, 1)
            self.assertIn("rolled back", output)
            self.assertFalse(Path(instance["data_path"]).exists())
            self.assertNotIn("_created_port", instance)
            self.assertTrue(any(command[:3] == ["docker", "rm", "-f"] for command in calls))
            self.assertTrue(any(command[:3] == ["docker", "network", "rm"] for command in calls))

if __name__ == "__main__":
    unittest.main()
