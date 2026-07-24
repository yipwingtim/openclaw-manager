#!/usr/bin/env python3

import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = ROOT_DIR / "scripts" / "update_manager_auth.sh"
MANAGER_TEMPLATE = ROOT_DIR / "templates" / "nginx" / "manager-web.conf.tpl"
NGINX_AUTH_LIBRARY = ROOT_DIR / "scripts" / "lib_nginx_auth.sh"
CREATE_USER_SCRIPT = ROOT_DIR / "scripts" / "create_user.sh"
ENABLE_INSTANCE_ADMIN_SCRIPT = ROOT_DIR / "scripts" / "enable_instance_admin.sh"
DEPLOY_SERVICES_SCRIPT = ROOT_DIR / "scripts" / "deploy_services.sh"


class ManagerAuthNginxTests(unittest.TestCase):
    def test_entry_generation_paths_use_shared_provider_guard(self):
        create_user = CREATE_USER_SCRIPT.read_text(encoding="utf-8")
        enable_admin = ENABLE_INSTANCE_ADMIN_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("render_instance_admin_provider_guard", create_user)
        self.assertIn("$NGINX_ADMIN_PROVIDER_GUARD", create_user)
        self.assertIn("render_instance_admin_provider_guard", enable_admin)
        self.assertIn("{provider_guard}", enable_admin)
        self.assertIn("Nginx root location marker not found", enable_admin)

    def test_deploy_updates_nginx_before_restarting_manager_web(self):
        script = DEPLOY_SERVICES_SCRIPT.read_text(encoding="utf-8")

        self.assertLess(
            script.index('bash "$SCRIPT_DIR/update_manager_auth.sh"'),
            script.index("docker compose up -d --no-build"),
        )
        self.assertIn("actual_provider=", script)
        self.assertIn('actual_provider="${actual_provider:-nginx-basic}"', script)

    def test_new_instance_guard_uses_configured_public_host(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; render_instance_admin_provider_guard local manager.example.test',
                "bash",
                str(NGINX_AUTH_LIBRARY),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "        return 302 https://manager.example.test:30015/; # managed-by-openclaw-manager-auth\n",
        )

    def test_new_instance_guard_rejects_unknown_provider_without_protocol(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; render_instance_admin_provider_guard typo manager.example.test',
                "bash",
                str(NGINX_AUTH_LIBRARY),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)

    def make_runtime(self, root, provider="local", fail_nginx_test=False):
        manager = root / "manager"
        conf_dir = root / "nginx" / "conf"
        public_dir = root / "public"
        (manager / "scripts").mkdir(parents=True)
        (manager / "templates" / "nginx").mkdir(parents=True)
        (manager / "config").mkdir(parents=True)
        conf_dir.mkdir(parents=True)
        public_dir.mkdir()
        shutil.copy2(UPDATE_SCRIPT, manager / "scripts" / UPDATE_SCRIPT.name)
        shutil.copy2(NGINX_AUTH_LIBRARY, manager / "scripts" / NGINX_AUTH_LIBRARY.name)
        shutil.copy2(MANAGER_TEMPLATE, manager / "templates" / "nginx" / MANAGER_TEMPLATE.name)
        (manager / "config" / "openclaw-manager.env").write_text(
            textwrap.dedent(
                f"""
                MANAGER_AUTH_PROVIDER={provider}
                PUBLIC_HOST=manager.example.test
                OPENCLAW_PUBLIC_DIR={public_dir}
                NGINX_USERS_CONF_DIR={conf_dir}
                NGINX_CONTAINER_NAME=openclaw-nginx
                NGINX_HTPASSWD_FILE_IN_CONTAINER=/etc/nginx/auth/.htpasswd
                NGINX_SSL_CERT=/etc/nginx/certs/openclaw.crt
                NGINX_SSL_KEY=/etc/nginx/certs/openclaw.key
                OPENCLAW_INTERNAL_TOKEN=test-token
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            textwrap.dedent(
                f"""
                #!/bin/sh
                if [ "${{1:-}} ${{2:-}} ${{3:-}} ${{4:-}}" = "exec openclaw-nginx nginx -t" ]; then
                  exit {1 if fail_nginx_test else 0}
                fi
                exit 0
                """
            ).lstrip(),
            encoding="utf-8",
        )
        docker.chmod(0o755)
        return manager, conf_dir, bin_dir

    def run_update(self, manager, bin_dir):
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        return subprocess.run(
            ["bash", str(manager / "scripts" / "update_manager_auth.sh")],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def write_instance_config(self, path):
        path.write_text(
            textwrap.dedent(
                """
                server {
                    location /admin/ {
                        auth_basic "OpenClaw Login";
                        proxy_pass http://manager/instance-admin/;
                        proxy_set_header X-OpenClaw-User "alice";
                    }
                }
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def test_provider_switch_toggles_legacy_instance_admin_redirect(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager, conf_dir, bin_dir = self.make_runtime(root)
            instance_config = conf_dir / "alice.conf"
            self.write_instance_config(instance_config)
            deleted_config = root / "public" / "deleted" / "bob_20260723" / "nginx" / "bob.conf"
            deleted_config.parent.mkdir(parents=True)
            self.write_instance_config(deleted_config)

            local_result = self.run_update(manager, bin_dir)

            self.assertEqual(local_result.returncode, 0, local_result.stderr)
            self.assertIn(
                "return 302 https://manager.example.test:30015/; # managed-by-openclaw-manager-auth",
                instance_config.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "managed-by-openclaw-manager-auth",
                deleted_config.read_text(encoding="utf-8"),
            )
            config_file = manager / "config" / "openclaw-manager.env"
            config_file.write_text(
                config_file.read_text(encoding="utf-8").replace(
                    "MANAGER_AUTH_PROVIDER=local",
                    "MANAGER_AUTH_PROVIDER=nginx-basic",
                ),
                encoding="utf-8",
            )

            basic_result = self.run_update(manager, bin_dir)

            self.assertEqual(basic_result.returncode, 0, basic_result.stderr)
            self.assertNotIn(
                "managed-by-openclaw-manager-auth",
                instance_config.read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "managed-by-openclaw-manager-auth",
                deleted_config.read_text(encoding="utf-8"),
            )

    def test_external_provider_exposes_only_basic_auth_emergency_path(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager, conf_dir, bin_dir = self.make_runtime(root, provider="campus-uis")
            config_file = manager / "config" / "openclaw-manager.env"
            config_file.write_text(
                config_file.read_text(encoding="utf-8") + "MANAGER_AUTH_TYPE=oidc\n",
                encoding="utf-8",
            )

            result = self.run_update(manager, bin_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (conf_dir / "manager-web.conf").read_text(encoding="utf-8")
            self.assertIn("location = /emergency/login", config)
            self.assertIn('auth_basic "OpenClaw Manager Emergency";', config)
            self.assertIn('X-OpenClaw-Internal-Token "test-token";', config)
            root_location = config.split("    location / {", 1)[1]
            self.assertNotIn("auth_basic_user_file", root_location)

    def test_nginx_validation_failure_restores_instance_config(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager, conf_dir, bin_dir = self.make_runtime(root, fail_nginx_test=True)
            instance_config = conf_dir / "alice.conf"
            self.write_instance_config(instance_config)
            deleted_config = root / "public" / "deleted" / "alice_20260723" / "nginx" / "alice.conf"
            deleted_config.parent.mkdir(parents=True)
            self.write_instance_config(deleted_config)
            original = instance_config.read_text(encoding="utf-8")
            deleted_original = deleted_config.read_text(encoding="utf-8")

            result = self.run_update(manager, bin_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(instance_config.read_text(encoding="utf-8"), original)
            self.assertEqual(deleted_config.read_text(encoding="utf-8"), deleted_original)

    def test_enable_instance_admin_failure_restores_original_config(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "manager"
            scripts = manager / "scripts"
            conf_dir = root / "nginx" / "conf"
            scripts.mkdir(parents=True)
            conf_dir.mkdir(parents=True)
            shutil.copy2(ENABLE_INSTANCE_ADMIN_SCRIPT, scripts / ENABLE_INSTANCE_ADMIN_SCRIPT.name)
            shutil.copy2(NGINX_AUTH_LIBRARY, scripts / NGINX_AUTH_LIBRARY.name)
            (scripts / "update_manager_auth.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (scripts / "update_manager_auth.sh").chmod(0o755)
            htpasswd = root / "auth" / ".htpasswd"
            user_htpasswd = root / "auth" / "users" / "alice" / ".htpasswd"
            user_htpasswd.parent.mkdir(parents=True)
            user_htpasswd.write_text("alice:test\n", encoding="utf-8")
            (manager / "config").mkdir()
            (manager / "config" / "openclaw-manager.env").write_text(
                textwrap.dedent(
                    f"""
                    MANAGER_AUTH_PROVIDER=local
                    PUBLIC_HOST=manager.example.test
                    NGINX_USERS_CONF_DIR={conf_dir}
                    NGINX_HTPASSWD_FILE={htpasswd}
                    NGINX_HTPASSWD_FILE_IN_CONTAINER=/etc/nginx/auth/.htpasswd
                    NGINX_CONTAINER_NAME=openclaw-nginx
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            instance_config = conf_dir / "alice.conf"
            instance_config.write_text("server {\n    location / {\n    }\n}\n", encoding="utf-8")
            original = instance_config.read_text(encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            docker.chmod(0o755)

            result = subprocess.run(
                ["bash", str(scripts / "enable_instance_admin.sh"), "alice"],
                text=True,
                capture_output=True,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Enabled instance admin: alice", result.stdout)
            self.assertIn("previous configs restored", result.stderr)
            self.assertEqual(instance_config.read_text(encoding="utf-8"), original)

    def test_explicit_restore_reinstates_previous_configs(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager, conf_dir, bin_dir = self.make_runtime(root)
            manager_config = conf_dir / "manager-web.conf"
            manager_config.write_text("original manager config\n", encoding="utf-8")
            instance_config = conf_dir / "alice.conf"
            self.write_instance_config(instance_config)
            original = instance_config.read_text(encoding="utf-8")

            update = self.run_update(manager, bin_dir)
            backup_dir = next(
                line.removeprefix("[INFO] Backup: ")
                for line in update.stdout.splitlines()
                if line.startswith("[INFO] Backup: ")
            )
            config_file = manager / "config" / "openclaw-manager.env"
            config_file.write_text(
                config_file.read_text(encoding="utf-8")
                .replace("MANAGER_AUTH_PROVIDER=local", "MANAGER_AUTH_PROVIDER=invalid")
                .replace("PUBLIC_HOST=manager.example.test\n", ""),
                encoding="utf-8",
            )
            restore = subprocess.run(
                [
                    "bash",
                    str(manager / "scripts" / "update_manager_auth.sh"),
                    "--restore",
                    backup_dir,
                ],
                text=True,
                capture_output=True,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                check=False,
            )

            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(manager_config.read_text(encoding="utf-8"), "original manager config\n")
            self.assertEqual(instance_config.read_text(encoding="utf-8"), original)

    def test_failed_explicit_restore_keeps_current_configs(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager, conf_dir, bin_dir = self.make_runtime(root)
            manager_config = conf_dir / "manager-web.conf"
            manager_config.write_text("original manager config\n", encoding="utf-8")
            instance_config = conf_dir / "alice.conf"
            self.write_instance_config(instance_config)

            update = self.run_update(manager, bin_dir)
            self.assertEqual(update.returncode, 0, update.stderr)
            current_manager = manager_config.read_text(encoding="utf-8")
            current_instance = instance_config.read_text(encoding="utf-8")
            backup_dir = next(
                line.removeprefix("[INFO] Backup: ")
                for line in update.stdout.splitlines()
                if line.startswith("[INFO] Backup: ")
            )
            docker = bin_dir / "docker"
            docker.write_text(
                docker.read_text(encoding="utf-8").replace(
                    "  exit 0\nfi",
                    "  exit 1\nfi",
                ),
                encoding="utf-8",
            )

            restore = subprocess.run(
                ["bash", str(manager / "scripts" / "update_manager_auth.sh"), "--restore", backup_dir],
                text=True,
                capture_output=True,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                check=False,
            )

            self.assertNotEqual(restore.returncode, 0)
            self.assertEqual(manager_config.read_text(encoding="utf-8"), current_manager)
            self.assertEqual(instance_config.read_text(encoding="utf-8"), current_instance)


if __name__ == "__main__":
    unittest.main()
