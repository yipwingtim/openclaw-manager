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
                args=[], returncode=0, stdout="Up 5 minutes\n", stderr=""
            )

            with patch.object(
                subprocess,
                "run",
                return_value=completed,
            ) as run:
                self.assertEqual(adapter.status(instance), "Up 5 minutes")

            self.assertEqual(
                run.call_args.args[0],
                [
                    "docker",
                    "ps",
                    "--filter",
                    "name=^openclaw_alice_custom$",
                    "--format",
                    "{{.Status}}",
                ],
            )

    def test_runtime_methods_reject_user_id_strings(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))

            with self.assertRaises(TypeError):
                adapter.get_runtime_target("alice")


if __name__ == "__main__":
    unittest.main()
