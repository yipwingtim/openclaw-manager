#!/usr/bin/env python3

import importlib.util
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))

from instance_adapters import EvoScientistDockerAdapter


class EvoScientistAdapterTests(unittest.TestCase):
    def make_adapter(self, root):
        return EvoScientistDockerAdapter(
            manager_dir=root,
            public_dir=root / "public",
            nginx_users_conf_dir=root / "nginx" / "conf",
            nginx_compose_dir=root / "nginx" / "compose",
            nginx_container_name="openclaw-nginx",
        )

    def test_restart_restarts_main_then_proxy(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            commands = []

            def run_command(command, **kwargs):
                commands.append(command)
                return 0, command[-1]

            with patch.object(adapter, "run_command", side_effect=run_command):
                code, _ = adapter.restart("alice")

            self.assertEqual(code, 0)
            self.assertEqual(
                commands,
                [
                    ["docker", "restart", "evoscientist_alice"],
                    ["docker", "restart", "evoscientist_alice-proxy"],
                ],
            )

    def test_stop_stops_proxy_before_main(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            commands = []

            def run_command(command, **kwargs):
                commands.append(command)
                return 0, command[-1]

            with patch.object(adapter, "disable_nginx_user_conf", return_value=(0, "disabled")):
                with patch.object(adapter, "run_command", side_effect=run_command):
                    code, _ = adapter.stop("alice")

            self.assertEqual(code, 0)
            self.assertEqual(
                commands,
                [
                    ["docker", "stop", "evoscientist_alice-proxy"],
                    ["docker", "stop", "evoscientist_alice"],
                ],
            )

    def test_status_is_degraded_when_proxy_is_stopped(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            with patch.object(
                adapter,
                "_container_status",
                side_effect=["Up 10 minutes", "STOPPED"],
            ):
                status = adapter.status("alice")

            self.assertTrue(status.startswith("DEGRADED"))
            self.assertIn("evoscientist_alice-proxy=STOPPED", status)

    def test_openclaw_only_actions_are_explicitly_unsupported(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))

            code, output = adapter.update_version("alice", "2026.6.11")

            self.assertNotEqual(code, 0)
            self.assertIn("not supported", output)


class EvoScientistRegistrationTests(unittest.TestCase):
    def test_register_instance_persists_product_container_and_detected_port(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            public_dir = root / "public"
            nginx_dir = root / "nginx"
            user_dir = public_dir / "users" / "alice"
            user_dir.mkdir(parents=True)
            nginx_dir.mkdir()
            (nginx_dir / "alice.conf").write_text(
                "server {\n  listen 40062 ssl;\n}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["OPENCLAW_PUBLIC_DIR"] = str(public_dir)
            env["NGINX_USERS_CONF_DIR"] = str(nginx_dir)
            env["METADATA_DB_FILE"] = str(public_dir / "manager.db")
            env["METADATA_SCHEMA_FILE"] = str(ROOT_DIR / "db" / "schema.sql")

            result = subprocess.run(
                [
                    "python3",
                    str(ROOT_DIR / "scripts" / "metadata_cli.py"),
                    "register-instance",
                    "--user-id",
                    "alice",
                    "--product",
                    "evoscientist",
                    "--container-name",
                    "evoscientist_alice",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(public_dir / "manager.db") as conn:
                row = conn.execute(
                    "SELECT product, status, container_name, port "
                    "FROM instances WHERE legacy_user_id = ?",
                    ("alice",),
                ).fetchone()
            self.assertEqual(
                row,
                ("evoscientist", "active", "evoscientist_alice", 40062),
            )


if __name__ == "__main__":
    unittest.main()
