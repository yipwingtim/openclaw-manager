#!/usr/bin/env python3

import os
import runpy
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
CREATE_USER_SCRIPT = ROOT_DIR / "scripts" / "create_user.sh"
MIGRATE_SCRIPT = ROOT_DIR / "scripts" / "migrate_nginx_upstreams.sh"
CHECKER = runpy.run_path(str(ROOT_DIR / "scripts" / "check_metadata_consistency.py"))
detect_nginx_conf = CHECKER["detect_nginx_conf"]


class NginxDynamicUpstreamTests(unittest.TestCase):
    def make_manager(self, root):
        manager = root / "manager"
        (manager / "scripts").mkdir(parents=True)
        (manager / "config").mkdir(parents=True)
        shutil.copy2(MIGRATE_SCRIPT, manager / "scripts" / "migrate_nginx_upstreams.sh")
        return manager

    def write_config(self, manager, nginx_conf_dir):
        (manager / "config" / "openclaw-manager.env").write_text(
            textwrap.dedent(
                f"""
                NGINX_USERS_CONF_DIR={nginx_conf_dir}
                NGINX_CONTAINER_NAME=openclaw-nginx
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def write_fake_docker(self, root, fail_test=False):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            textwrap.dedent(
                f"""
                #!/bin/sh
                printf '%s\n' "$*" >> "$DOCKER_LOG"
                if [ "${{1:-}} ${{2:-}} ${{3:-}}" = "exec openclaw-nginx nginx" ] && [ "${{4:-}}" = "-t" ]; then
                  exit {1 if fail_test else 0}
                fi
                exit 0
                """
            ).lstrip(),
            encoding="utf-8",
        )
        docker.chmod(0o755)
        return bin_dir

    def run_migration(self, manager, root, *user_ids):
        env = os.environ.copy()
        env["PATH"] = f"{root / 'bin'}:{env['PATH']}"
        env["DOCKER_LOG"] = str(root / "docker.log")
        return subprocess.run(
            ["bash", str(manager / "scripts" / "migrate_nginx_upstreams.sh"), *user_ids],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_create_user_renders_runtime_resolved_container_upstream(self):
        script = CREATE_USER_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("resolver 127.0.0.11 valid=10s ipv6=off;", script)
        self.assertIn('set \\$openclaw_upstream "openclaw_${USER_ID}:18789";', script)
        self.assertIn("proxy_pass http://\\$openclaw_upstream;", script)
        self.assertNotIn("proxy_pass http://openclaw_${USER_ID}:18789;", script)

    def test_migrates_ip_and_static_container_upstreams_then_reloads(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            conf_dir = root / "nginx" / "conf"
            conf_dir.mkdir(parents=True)
            self.write_config(manager, conf_dir)
            self.write_fake_docker(root)
            (conf_dir / "alice.conf").write_text(
                "location / {\n    proxy_pass http://172.20.0.7:18789;\n}\n",
                encoding="utf-8",
            )
            (conf_dir / "bob.conf").write_text(
                "location / {\n    proxy_pass http://openclaw_bob:18789;\n}\n",
                encoding="utf-8",
            )

            result = self.run_migration(manager, root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for user_id in ("alice", "bob"):
                text = (conf_dir / f"{user_id}.conf").read_text(encoding="utf-8")
                self.assertIn("resolver 127.0.0.11 valid=10s ipv6=off;", text)
                self.assertIn(f'set $openclaw_upstream "openclaw_{user_id}:18789";', text)
                self.assertIn("proxy_pass http://$openclaw_upstream;", text)
                self.assertIn("proxy_pass http://$openclaw_upstream;\n}", text)
            backups = list((conf_dir / ".dynamic-upstream-backups").glob("*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "alice.conf").is_file())
            docker_log = (root / "docker.log").read_text(encoding="utf-8")
            self.assertIn("exec openclaw-nginx nginx -t", docker_log)
            self.assertIn("exec openclaw-nginx nginx -s reload", docker_log)

    def test_restores_original_configs_when_nginx_test_fails(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            conf_dir = root / "nginx" / "conf"
            conf_dir.mkdir(parents=True)
            self.write_config(manager, conf_dir)
            self.write_fake_docker(root, fail_test=True)
            original = "location / {\n    proxy_pass http://172.20.0.7:18789;\n}\n"
            config = conf_dir / "alice.conf"
            config.write_text(original, encoding="utf-8")

            result = self.run_migration(manager, root)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertIn("restored original configs", (result.stdout + result.stderr).lower())

    def test_consistency_check_requires_dynamic_directives_in_root_location(self):
        with TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "alice.conf"
            config.write_text(
                textwrap.dedent(
                    """
                    server {
                        location /admin/ {
                            resolver 127.0.0.11 valid=10s ipv6=off;
                            set $openclaw_upstream "openclaw_alice:18789";
                        }

                        location / {
                            proxy_pass http://$openclaw_upstream;
                        }
                    }
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            detected = detect_nginx_conf(config)

            self.assertFalse(detected["dynamic_upstream"])

    def test_preflight_failure_does_not_partially_modify_configs(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            conf_dir = root / "nginx" / "conf"
            conf_dir.mkdir(parents=True)
            self.write_config(manager, conf_dir)
            self.write_fake_docker(root)
            original = "location / {\n    proxy_pass http://172.20.0.7:18789;\n}\n"
            alice = conf_dir / "alice.conf"
            alice.write_text(original, encoding="utf-8")
            (conf_dir / "bob.conf").write_text(
                "location / {\n    proxy_pass http://unsupported:18789;\n}\n",
                encoding="utf-8",
            )

            result = self.run_migration(manager, root, "alice", "bob")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(alice.read_text(encoding="utf-8"), original)
            self.assertFalse((root / "docker.log").exists())

if __name__ == "__main__":
    unittest.main()
