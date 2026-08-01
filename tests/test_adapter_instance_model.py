import importlib.util
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


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

    def test_set_basic_auth_restores_nginx_config_when_reload_fails(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            conf = root / "nginx" / "conf" / "alice.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text("original", encoding="utf-8")

            def update_config(*args, **kwargs):
                conf.write_text("changed", encoding="utf-8")
                return 0, "updated"

            with patch.object(
                adapter, "run_command", side_effect=update_config
            ) as run, patch.object(
                adapter,
                "reload_nginx",
                side_effect=[(1, "nginx test failed"), (0, "restored")],
            ) as reload_nginx:
                code, output = adapter.set_basic_auth(
                    {"legacy_user_id": "alice"}, False
                )

            self.assertEqual(code, 1)
            self.assertEqual(conf.read_text(encoding="utf-8"), "original")
            self.assertIn("Restored Nginx config", output)
            self.assertEqual(reload_nginx.call_count, 2)
            self.assertEqual(
                run.call_args.kwargs["env"]["OPENCLAW_SKIP_METADATA_WRITE"], "1"
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["OPENCLAW_SKIP_HTPASSWD_PERMISSIONS"],
                "1",
            )

    def test_set_basic_auth_restores_nginx_config_when_reload_raises(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            conf = root / "nginx" / "conf" / "alice.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text("original", encoding="utf-8")

            def update_config(*args, **kwargs):
                conf.write_text("changed", encoding="utf-8")
                return 0, "updated"

            with patch.object(
                adapter, "run_command", side_effect=update_config
            ), patch.object(
                adapter,
                "reload_nginx",
                side_effect=[subprocess.TimeoutExpired("nginx", 30), (0, "restored")],
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    adapter.set_basic_auth({"legacy_user_id": "alice"}, True)

            self.assertEqual(conf.read_text(encoding="utf-8"), "original")

    def test_update_version_restores_compose_after_timeout(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            user_dir = root / "public" / "users" / "alice"
            user_dir.mkdir(parents=True)
            compose = user_dir / "docker-compose.yml"
            compose.write_text("old", encoding="utf-8")
            script = root / "scripts" / "update_instance_version.sh"
            child_pid_file = root / "child.pid"
            script.parent.mkdir()
            script.write_text(
                "#!/usr/bin/env bash\n"
                f"trap 'printf old > {compose}' EXIT\n"
                f"printf new > {compose}\n"
                "sleep 60 &\n"
                f"echo $! > {child_pid_file}\n"
                "wait\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            with self.assertRaises(subprocess.TimeoutExpired):
                adapter.update_version(
                    {"legacy_user_id": "alice"}, "2026.7.28", timeout=0.2
                )

            self.assertEqual(compose.read_text(encoding="utf-8"), "old")
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_install_skill_uses_resolved_runtime_target(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            (root / "public" / "users" / "alice" / "skills").mkdir(parents=True)
            with patch.object(adapter, "_validate_skill_install", return_value=(0, "verified")), patch("subprocess.Popen") as process:
                process.return_value.communicate.return_value = ("installed\n", None)
                process.return_value.returncode = 0
                process.return_value.poll.return_value = 0
                result = adapter.install_skill(
                    {"legacy_user_id": "alice", "runtime_identifier": "openclaw_alice"},
                    "weather@1.0",
                    request_id="skill-1",
                )
            command = process.call_args.args[0]
            self.assertEqual(result, (0, "installed"))
            self.assertEqual(
                command,
                [
                    "docker", "exec", "openclaw_alice", "timeout", "180s",
                    "openclaw", "skills", "install", "weather@1.0",
                ],
            )
            self.assertTrue(process.call_args.kwargs["start_new_session"])
            self.assertEqual(
                process.return_value.communicate.call_args.kwargs["timeout"], 190
            )

    def test_install_skill_restores_skill_data_on_failure(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            skills = root / "public" / "users" / "alice" / "skills"
            skills.mkdir(parents=True)
            (skills / "existing.txt").write_text("old", encoding="utf-8")

            process = subprocess.CompletedProcess([], 1, "failed", "")
            def popen(*args, **kwargs):
                (skills / "existing.txt").write_text("changed", encoding="utf-8")
                (skills / "new.txt").write_text("partial", encoding="utf-8")
                mock = Mock()
                mock.communicate.return_value = ("failed", None)
                mock.returncode = process.returncode
                return mock

            with patch.object(adapter, "_validate_skill_install", return_value=(0, "verified")), patch("subprocess.Popen", side_effect=popen):
                code, output = adapter.install_skill(
                    {"legacy_user_id": "alice", "runtime_identifier": "openclaw_alice"},
                    "weather@1.0",
                    request_id="skill-failed",
                )

            self.assertEqual(code, 1)
            self.assertEqual((skills / "existing.txt").read_text(), "old")
            self.assertFalse((skills / "new.txt").exists())
            self.assertIn("Skill data restored", output)

    def test_install_skill_reuses_original_snapshot_after_interruption(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            skills = root / "public" / "users" / "alice" / "skills"
            skills.mkdir(parents=True)
            (skills / "existing.txt").write_text("old", encoding="utf-8")
            instance = {"legacy_user_id": "alice", "runtime_identifier": "openclaw_alice"}

            process = Mock()
            process.communicate.return_value = ("installed", None)
            process.returncode = 0
            process.poll.return_value = 0
            with patch.object(adapter, "_validate_skill_install", return_value=(0, "verified")), patch("subprocess.Popen", return_value=process):
                adapter.install_skill(instance, "weather@1.0", request_id="skill-retry")

            (skills / "existing.txt").write_text("partial", encoding="utf-8")
            with patch.object(adapter, "_validate_skill_install", return_value=(0, "verified")), patch("subprocess.Popen", return_value=process):
                adapter.install_skill(instance, "weather@1.0", request_id="skill-retry")

            snapshot_key = hashlib.sha256(b"skill-retry").hexdigest()
            failed = root / "public" / "users" / "alice" / "backups" / "skill-installs" / snapshot_key / "failed-install-data"
            self.assertEqual((failed / "existing.txt").read_text(), "partial")
            self.assertEqual((skills / "existing.txt").read_text(), "old")

    def test_install_skill_hashes_request_id_for_snapshot_path(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = self.make_adapter(root)
            skills = root / "public" / "users" / "alice" / "skills"
            skills.mkdir(parents=True)
            process = Mock()
            process.communicate.return_value = ("installed", None)
            process.returncode = 0
            process.poll.return_value = 0
            with patch.object(adapter, "_validate_skill_install", return_value=(0, "verified")), patch("subprocess.Popen", return_value=process):
                adapter.install_skill(
                    {"legacy_user_id": "alice", "runtime_identifier": "openclaw_alice"},
                    "weather@1.0",
                    request_id="..",
                )
            snapshot_root = skills.parent / "backups" / "skill-installs"
            self.assertTrue((snapshot_root / hashlib.sha256(b"..").hexdigest()).is_dir())
            self.assertFalse((skills.parent / "backups" / "skills").exists())

    def test_install_skill_rejects_bundled_skill(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            info = '{"source":"openclaw-bundled","bundled":true,"missing":{"bins":["gh"]}}'
            with patch.object(adapter, "run_command", return_value=(0, info)):
                code, output = adapter._validate_skill_install("openclaw_alice", "github")

            self.assertEqual(code, 1)
            self.assertIn("already bundled", output)
            self.assertIn("gh", output)

    def test_install_skill_rejects_duplicate_clawhub_slug(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            search = (
                '{"results":['
                '{"slug":"github","install":{"reference":"steipete/github"}},'
                '{"slug":"github","install":{"reference":"eohmig/github"}}]}'
            )
            with patch.object(
                adapter,
                "run_command",
                side_effect=[(1, "Skill not found"), (0, search)],
            ):
                code, output = adapter._validate_skill_install("openclaw_alice", "github")

            self.assertEqual(code, 1)
            self.assertIn("ambiguous", output)
            self.assertIn("steipete/github", output)
            self.assertIn("eohmig/github", output)

    def test_install_skill_rejects_duplicate_results_with_same_reference(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            search = (
                '{"results":['
                '{"slug":"github","install":{"reference":"steipete/github"}},'
                '{"slug":"github","install":{"reference":"steipete/github"}}]}'
            )
            with patch.object(
                adapter,
                "run_command",
                side_effect=[(1, "Skill not found"), (0, search)],
            ):
                code, output = adapter._validate_skill_install("openclaw_alice", "github")

            self.assertEqual(code, 1)
            self.assertIn("ambiguous", output)

    def test_install_skill_accepts_unique_clawhub_slug(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            search = (
                '{"results":['
                '{"slug":"weather","install":{"reference":"trusted/weather"}},'
                '{"slug":"weather-tools","install":{"reference":"other/weather-tools"}}]}'
            )
            with patch.object(
                adapter,
                "run_command",
                side_effect=[(1, "Skill not found"), (0, search)],
            ):
                result = adapter._validate_skill_install("openclaw_alice", "weather")

            self.assertEqual(result, (0, "Verified unique Skill source: trusted/weather"))

    def test_device_actions_use_server_resolved_instance_targets(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            instance = {
                "legacy_user_id": "alice",
                "runtime_identifier": "openclaw_custom_runtime",
            }
            process = Mock()
            process.communicate.return_value = ("ok", None)
            process.returncode = 0
            process.poll.return_value = 0
            with patch("subprocess.Popen", return_value=process) as popen:
                adapter.refresh_devices(instance)
                refresh = popen.call_args
                adapter.approve_latest_device(instance, request_id="devices-1")
                approve = popen.call_args

            self.assertEqual(refresh.args[0][-1], "--list-only")
            self.assertEqual(approve.args[0][-1], "--latest")
            self.assertEqual(
                refresh.kwargs["env"]["OPENCLAW_RUNTIME_TARGET"],
                "openclaw_custom_runtime",
            )
            self.assertEqual(
                approve.kwargs["env"]["OPENCLAW_RUNTIME_TARGET"],
                "openclaw_custom_runtime",
            )
            self.assertEqual(
                approve.kwargs["env"]["OPENCLAW_EXECUTION_REQUEST_ID"],
                "devices-1",
            )
            self.assertTrue(approve.kwargs["start_new_session"])

    def test_retention_actions_run_in_separate_process_groups(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))
            instance = {
                "legacy_user_id": "alice",
                "runtime_identifier": "openclaw_alice",
            }
            process = Mock()
            process.communicate.return_value = ("ok", None)
            process.returncode = 0
            process.poll.return_value = 0
            with patch("subprocess.Popen", return_value=process) as popen:
                adapter.delete(instance)
                deleted = popen.call_args
                adapter.restore(instance)
                restored = popen.call_args

            self.assertTrue(deleted.kwargs["start_new_session"])
            self.assertEqual(deleted.args[0][-1], "alice")
            self.assertTrue(restored.kwargs["start_new_session"])
            self.assertEqual(restored.args[0][-1], "alice")

    def test_runtime_methods_reject_user_id_strings(self):
        with TemporaryDirectory() as temp_dir:
            adapter = self.make_adapter(Path(temp_dir))

            with self.assertRaises(TypeError):
                adapter.get_runtime_target("alice")

    def test_product_capabilities_fail_closed(self):
        self.assertTrue(product_supports("openclaw", "restart"))
        self.assertTrue(product_supports("hermes", "restart"))
        self.assertTrue(product_supports("hermes", "batch_set_model_provider"))
        self.assertTrue(product_supports("evoscientist", "batch_set_model_provider"))
        self.assertTrue(product_supports("hermes", "delete"))
        self.assertTrue(product_supports("hermes", "restore"))
        self.assertTrue(product_supports("hermes", "update_version"))
        self.assertFalse(product_supports("evoscientist", "file_upload"))
        self.assertFalse(product_supports("unknown", "restart"))
        self.assertEqual(
            execution_action_capability("instance.wechat_bind"),
            "device_pairing",
        )
        self.assertEqual(
            execution_action_capability("instance.set_basic_auth"),
            "basic_auth",
        )
        self.assertEqual(
            execution_action_capability("instance.update_version"),
            "update_version",
        )
        self.assertEqual(
            execution_action_capability("instance.refresh_devices"),
            "device_pairing",
        )
        self.assertIsNone(execution_action_capability("shell.run"))


if __name__ == "__main__":
    unittest.main()
