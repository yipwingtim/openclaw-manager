#!/usr/bin/env python3

import importlib.util
import os
import sqlite3
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


if __name__ == "__main__":
    unittest.main()
