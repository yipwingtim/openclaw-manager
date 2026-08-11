import os
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "scripts" / "refresh_device_cache.sh"


class RefreshDeviceCacheScriptTests(unittest.TestCase):
    def test_uses_active_openclaw_instance_metadata(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "manager"
            scripts = manager / "scripts"
            config = manager / "config"
            public = root / "public"
            instance_dir = public / "instances" / "openclaw" / "instance-1"
            scripts.mkdir(parents=True)
            config.mkdir()
            instance_dir.mkdir(parents=True)
            shutil.copy2(SCRIPT, scripts / SCRIPT.name)
            (config / "openclaw-manager.env").write_text(
                f"OPENCLAW_PUBLIC_DIR={public}\n", encoding="utf-8"
            )
            (scripts / "approve_device.sh").write_text(
                '#!/bin/sh\nprintf "%s|%s|%s\\n" "$OPENCLAW_DATA_PATH" "$OPENCLAW_RUNTIME_TARGET" "$*" >> "$CALL_LOG"\n',
                encoding="utf-8",
            )
            (scripts / "approve_device.sh").chmod(0o755)
            with sqlite3.connect(public / "manager.db") as connection:
                connection.execute(
                    "CREATE TABLE instances (legacy_user_id TEXT, product TEXT, status TEXT, data_path TEXT, runtime_identifier TEXT)"
                )
                connection.execute(
                    "INSERT INTO instances VALUES (?, 'openclaw', 'active', ?, ?)",
                    ("alice", str(instance_dir), "openclaw_alice"),
                )
                connection.execute(
                    "INSERT INTO instances VALUES (?, 'hermes', 'active', ?, ?)",
                    ("bob", str(public / "instances" / "hermes" / "instance-2"), "hermes_bob"),
                )
            env = os.environ.copy()
            env["CALL_LOG"] = str(root / "calls.log")

            result = subprocess.run(
                ["bash", str(scripts / SCRIPT.name)], text=True, capture_output=True, env=env, check=False
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                (root / "calls.log").read_text(encoding="utf-8").strip(),
                f"{instance_dir}|openclaw_alice|alice --list-only",
            )

    def test_metadata_query_failure_stops_refresh(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "manager"
            scripts = manager / "scripts"
            config = manager / "config"
            public = root / "public"
            scripts.mkdir(parents=True)
            config.mkdir()
            public.mkdir()
            shutil.copy2(SCRIPT, scripts / SCRIPT.name)
            (config / "openclaw-manager.env").write_text(
                f"OPENCLAW_PUBLIC_DIR={public}\n", encoding="utf-8"
            )
            (scripts / "approve_device.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (scripts / "approve_device.sh").chmod(0o755)

            result = subprocess.run(
                ["bash", str(scripts / SCRIPT.name)], text=True, capture_output=True, env=os.environ.copy(), check=False
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("metadata", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main()
