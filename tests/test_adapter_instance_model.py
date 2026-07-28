import importlib.util
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
        self.assertIsNone(execution_action_capability("shell.run"))


if __name__ == "__main__":
    unittest.main()
