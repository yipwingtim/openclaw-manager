#!/usr/bin/env python3

import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
BATCH_CREATE_SCRIPT = ROOT_DIR / "scripts" / "batch_create_users.sh"


class BatchCreateUsersScriptTests(unittest.TestCase):
    def make_manager(self, root):
        manager = root / "manager"
        scripts = manager / "scripts"
        config = manager / "config"
        scripts.mkdir(parents=True)
        config.mkdir(parents=True)
        shutil.copy2(BATCH_CREATE_SCRIPT, scripts / "batch_create_users.sh")
        (scripts / "lib_nginx_auth.sh").write_text(
            """
normalize_basic_auth_enabled() {
  case "${1:-true}" in
    true|false) printf '%s' "$1" ;;
    "") printf 'true' ;;
    *) return 1 ;;
  esac
}
""".lstrip(),
            encoding="utf-8",
        )
        (scripts / "create_user.sh").write_text(
            """
#!/bin/sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1090
. "$SCRIPT_DIR/../config/openclaw-manager.env"
user_id="$1"
mkdir -p "$OPENCLAW_PUBLIC_DIR/users/$user_id/config"
cat > "$OPENCLAW_PUBLIC_DIR/users/$user_id/config/openclaw.json" <<'JSON'
{"gateway":{"auth":{"token":"test-token"}}}
JSON
cat > "$NGINX_USERS_CONF_DIR/$user_id.conf" <<EOF
server {
  listen 41001 ssl;
}
EOF
""".lstrip(),
            encoding="utf-8",
        )
        (scripts / "create_user.sh").chmod(0o755)
        return manager

    def write_fake_commands(self, root):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in ("bash", "sh", "python3", "head", "tr", "dirname", "xargs", "grep", "sed", "mkdir", "cat", "echo", "sleep"):
            target = bin_dir / name
            target.symlink_to(shutil.which(name))
        docker = bin_dir / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        docker.chmod(0o755)
        return bin_dir

    def test_batch_create_runs_without_sudo_on_path(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            bin_dir = self.write_fake_commands(root)
            public_dir = root / "public"
            nginx_conf_dir = root / "nginx-conf"
            nginx_compose_dir = root / "nginx-compose"
            public_dir.mkdir()
            nginx_conf_dir.mkdir()
            nginx_compose_dir.mkdir()
            (manager / "config" / "openclaw-manager.env").write_text(
                textwrap.dedent(
                    f"""
                    OPENCLAW_PUBLIC_DIR={public_dir}
                    PUBLIC_HOST=example.test
                    NGINX_COMPOSE_DIR={nginx_compose_dir}
                    NGINX_USERS_CONF_DIR={nginx_conf_dir}
                    NGINX_CONTAINER_NAME=openclaw-nginx
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            input_csv = root / "input.csv"
            output_csv = root / "results.csv"
            input_csv.write_text("user_id,basic_auth_password,basic_auth_enabled\nalice,,true\n", encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = str(bin_dir)
            result = subprocess.run(
                ["bash", str(manager / "scripts" / "batch_create_users.sh"), str(input_csv), str(output_csv)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("alice", output_csv.read_text(encoding="utf-8"))
            self.assertIn("created", output_csv.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
