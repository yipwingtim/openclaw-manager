#!/usr/bin/env python3

import importlib.util
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "scripts" / "migrate_instance_auth.py"


def load_script():
    spec = importlib.util.spec_from_file_location("migrate_instance_auth", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrateInstanceAuthTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script()
        self.module.PUBLIC_HOST = "manager.example.test"

    def test_evoscientist_config_migration_is_idempotent(self):
        old = (
            "upstream evosci_ui_40001 { server evo:4716; }\n"
            "server {\n"
            "    auth_basic \"OpenClaw Login\";\n"
            "    auth_basic_user_file /etc/nginx/auth/users/alice/.htpasswd;\n"
            "    location / { proxy_pass http://evosci_ui_40001; }\n"
            "}\n"
        )
        updated = self.module.migrate_config(old, "instance-1", "evoscientist")

        self.assertEqual(updated, self.module.migrate_config(updated, "instance-1", "evoscientist"))
        self.assertIn("auth_request /_instance_auth;", updated)
        self.assertIn("/login?instance=instance-1", updated)
        self.assertNotIn("auth_basic_user_file", updated)

    def test_hermes_migration_keeps_dashboard_auth_out_of_scope(self):
        old = (
            "upstream hermes_backend_39119 { server hermes:9119; }\n"
            "server {\n    location / {\n        proxy_pass http://hermes_backend_39119;\n    }\n}\n"
        )
        updated = self.module.migrate_config(old, "hermes-1", "hermes")

        self.assertIn("auth_request /_instance_auth;", updated)
        self.assertIn("proxy_pass http://hermes_backend_39119;", updated)

    def test_shared_nginx_compose_network_migration_is_idempotent(self):
        old = (
            "services:\n  nginx:\n    networks:\n      - manager-net\n"
            "networks:\n  manager-net:\n    external: true\n"
        )
        updated = self.module.ensure_compose_network(old)

        self.assertEqual(updated, self.module.ensure_compose_network(updated))
        self.assertEqual(updated.count("      - instance-auth-net\n"), 1)
        self.assertEqual(updated.count("  instance-auth-net:\n"), 1)

    def test_apply_rolls_back_config_and_network_when_nginx_test_fails(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.module.PUBLIC_DIR = root
            path = root / "deleted" / "evoscientist" / "instance-1.nginx.conf"
            path.parent.mkdir(parents=True)
            old = "server {\n    location / { proxy_pass http://evo; }\n}\n"
            path.write_text(old, encoding="utf-8")
            instance = {
                "public_id": "instance-1", "product": "evoscientist",
                "runtime_identifier": "evoscientist_alice", "status": "active",
            }
            commands = []

            def run(command):
                commands.append(command)
                if command[:2] == ["docker", "inspect"]:
                    return 0, "running|tenant-net"
                if command[-2:] == ["nginx", "-t"] and len(commands) < 5:
                    return 1, "invalid"
                return 0, "ok"

            backup = root / "backup"
            backup.mkdir()
            with patch.object(self.module, "run", side_effect=run):
                with self.assertRaises(RuntimeError):
                    self.module.apply_one(instance, backup)

            self.assertEqual(path.read_text(encoding="utf-8"), old)
            self.assertIn(
                ["docker", "network", "disconnect", "instance-auth-net", "evoscientist_alice-ingress"],
                commands,
            )

    def test_apply_preflight_failure_makes_no_changes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.module.PUBLIC_DIR = root / "public"
            self.module.NGINX_CONF_DIR = root / "nginx"
            self.module.NGINX_COMPOSE_FILE = root / "compose" / "docker-compose.yml"
            evo = self.module.PUBLIC_DIR / "deleted" / "evoscientist" / "evo.nginx.conf"
            hermes = self.module.NGINX_CONF_DIR / "hermes-hermes.conf"
            evo.parent.mkdir(parents=True)
            hermes.parent.mkdir(parents=True)
            self.module.NGINX_COMPOSE_FILE.parent.mkdir(parents=True)
            config = "server {\n    location / { proxy_pass http://agent; }\n}\n"
            compose = (
                "services:\n  nginx:\n    networks:\n      - manager-net\n"
                "networks:\n  manager-net:\n    external: true\n"
            )
            evo.write_text(config, encoding="utf-8")
            hermes.write_text(config, encoding="utf-8")
            self.module.NGINX_COMPOSE_FILE.write_text(compose, encoding="utf-8")
            rows = [
                {
                    "public_id": "evo", "product": "evoscientist",
                    "runtime_identifier": "evoscientist_evo", "status": "active",
                },
                {
                    "public_id": "hermes", "product": "hermes",
                    "runtime_identifier": "hermes", "status": "active",
                },
            ]
            commands = []

            def run(command):
                commands.append(command)
                return 0, "ok"

            real_access = os.access

            def access(path, mode):
                return False if Path(path) == evo else real_access(path, mode)

            with (
                patch.object(self.module, "instances", return_value=rows),
                patch.object(self.module, "run", side_effect=run),
                patch.object(self.module.os, "access", side_effect=access),
                patch.object(sys, "argv", [str(SCRIPT), "--apply"]),
                self.assertRaisesRegex(SystemExit, "Run this command through manager-executor"),
            ):
                self.module.main()

            self.assertEqual(evo.read_text(encoding="utf-8"), config)
            self.assertEqual(hermes.read_text(encoding="utf-8"), config)
            self.assertEqual(self.module.NGINX_COMPOSE_FILE.read_text(encoding="utf-8"), compose)
            self.assertFalse((self.module.PUBLIC_DIR / ".manager-auth-backups").exists())
            self.assertFalse(any(command[:2] == ["docker", "compose"] for command in commands))
            self.assertFalse(any(command[:3] == ["docker", "network", "connect"] for command in commands))
            self.assertFalse(any(command[-3:] == ["nginx", "-s", "reload"] for command in commands))


if __name__ == "__main__":
    unittest.main()
