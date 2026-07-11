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

        self.assertIn("upstream openclaw_backend_${PORT} {", script)
        self.assertIn("zone openclaw_backend_${PORT} 64k;", script)
        self.assertIn("resolver 127.0.0.11 valid=10s ipv6=off;", script)
        self.assertIn("server openclaw_${USER_ID}:18789 resolve;", script)
        self.assertIn("proxy_pass http://openclaw_backend_${PORT};", script)
        self.assertIn("server openclaw-manager-web:8080 resolve;", script)
        self.assertIn("proxy_pass http://manager_web_backend_${PORT}/instance-admin/;", script)
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
                self.assertIn(f"server openclaw_{user_id}:18789 resolve;", text)
                self.assertIn(f"proxy_pass http://agent_{user_id}_1;", text)
                self.assertIn(f"proxy_pass http://agent_{user_id}_1;\n}}", text)
            backups = list((conf_dir / ".dynamic-upstream-backups").glob("*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "active" / "alice.conf").is_file())
            docker_log = (root / "docker.log").read_text(encoding="utf-8")
            self.assertIn("exec openclaw-nginx nginx -t", docker_log)
            self.assertIn("exec openclaw-nginx nginx -s reload", docker_log)

    def test_migrates_active_and_disabled_user_configs(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            conf_dir = root / "nginx" / "conf"
            disabled_dir = conf_dir / "_disabled"
            legacy_disabled_dir = Path(f"{conf_dir}.disabled")
            disabled_dir.mkdir(parents=True)
            legacy_disabled_dir.mkdir(parents=True)
            self.write_config(manager, conf_dir)
            self.write_fake_docker(root)

            configs = {
                conf_dir / "active.conf": "172.20.0.7",
                disabled_dir / "stopped.conf": "172.20.0.8",
                legacy_disabled_dir / "legacy.conf": "172.20.0.9",
            }
            for path, upstream in configs.items():
                path.write_text(
                    f"location / {{\n    proxy_pass http://{upstream}:18789;\n}}\n",
                    encoding="utf-8",
                )

            result = self.run_migration(manager, root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for path in configs:
                user_id = path.stem
                text = path.read_text(encoding="utf-8")
                self.assertIn(f"server openclaw_{user_id}:18789 resolve;", text)
                self.assertIn(f"proxy_pass http://agent_{user_id}_1;", text)

            backup_dir = next((conf_dir / ".dynamic-upstream-backups").iterdir())
            self.assertTrue((backup_dir / "active" / "active.conf").is_file())
            self.assertTrue((backup_dir / "disabled" / "stopped.conf").is_file())
            self.assertTrue((backup_dir / "legacy-disabled" / "legacy.conf").is_file())

    def test_named_user_migration_finds_disabled_config(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            conf_dir = root / "nginx" / "conf"
            disabled_dir = conf_dir / "_disabled"
            disabled_dir.mkdir(parents=True)
            self.write_config(manager, conf_dir)
            self.write_fake_docker(root)
            config = disabled_dir / "stopped.conf"
            config.write_text(
                "location / {\n    proxy_pass http://172.20.0.8:18789;\n}\n",
                encoding="utf-8",
            )

            result = self.run_migration(manager, root, "stopped")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = config.read_text(encoding="utf-8")
            self.assertIn("server openclaw_stopped:18789 resolve;", text)
            self.assertIn("proxy_pass http://agent_stopped_1;", text)
            repeated = self.run_migration(manager, root, "stopped")
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertIn("already use Docker DNS", repeated.stdout)


    def test_bulk_migration_skips_manager_web_config(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            conf_dir = root / "nginx" / "conf"
            conf_dir.mkdir(parents=True)
            self.write_config(manager, conf_dir)
            self.write_fake_docker(root)
            manager_config = conf_dir / "manager-web.conf"
            manager_text = (
                "location / {\n"
                "    proxy_pass http://openclaw-manager-web:8080;\n"
                "}\n"
            )
            manager_config.write_text(manager_text, encoding="utf-8")
            user_config = conf_dir / "alice.conf"
            user_config.write_text(
                "location / {\n    proxy_pass http://172.20.0.7:18789;\n}\n",
                encoding="utf-8",
            )

            result = self.run_migration(manager, root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(manager_config.read_text(encoding="utf-8"), manager_text)
            migrated_user = user_config.read_text(encoding="utf-8")
            self.assertIn("server openclaw_alice:18789 resolve;", migrated_user)
            self.assertIn("proxy_pass http://agent_alice_1;", migrated_user)
            self.assertIn("Migrated 1 Nginx user config(s)", result.stdout)

    def test_bulk_migration_converts_evoscientist_multi_port_config(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            conf_dir = root / "nginx" / "conf"
            conf_dir.mkdir(parents=True)
            self.write_config(manager, conf_dir)
            self.write_fake_docker(root)
            evoscientist_config = conf_dir / "evosci-test001.conf"
            evoscientist_text = textwrap.dedent(
                """
                server {
                    location = /api/workspace/upload { proxy_pass http://evoscientist_evosci-test001:4716; }
                    location /api/ { proxy_pass http://evoscientist_evosci-test001:6175/; }
                    location / {
                        proxy_pass http://evoscientist_evosci-test001:4716;
                    }
                }
                """
            ).lstrip()
            evoscientist_config.write_text(evoscientist_text, encoding="utf-8")
            user_config = conf_dir / "alice.conf"
            user_config.write_text(
                "location / {\n    proxy_pass http://172.20.0.7:18789;\n}\n",
                encoding="utf-8",
            )

            result = self.run_migration(manager, root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            migrated = evoscientist_config.read_text(encoding="utf-8")
            self.assertIn(
                "server evoscientist_evosci-test001:4716 resolve;",
                migrated,
            )
            self.assertIn(
                "server evoscientist_evosci-test001:6175 resolve;",
                migrated,
            )
            self.assertEqual(migrated.count("evoscientist_evosci-test001:4716 resolve;"), 1)
            self.assertIn("proxy_pass http://agent_evosci_test001_1;", migrated)
            self.assertIn("proxy_pass http://agent_evosci_test001_2/;", migrated)
            self.assertIn("Migrated 2 Nginx user config(s)", result.stdout)

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

    def test_consistency_check_accepts_resolved_upstream_block(self):
        with TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "alice.conf"
            config.write_text(
                textwrap.dedent(
                    """
                    upstream agent_alice_1 {
                        zone agent_alice_1 64k;
                        resolver 127.0.0.11 valid=10s ipv6=off;
                        resolver_timeout 5s;
                        server openclaw_alice:18789 resolve;
                    }

                    server {
                        listen 30123 ssl;
                        location / {
                            proxy_pass http://agent_alice_1;
                        }
                    }
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            detected = detect_nginx_conf(config)

            self.assertTrue(detected["dynamic_upstream"])
            self.assertEqual(detected["proxy_user"], "alice")
            self.assertEqual(detected["root_proxy"], "openclaw_alice")


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
                "location / {\n    proxy_pass http://172.20.0.8:4716;\n}\n",
                encoding="utf-8",
            )

            result = self.run_migration(manager, root, "alice", "bob")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(alice.read_text(encoding="utf-8"), original)
            self.assertFalse((root / "docker.log").exists())

if __name__ == "__main__":
    unittest.main()
