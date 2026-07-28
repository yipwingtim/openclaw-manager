import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))

from instance_adapters import OpenClawDockerAdapter
from product_capabilities import (
    execution_action_capability,
    product_supports,
)


class AdapterInstanceModelTests(unittest.TestCase):
    def make_adapter(self, root):
        return OpenClawDockerAdapter(
            manager_dir=root,
            public_dir=root / "public",
            nginx_users_conf_dir=root / "nginx" / "conf",
            nginx_compose_dir=root / "nginx" / "compose",
            nginx_container_name="openclaw-nginx",
        )

    def test_runtime_target_comes_from_instance_record(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            instance = {
                "legacy_user_id": "alice",
                "runtime_identifier": "openclaw_alice_custom",
            }

            self.assertEqual(
                adapter.get_runtime_target(instance),
                "openclaw_alice_custom",
            )

    def test_status_uses_instance_runtime_identifier(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            instance = {"runtime_identifier": "openclaw_alice_custom"}
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="running\n", stderr=""
            )

            with patch.object(
                subprocess,
                "run",
                return_value=completed,
            ) as run:
                self.assertEqual(adapter.status(instance), "Up")

            self.assertEqual(
                run.call_args.args[0],
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}}",
                    "openclaw_alice_custom",
                ],
            )

    def test_start_without_legacy_user_id_skips_nginx(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            instance = {"runtime_identifier": "openclaw.project-1"}

            with patch.object(adapter, "run_command", return_value=(0, "started")) as run, patch.object(
                adapter, "enable_nginx_user_conf"
            ) as enable_nginx:
                result = adapter.start(instance)

            self.assertEqual(result, (0, "started"))
            run.assert_called_once_with(["docker", "start", "openclaw.project-1"], timeout=90)
            enable_nginx.assert_not_called()

    def test_set_basic_auth_restores_nginx_config_when_reload_fails(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            conf = root / "nginx" / "conf" / "alice.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text("original", encoding="utf-8")

            def update_config(*args, **kwargs):
                conf.write_text("changed", encoding="utf-8")
                return 0, "updated"

            with patch.object(
                adapter, "run_command", side_effect=update_config
            ) as run, patch.object(
                adapter,
                "reload_nginx",
                side_effect=[(1, "nginx test failed"), (0, "restored")],
            ) as reload_nginx:
                code, output = adapter.set_basic_auth(
                    {"legacy_user_id": "alice"}, False
                )

            self.assertEqual(code, 1)
            self.assertEqual(conf.read_text(encoding="utf-8"), "original")
            self.assertIn("Restored Nginx config", output)
            self.assertEqual(reload_nginx.call_count, 2)
            self.assertEqual(
                run.call_args.kwargs["env"]["OPENCLAW_SKIP_METADATA_WRITE"], "1"
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["OPENCLAW_SKIP_HTPASSWD_PERMISSIONS"],
                "1",
            )

    def test_set_basic_auth_restores_nginx_config_when_reload_raises(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            conf = root / "nginx" / "conf" / "alice.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text("original", encoding="utf-8")

            def update_config(*args, **kwargs):
                conf.write_text("changed", encoding="utf-8")
                return 0, "updated"

            with patch.object(
                adapter, "run_command", side_effect=update_config
            ), patch.object(
                adapter,
                "reload_nginx",
                side_effect=[subprocess.TimeoutExpired("nginx", 30), (0, "restored")],
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    adapter.set_basic_auth({"legacy_user_id": "alice"}, True)

            self.assertEqual(conf.read_text(encoding="utf-8"), "original")

    def test_update_version_restores_compose_after_timeout(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            user_dir = root / "public" / "users" / "alice"
            user_dir.mkdir(parents=True)
            compose = user_dir / "docker-compose.yml"
            compose.write_text("old", encoding="utf-8")
            script = root / "scripts" / "update_instance_version.sh"
            child_pid_file = root / "child.pid"
            script.parent.mkdir()
            script.write_text(
                "#!/usr/bin/env bash\n"
                f"trap 'printf old > {compose}' EXIT\n"
                f"printf new > {compose}\n"
                "sleep 60 &\n"
                f"echo $! > {child_pid_file}\n"
                "wait\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            with self.assertRaises(subprocess.TimeoutExpired):
                adapter.update_version(
                    {"legacy_user_id": "alice"}, "2026.7.28", timeout=0.2
                )

            self.assertEqual(compose.read_text(encoding="utf-8"), "old")
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_runtime_methods_reject_user_id_strings(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))

            with self.assertRaises(TypeError):
                adapter.get_runtime_target("alice")

    def test_product_capabilities_fail_closed(self):
        self.assertTrue(product_supports("openclaw", "restart"))
        self.assertFalse(product_supports("evoscientist", "file_upload"))
        self.assertFalse(product_supports("unknown", "restart"))
        self.assertEqual(
            execution_action_capability("instance.wechat_bind"),
            "device_pairing",
        )
        self.assertEqual(
            execution_action_capability("instance.set_basic_auth"),
            "basic_auth",
        )
        self.assertEqual(
            execution_action_capability("instance.update_version"),
            "update_version",
        )
        self.assertIsNone(execution_action_capability("shell.run"))


if __name__ == "__main__":
    unittest.main()
