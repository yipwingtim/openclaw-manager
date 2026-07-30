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
    INSTANCE = {"runtime_identifier": "hermes-alice"}

    def make_adapter(self, root):
        return HermesDockerAdapter(
            manager_dir=root,
            public_dir=root / "public",
            nginx_users_conf_dir=root / "nginx" / "conf",
            nginx_compose_dir=root / "nginx" / "compose",
            nginx_container_name="openclaw-nginx",
        )

    def test_start_and_stop_only_manage_registered_container(self):
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

if __name__ == "__main__":
    unittest.main()
