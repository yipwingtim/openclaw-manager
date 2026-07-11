#!/usr/bin/env python3

import os
import shutil
import sqlite3
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
METADATA_CLI = ROOT_DIR / "scripts" / "metadata_cli.py"
UPDATE_SCRIPT = ROOT_DIR / "scripts" / "update_instance_version.sh"


class UpgradeMetadataConsistencyTests(unittest.TestCase):
    def metadata_env(self, root):
        public_dir = root / "public"
        public_dir.mkdir()
        env = os.environ.copy()
        env["OPENCLAW_PUBLIC_DIR"] = str(public_dir)
        env["METADATA_DB_FILE"] = str(public_dir / "manager.db")
        env["METADATA_SCHEMA_FILE"] = str(ROOT_DIR / "db" / "schema.sql")
        return env

    def run_metadata(self, env, *args):
        return subprocess.run(
            ["python3", str(METADATA_CLI), *args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_update_version_preserves_stopped_status(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.metadata_env(root)

            created = self.run_metadata(
                env,
                "create-instance",
                "--user-id",
                "alice",
                "--openclaw-version",
                "2026.5.26",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            stopped = self.run_metadata(
                env,
                "set-instance-status",
                "--user-id",
                "alice",
                "--status",
                "stopped",
                "--action",
                "stop_instance",
            )
            self.assertEqual(stopped.returncode, 0, stopped.stderr)

            updated = self.run_metadata(
                env,
                "update-version",
                "--user-id",
                "alice",
                "--openclaw-version",
                "2026.6.11",
            )

            self.assertEqual(updated.returncode, 0, updated.stderr)
            with sqlite3.connect(env["METADATA_DB_FILE"]) as conn:
                row = conn.execute(
                    "SELECT status, openclaw_version FROM instances WHERE user_id = ?",
                    ("alice",),
                ).fetchone()
            self.assertEqual(row, ("stopped", "2026.6.11"))

    def make_upgrade_fixture(self, root, metadata_exit_code):
        manager = root / "manager"
        scripts = manager / "scripts"
        config = manager / "config"
        public_dir = root / "public"
        user_dir = public_dir / "users" / "alice"
        scripts.mkdir(parents=True)
        config.mkdir()
        user_dir.mkdir(parents=True)
        shutil.copy2(UPDATE_SCRIPT, scripts / "update_instance_version.sh")
        (scripts / "metadata_cli.py").write_text(
            textwrap.dedent(
                f"""
                import sys
                print("metadata sync invoked")
                raise SystemExit({metadata_exit_code})
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (config / "openclaw-manager.env").write_text(
            f"OPENCLAW_PUBLIC_DIR={public_dir}\n",
            encoding="utf-8",
        )
        (user_dir / "docker-compose.yml").write_text(
            "services:\n"
            "  openclaw-alice:\n"
            "    image: ghcr.io/openclaw/openclaw:2026.6.11\n",
            encoding="utf-8",
        )
        return scripts / "update_instance_version.sh"

    def run_upgrade(self, script):
        return subprocess.run(
            ["bash", str(script), "alice", "2026.6.11"],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_already_target_version_reconciles_metadata(self):
        with TemporaryDirectory() as temp_dir:
            script = self.make_upgrade_fixture(Path(temp_dir), metadata_exit_code=0)

            result = self.run_upgrade(script)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("metadata sync invoked", result.stdout)
            self.assertIn("Instance already uses target image", result.stdout)

    def test_metadata_failure_does_not_report_upgrade_success(self):
        with TemporaryDirectory() as temp_dir:
            script = self.make_upgrade_fixture(Path(temp_dir), metadata_exit_code=1)

            result = self.run_upgrade(script)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Metadata update failed", result.stdout + result.stderr)
            self.assertNotIn("UPDATE SUCCESS", result.stdout)

    def test_upgrade_success_is_printed_after_metadata_sync(self):
        script = UPDATE_SCRIPT.read_text(encoding="utf-8")
        success_index = script.index('echo "UPDATE SUCCESS"')
        sync_index = script.rfind(
            "if ! sync_metadata_version;",
            0,
            success_index,
        )
        self.assertGreater(sync_index, 0)
        self.assertNotIn('|| echo "[WARN] Metadata update failed', script)


if __name__ == "__main__":
    unittest.main()
