#!/usr/bin/env python3

import importlib.util
import os
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.tls_fixtures import write_test_ca


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "scripts" / "migrate_hermes_uis_auth.py"
SCHEMA = ROOT_DIR / "db" / "schema.sql"

spec = importlib.util.spec_from_file_location("migrate_hermes_uis_auth", SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class HermesUISMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "manager.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript(SCHEMA.read_text())
            conn.execute(
                "INSERT INTO users(id,public_id,username,normalized_username) VALUES(1,?,?,?)",
                ("22222222-2222-2222-2222-222222222222", "alice", "alice"),
            )
            self.data = self.root / "hermes" / "alice"
            self.data.mkdir(parents=True)
            conn.execute(
                "INSERT INTO instances(id,public_id,owner_user_id,product,instance_name,runtime_identifier,status,access_url,data_path) "
                "VALUES(1,?,1,'hermes','Hermes','hermes_alice','active',?,?)",
                ("11111111-1111-1111-1111-111111111111", "https://manager.example.test:39119", str(self.data)),
            )
        (self.data / ".env").write_text(
            "KEEP=value\nHERMES_DASHBOARD_BASIC_AUTH_USERNAME=alice\n"
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET=secret\n", encoding="utf-8",
        )
        (self.data / "config.yaml").write_text("security: {}\n", encoding="utf-8")
        self.template = self.root / "templates" / "hermes" / "plugins" / "campus-uis-bridge"
        self.template.mkdir(parents=True)
        (self.template / "plugin.yaml").write_text("name: campus-uis-bridge\n")
        (self.template / "__init__.py").write_text("def register(ctx): pass\n")
        self.template.chmod(0o770)
        (self.template / "plugin.yaml").chmod(0o660)
        (self.template / "__init__.py").chmod(0o660)
        self.ca_file = write_test_ca(self.root / "manager-ca.crt")

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_does_not_change_files_or_clients(self):
        before = (self.data / ".env").read_bytes()
        with patch.object(migration, "ROOT_DIR", self.root):
            status = migration.main([
                "--db", str(self.db), "--instance", "11111111-1111-1111-1111-111111111111",
                "--issuer", "https://manager.example.test:30015/auth/hermes",
                "--ca-file", str(self.ca_file),
            ])
        self.assertEqual(status, 0)
        self.assertEqual((self.data / ".env").read_bytes(), before)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hermes_auth_clients").fetchone()[0], 0)

    def test_apply_removes_basic_auth_and_registers_one_client(self):
        success = types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        instance = migration.load_instance(self.db, "11111111-1111-1111-1111-111111111111")
        with patch.object(migration, "ROOT_DIR", self.root), patch.object(
            migration, "run", return_value=success
        ) as run, patch.object(migration.os, "chown") as chown:
            migration.apply(
                instance, self.db, "https://manager.example.test:30015/auth/hermes",
                self.ca_file,
            )
        env = (self.data / ".env").read_text()
        self.assertIn("KEEP=value", env)
        self.assertNotIn("HERMES_DASHBOARD_BASIC_AUTH", env)
        self.assertIn("HERMES_UIS_BRIDGE_CLIENT_SECRET=", env)
        self.assertIn(
            "HERMES_UIS_BRIDGE_REDIRECT_URI=https://manager.example.test:39119/auth/callback",
            env,
        )
        self.assertIn(
            "HERMES_UIS_BRIDGE_CA_FILE=/opt/data/manager-auth/bridge-ca.crt", env
        )
        staged_ca = self.data / "manager-auth" / "bridge-ca.crt"
        self.assertEqual(staged_ca.read_bytes(), self.ca_file.read_bytes())
        self.assertEqual(staged_ca.stat().st_mode & 0o777, 0o640)
        self.assertEqual(run.call_args_list[-1].args[0], ["docker", "restart", "hermes_alice"])
        with sqlite3.connect(self.db) as conn:
            client = conn.execute("SELECT client_secret_hash,redirect_uri FROM hermes_auth_clients").fetchone()
        self.assertTrue(client[0].startswith("scrypt$"))
        self.assertEqual(client[1], "https://manager.example.test:39119/auth/callback")
        backups = list((self.root / ".manager-auth-backups").glob("hermes-uis-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("HERMES_DASHBOARD_BASIC_AUTH", (backups[0] / ".env").read_text())
        self.assertEqual((backups[0] / ".env").stat().st_mode & 0o777, 0o600)
        plugin = self.data / "plugins" / "campus-uis-bridge"
        self.assertEqual(plugin.parent.stat().st_mode & 0o777, 0o750)
        self.assertEqual(plugin.stat().st_mode & 0o777, 0o750)
        self.assertEqual((plugin / "plugin.yaml").stat().st_mode & 0o777, 0o640)
        self.assertEqual((plugin / "__init__.py").stat().st_mode & 0o777, 0o640)
        self.assertTrue(any(call.args[1:] == (self.data.stat().st_uid, self.data.stat().st_gid)
                            for call in chown.call_args_list))

    def test_apply_reuses_matching_existing_client_to_repair_provider(self):
        secret = "s" * 48
        redirect_uri = "https://manager.example.test:39119/auth/callback"
        store = migration.BridgeStore(self.db)
        store.create_client(1, "existing-client", secret, redirect_uri)
        self.data.joinpath(".env").write_text(
            "KEEP=value\n"
            "HERMES_UIS_BRIDGE_ISSUER=https://manager.example.test:30015/auth/hermes\n"
            "HERMES_UIS_BRIDGE_CLIENT_ID=existing-client\n"
            f"HERMES_UIS_BRIDGE_CLIENT_SECRET={secret}\n"
            "HERMES_UIS_BRIDGE_INSTANCE_ID=11111111-1111-1111-1111-111111111111\n",
            encoding="utf-8",
        )
        success = types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        instance = migration.load_instance(
            self.db, "11111111-1111-1111-1111-111111111111"
        )
        with patch.object(migration, "ROOT_DIR", self.root), patch.object(
            migration, "run", return_value=success
        ), patch.object(migration.os, "chown"):
            migration.apply(
                instance, self.db,
                "https://manager.example.test:30015/auth/hermes",
                self.ca_file,
            )
        env = self.data.joinpath(".env").read_text(encoding="utf-8")
        self.assertIn("HERMES_UIS_BRIDGE_CLIENT_ID=existing-client", env)
        self.assertIn(f"HERMES_UIS_BRIDGE_CLIENT_SECRET={secret}", env)
        self.assertIn(f"HERMES_UIS_BRIDGE_REDIRECT_URI={redirect_uri}", env)
        self.assertIn(
            "HERMES_UIS_BRIDGE_CA_FILE=/opt/data/manager-auth/bridge-ca.crt", env
        )
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM hermes_auth_clients").fetchone()[0], 1
            )

    def test_apply_requires_root_before_writing_files(self):
        before = (self.data / ".env").read_bytes()
        with patch.object(migration, "ROOT_DIR", self.root), patch.object(
            migration.os, "geteuid", return_value=1000
        ):
            with self.assertRaises(SystemExit):
                migration.main([
                    "--db", str(self.db),
                    "--instance", "11111111-1111-1111-1111-111111111111",
                    "--issuer", "https://manager.example.test:30015/auth/hermes",
                    "--ca-file", str(self.ca_file),
                    "--apply",
                ])
        self.assertEqual((self.data / ".env").read_bytes(), before)
        self.assertFalse((self.data.parent.parent / ".manager-auth-backups").exists())

    def test_restart_failure_restores_files_and_deletes_client(self):
        before_env = (self.data / ".env").read_bytes()
        before_config = (self.data / "config.yaml").read_bytes()
        results = [
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),
            types.SimpleNamespace(returncode=1, stdout="", stderr="restart failed"),
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        instance = migration.load_instance(self.db, "11111111-1111-1111-1111-111111111111")
        with patch.object(migration, "ROOT_DIR", self.root), patch.object(
            migration, "run", side_effect=results
        ):
            with self.assertRaises(RuntimeError):
                migration.apply(
                    instance, self.db,
                    "https://manager.example.test:30015/auth/hermes", self.ca_file,
                )
        self.assertEqual((self.data / ".env").read_bytes(), before_env)
        self.assertEqual((self.data / "config.yaml").read_bytes(), before_config)
        self.assertFalse((self.data / "plugins").exists())
        self.assertFalse((self.data / "manager-auth").exists())
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hermes_auth_clients").fetchone()[0], 0)

    def test_restart_failure_restores_existing_ca(self):
        old_ca = self.data / "manager-auth" / "bridge-ca.crt"
        old_ca.parent.mkdir()
        old_ca.parent.chmod(0o750)
        write_test_ca(old_ca)
        old_ca.chmod(0o640)
        before = old_ca.read_bytes()
        results = [
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),
            types.SimpleNamespace(returncode=1, stdout="", stderr="restart failed"),
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        instance = migration.load_instance(
            self.db, "11111111-1111-1111-1111-111111111111"
        )
        with patch.object(migration, "ROOT_DIR", self.root), patch.object(
            migration, "run", side_effect=results
        ), patch.object(migration.os, "chown"):
            with self.assertRaises(RuntimeError):
                migration.apply(
                    instance, self.db,
                    "https://manager.example.test:30015/auth/hermes", self.ca_file,
                )
        self.assertEqual(old_ca.read_bytes(), before)
        self.assertEqual(old_ca.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
