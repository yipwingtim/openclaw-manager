import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "scripts" / "approve_device.sh"


class ApproveDeviceScriptTests(unittest.TestCase):
    def make_fixture(self, root, docker_body):
        manager = root / "manager"
        scripts = manager / "scripts"
        config = manager / "config"
        user_dir = root / "public" / "users" / "alice"
        bin_dir = root / "bin"
        scripts.mkdir(parents=True)
        config.mkdir()
        user_dir.mkdir(parents=True)
        bin_dir.mkdir()
        shutil.copy2(SCRIPT, scripts / "approve_device.sh")
        (config / "openclaw-manager.env").write_text(
            f"OPENCLAW_PUBLIC_DIR={root / 'public'}\n",
            encoding="utf-8",
        )
        docker = bin_dir / "docker"
        docker.write_text("#!/bin/sh\n" + docker_body, encoding="utf-8")
        docker.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["DOCKER_LOG"] = str(root / "docker.log")
        env["OPENCLAW_RUNTIME_TARGET"] = "openclaw.custom-runtime"
        return scripts / "approve_device.sh", user_dir, env

    def run_script(self, script, env):
        return subprocess.run(
            ["bash", str(script), "alice", "--list-only"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_list_failure_preserves_existing_cache(self):
        with TemporaryDirectory() as temp_dir:
            script, user_dir, env = self.make_fixture(
                Path(temp_dir),
                'echo "$*" >> "$DOCKER_LOG"\n'
                'if [ "$1" = "ps" ]; then echo openclaw.custom-runtime; exit 0; fi\n'
                'echo list-failed >&2\nexit 1\n',
            )
            cache = user_dir / "devices.txt"
            cache.write_text("old cache", encoding="utf-8")

            result = self.run_script(script, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(cache.read_text(encoding="utf-8"), "old cache")
            self.assertIn("Could not list devices", result.stderr)

    def test_runtime_target_is_used_and_cache_is_replaced(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script, user_dir, env = self.make_fixture(
                root,
                'echo "$*" >> "$DOCKER_LOG"\n'
                'if [ "$1" = "ps" ]; then echo openclaw.custom-runtime; exit 0; fi\n'
                'echo "Paired (0)"\n',
            )

            result = self.run_script(script, env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(
                "exec openclaw.custom-runtime timeout 45s openclaw devices list",
                (root / "docker.log").read_text(encoding="utf-8"),
            )
            self.assertIn("Paired (0)", (user_dir / "devices.txt").read_text())

    def test_instance_data_path_is_used_for_device_cache(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script, legacy_dir, env = self.make_fixture(
                root,
                'if [ "$1" = "ps" ]; then echo openclaw.custom-runtime; exit 0; fi\n'
                'echo "Paired (0)"\n',
            )
            instance_dir = root / "public" / "instances" / "openclaw" / "instance-1"
            instance_dir.mkdir(parents=True)
            env["OPENCLAW_DATA_PATH"] = str(instance_dir)

            result = self.run_script(script, env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((instance_dir / "devices.txt").is_file())

    def test_refresh_cache_fails_when_metadata_query_fails(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "manager"
            scripts = manager / "scripts"
            config = manager / "config"
            scripts.mkdir(parents=True)
            config.mkdir()
            refresh = ROOT_DIR / "scripts" / "refresh_device_cache.sh"
            shutil.copy2(refresh, scripts / refresh.name)
            public = root / "public"
            public.mkdir()
            (config / "openclaw-manager.env").write_text(
                f"OPENCLAW_PUBLIC_DIR={public}\nMETADATA_DB_FILE={public / 'manager.db'}\n",
                encoding="utf-8",
            )
            (public / "manager.db").write_text("not sqlite", encoding="utf-8")
            result = subprocess.run(["bash", str(scripts / refresh.name)], text=True, capture_output=True, env={**os.environ, "PATH": os.environ["PATH"]}, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not read OpenClaw instances", result.stdout)

    def test_rejects_data_path_outside_public_directory(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script, _, env = self.make_fixture(root, 'exit 0\n')
            env["OPENCLAW_DATA_PATH"] = str(root / "outside")
            result = self.run_script(script, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid instance data path", result.stderr)

    def test_refresh_cache_script_reads_instance_metadata_paths(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "manager"
            scripts = manager / "scripts"
            config = manager / "config"
            scripts.mkdir(parents=True)
            config.mkdir()
            refresh = ROOT_DIR / "scripts" / "refresh_device_cache.sh"
            shutil.copy2(refresh, scripts / refresh.name)
            shutil.copy2(SCRIPT, scripts / SCRIPT.name)
            public = root / "public"
            public.mkdir()
            instance_dir = public / "instances" / "openclaw" / "instance-1"
            instance_dir.mkdir(parents=True)
            (config / "openclaw-manager.env").write_text(
                f"OPENCLAW_PUBLIC_DIR={public}\nMETADATA_DB_FILE={public / 'manager.db'}\n",
                encoding="utf-8",
            )
            import sqlite3
            with sqlite3.connect(public / "manager.db") as conn:
                conn.execute("CREATE TABLE instances (legacy_user_id TEXT, data_path TEXT, runtime_identifier TEXT, product TEXT, status TEXT)")
                conn.execute("INSERT INTO instances VALUES ('alice', ?, 'openclaw.custom-runtime', 'openclaw', 'active')", (str(instance_dir),))
                conn.commit()
            docker = root / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = ps ]; then echo openclaw.custom-runtime; exit 0; fi\n"
                "echo 'Paired (0)'\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{root}:{env['PATH']}"
            result = subprocess.run(["bash", str(scripts / refresh.name)], text=True, capture_output=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((instance_dir / "devices.txt").is_file())

    def test_executor_latest_approval_refuses_second_attempt(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script, user_dir, env = self.make_fixture(
                root,
                'echo "$*" >> "$DOCKER_LOG"\n'
                'if [ "$1" = "ps" ]; then echo openclaw.custom-runtime; exit 0; fi\n'
                'if echo "$*" | grep -q "devices list"; then echo "Pending (1) requestId: req-1"; exit 0; fi\n'
                'if echo "$*" | grep -q "devices approve --latest"; then echo "Selected pending device request req-1"; exit 1; fi\n'
                'echo "Approved"\n',
            )
            env["OPENCLAW_EXECUTION_REQUEST_ID"] = "approval-1"
            first = subprocess.run(
                ["bash", str(script), "alice", "--latest"],
                text=True, capture_output=True, env=env, check=False,
            )
            second = subprocess.run(
                ["bash", str(script), "alice", "--latest"],
                text=True, capture_output=True, env=env, check=False,
            )

            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already attempted", second.stderr)
            state_dir = user_dir / "backups" / "device-approvals"
            self.assertEqual(len(list(state_dir.iterdir())), 1)

    def test_latest_approval_runs_once(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script, _, env = self.make_fixture(
                root,
                'echo "$*" >> "$DOCKER_LOG"\n'
                'if [ "$1" = "ps" ]; then echo openclaw.custom-runtime; exit 0; fi\n'
                'if echo "$*" | grep -q "devices list"; then echo "Pending (1) requestId: req-1"; exit 0; fi\n'
                'if echo "$*" | grep -q "devices approve --latest"; then echo "Selected pending device request req-1"; exit 1; fi\n'
                'echo "Approved"\n',
            )

            result = subprocess.run(
                ["bash", str(script), "alice", "--latest"],
                text=True, capture_output=True, env=env, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            approvals = [
                line for line in (root / "docker.log").read_text().splitlines()
                if "devices approve" in line
            ]
            self.assertEqual(len(approvals), 2)
            self.assertIn("devices approve --latest", approvals[0])
            self.assertIn("devices approve req-1", approvals[1])


if __name__ == "__main__":
    unittest.main()
