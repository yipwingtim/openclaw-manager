#!/usr/bin/env python3

import importlib.util
import json
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
    INSTANCE = {
        "public_id": "instance-1",
        "legacy_user_id": "alice",
        "runtime_identifier": "evoscientist_alice",
        "data_path": "/unused/public/users/alice",
        "port": 40062,
    }

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
                code, _ = adapter.restart(self.INSTANCE)

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
                    code, _ = adapter.stop(self.INSTANCE)

            self.assertEqual(code, 0)
            self.assertEqual(
                commands,
                [
                    ["docker", "stop", "evoscientist_alice-proxy"],
                    ["docker", "stop", "evoscientist_alice"],
                ],
            )

    def test_start_without_legacy_user_id_skips_nginx(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            instance = {"runtime_identifier": "evoscientist.project-1"}

            with patch.object(adapter, "run_command", return_value=(0, "started")) as run, patch.object(
                adapter, "enable_nginx_user_conf"
            ) as enable_nginx:
                result = adapter.start(instance)

            self.assertEqual(result, (0, ""))
            self.assertEqual(run.call_count, 2)
            enable_nginx.assert_not_called()

    def test_status_is_degraded_when_proxy_is_stopped(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            with patch.object(
                adapter,
                "_container_status",
                side_effect=["Up 10 minutes", "STOPPED"],
            ):
                status = adapter.status(self.INSTANCE)

            self.assertTrue(status.startswith("DEGRADED"))
            self.assertIn("evoscientist_alice-proxy=STOPPED", status)

    def test_update_version_rejects_mutable_tag(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))

            code, output = adapter.update_version(self.INSTANCE, "latest")

            self.assertNotEqual(code, 0)
            self.assertIn("digest", output)

    def test_create_runs_main_then_network_namespace_proxy(self):
        digest = "sha256:" + "a" * 64
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            instance = dict(self.INSTANCE, data_path=str(root / "public" / "users" / "alice"))
            commands = []

            def run_command(command, **kwargs):
                commands.append(command)
                if command[:3] == ["docker", "network", "inspect"]:
                    return 1, "missing"
                if "allocate_port" in command:
                    return 0, "40062"
                return 0, "ok"

            with patch.dict(os.environ, {"EVOSCIENTIST_IMAGE": f"ghcr.io/evoscientist/evoscientist@{digest}"}), patch.object(
                adapter, "run_command", side_effect=run_command
            ), patch.object(adapter, "_write_htpasswd", return_value=None), patch.object(
                adapter, "_wait_for_services", return_value=(0, "ready")
            ):
                code, _ = adapter.create(instance, "true", "secret")

            self.assertEqual(code, 0)
            docker_runs = [command for command in commands if command[:2] == ["docker", "run"]]
            self.assertEqual(len(docker_runs), 2)
            self.assertIn("--network", docker_runs[0])
            self.assertIn("--network", docker_runs[1])
            self.assertIn("container:evoscientist_alice", docker_runs[1])
            self.assertEqual(instance["_created_port"], 40062)
            self.assertTrue((root / "public" / "users" / "alice" / "tcp_proxy.py").is_file())

    def test_update_version_requires_local_digest(self):
        digest = "sha256:" + "b" * 64
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            instance = dict(self.INSTANCE, data_path=str(root / "public" / "users" / "alice"))
            (root / "public" / "users" / "alice").mkdir(parents=True)
            with patch.object(
                adapter,
                "_inspect_deployment",
                return_value=("sha256:" + "a" * 64, True, "tenant-net"),
            ), patch.object(adapter, "run_command", return_value=(1, "missing")):
                code, output = adapter.update_version(instance, digest)

            self.assertNotEqual(code, 0)
            self.assertIn(f"ghcr.io/evoscientist/evoscientist@{digest}", output)

    def test_create_failure_removes_new_data_directories(self):
        digest = "sha256:" + "a" * 64
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            user_dir = root / "public" / "users" / "alice"
            instance = dict(self.INSTANCE, data_path=str(user_dir))
            with patch.dict(os.environ, {"EVOSCIENTIST_IMAGE": f"ghcr.io/evoscientist/evoscientist@{digest}"}), patch.object(
                adapter, "run_command", side_effect=[(1, "missing"), (0, "prepared"), (1, "run failed"), (0, "missing"), (0, "removed"), (0, "network removed")]
            ), patch.object(adapter, "_write_htpasswd"):
                code, _ = adapter.create(instance, "true", "secret")

            self.assertNotEqual(code, 0)
            self.assertFalse(user_dir.exists())

    def test_update_version_stops_when_container_removal_fails(self):
        digest = "sha256:" + "b" * 64
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            with patch.object(
                adapter, "_inspect_deployment", return_value=("sha256:" + "a" * 64, True, "tenant-net")
            ), patch.object(adapter, "run_command", return_value=(0, "present")), patch.object(
                adapter, "_remove_containers", return_value=(1, "remove failed")
            ), patch.object(adapter, "_run_containers") as run:
                code, output = adapter.update_version(self.INSTANCE, digest)

            self.assertNotEqual(code, 0)
            self.assertIn("remove failed", output)
            run.assert_not_called()


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
