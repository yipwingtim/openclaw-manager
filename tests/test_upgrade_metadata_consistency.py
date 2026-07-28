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
                    "SELECT status, openclaw_version FROM instances WHERE legacy_user_id = ?",
                    ("alice",),
                ).fetchone()
            self.assertEqual(row, ("stopped", "2026.6.11"))

    def test_deleted_instance_restore_reuses_record_and_active_data_path(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.metadata_env(root)
            public_dir = Path(env["OPENCLAW_PUBLIC_DIR"])
            recycle_user = public_dir / "deleted" / "alice_20260722_120000" / "user"
            recycle_user.mkdir(parents=True)
            (recycle_user / "docker-compose.yml").write_text(
                "services:\n  openclaw-alice:\n",
                encoding="utf-8",
            )
            recycle_nginx = recycle_user.parent / "nginx"
            recycle_nginx.mkdir()
            (recycle_nginx / "alice.conf").write_text(
                "server {\n    listen 30021;\n}\n", encoding="utf-8"
            )

            created = self.run_metadata(env, "create-instance", "--user-id", "alice")
            self.assertEqual(created.returncode, 0, created.stderr)
            deleted = self.run_metadata(
                env,
                "set-instance-status",
                "--user-id",
                "alice",
                "--status",
                "deleted",
                "--action",
                "delete_instance",
            )
            self.assertEqual(deleted.returncode, 0, deleted.stderr)

            active_user_dir = public_dir / "users" / "alice"
            active_user_dir.mkdir(parents=True)
            restored = self.run_metadata(
                env,
                "set-instance-status",
                "--user-id",
                "alice",
                "--status",
                "active",
                "--action",
                "restore_instance",
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)

            with sqlite3.connect(env["METADATA_DB_FILE"]) as conn:
                rows = conn.execute(
                    """
                    SELECT id, status, restore_state, data_path, deleted_at
                    FROM instances WHERE legacy_user_id = 'alice'
                    """
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0][1:],
                ("active", "not_applicable", str(active_user_dir), None),
            )

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

    def run_upgrade(self, script, env=None):
        return subprocess.run(
            ["bash", str(script), "alice", "2026.6.11"],
            text=True,
            capture_output=True,
            env=env,
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

    def test_executor_mode_skips_metadata_sync(self):
        with TemporaryDirectory() as temp_dir:
            script = self.make_upgrade_fixture(Path(temp_dir), metadata_exit_code=1)
            env = os.environ.copy()
            env["OPENCLAW_SKIP_METADATA_WRITE"] = "1"

            result = self.run_upgrade(script, env=env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("metadata sync invoked", result.stdout)

    def test_upgrade_failure_restores_compose_and_recreates_old_container(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = self.make_upgrade_fixture(root, metadata_exit_code=0)
            user_dir = root / "public" / "users" / "alice"
            compose = user_dir / "docker-compose.yml"
            compose.write_text(
                compose.read_text(encoding="utf-8").replace("2026.6.11", "2026.5.1"),
                encoding="utf-8",
            )
            instance_config = user_dir / "config" / "openclaw.json"
            instance_config.parent.mkdir()
            instance_config.write_text("old config", encoding="utf-8")
            skill = user_dir / "skills" / "skill.txt"
            skill.parent.mkdir()
            skill.write_text("old skill", encoding="utf-8")
            token_dir = root / "tokens"
            token_dir.mkdir()
            token = token_dir / "alice.token"
            models = token_dir / "alice.models"
            token.write_text("old token", encoding="utf-8")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1 $2\" = \"image inspect\" ]; then exit 0; fi\n"
                "if [ \"$1 $2 $3\" = \"compose config --services\" ]; then echo app; exit 0; fi\n"
                "if [ \"$1 $2 $3\" = \"compose up -d\" ]; then\n"
                "  count_file=\"$FAKE_ROOT/up-count\"\n"
                "  count=$(cat \"$count_file\" 2>/dev/null || echo 0)\n"
                "  count=$((count + 1)); echo \"$count\" > \"$count_file\"\n"
                "  if [ \"$count\" = 1 ]; then\n"
                "    printf changed > \"$FAKE_USER_DIR/config/openclaw.json\"\n"
                "    printf changed > \"$FAKE_USER_DIR/skills/skill.txt\"\n"
                "    mkdir -p \"$FAKE_USER_DIR/extensions\"\n"
                "    printf created > \"$FAKE_USER_DIR/extensions/new.txt\"\n"
                "    printf changed > \"$MODEL_PROXY_TOKEN_DIR/alice.token\"\n"
                "    printf created > \"$MODEL_PROXY_TOKEN_DIR/alice.models\"\n"
                "    exit 1\n"
                "  fi\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "FAKE_ROOT": str(root),
                    "FAKE_USER_DIR": str(user_dir),
                    "MODEL_PROXY_TOKEN_DIR": str(token_dir),
                    "OPENCLAW_SKIP_METADATA_WRITE": "1",
                }
            )

            result = subprocess.run(
                ["bash", str(script), "alice", "2026.6.11"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("2026.5.1", compose.read_text(encoding="utf-8"))
            self.assertEqual(instance_config.read_text(encoding="utf-8"), "old config")
            self.assertEqual(skill.read_text(encoding="utf-8"), "old skill")
            self.assertFalse((user_dir / "extensions").exists())
            self.assertEqual(token.read_text(encoding="utf-8"), "old token")
            self.assertFalse(models.exists())
            self.assertEqual((root / "up-count").read_text().strip(), "2")
            failed_data = next(user_dir.glob("backups/version-upgrades/*/failed-upgrade-data"))
            self.assertEqual(
                (failed_data / "skills" / "skill.txt").read_text(encoding="utf-8"),
                "changed",
            )

    def test_persistent_restore_failure_does_not_start_old_container(self):
        script = UPDATE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('if ! cp -a "$PERSISTENT_BACKUP_DIR/$relative_path"', script)
        self.assertIn("Old container was not started with incomplete persistent data", script)

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
