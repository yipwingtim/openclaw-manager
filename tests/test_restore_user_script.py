#!/usr/bin/env python3

import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
RESTORE_SCRIPT = ROOT_DIR / "scripts" / "restore_user.sh"
MIGRATE_NGINX_UPSTREAMS_SCRIPT = ROOT_DIR / "scripts" / "migrate_nginx_upstreams.sh"
TENANT_NETWORK_HELPER = ROOT_DIR / "scripts" / "lib_tenant_network.sh"
LEGACY_USER_COMPOSE = """\
services:
  openclaw-alice:
    container_name: openclaw_alice
    networks:
      - agent-net
networks:
  agent-net:
    external: true
"""


class RestoreUserScriptTests(unittest.TestCase):
    def make_manager(self, root):
        manager = root / "manager"
        scripts = manager / "scripts"
        config = manager / "config"
        scripts.mkdir(parents=True)
        config.mkdir(parents=True)
        allocator = scripts / "tenant_network_allocator.py"
        allocator.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import sys\n"
            "with open(os.environ['DOCKER_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write('allocator|' + ' '.join(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        allocator.chmod(0o755)
        shutil.copy2(RESTORE_SCRIPT, scripts / "restore_user.sh")
        shutil.copy2(MIGRATE_NGINX_UPSTREAMS_SCRIPT, scripts / "migrate_nginx_upstreams.sh")
        shutil.copy2(TENANT_NETWORK_HELPER, scripts / "lib_tenant_network.sh")
        (scripts / "metadata_cli.py").write_text(
            """
import os
import sys
from pathlib import Path
log = os.environ.get('METADATA_LOG')
if log:
    Path(log).write_text(' '.join(sys.argv[1:]) + '\\n', encoding='utf-8')
""".lstrip(),
            encoding="utf-8",
        )
        return manager

    def write_fake_docker(self, root):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            """
#!/bin/sh
printf '%s|%s\\n' "$PWD" "$*" >> "$DOCKER_LOG"
exit 0
""".lstrip(),
            encoding="utf-8",
        )
        docker.chmod(0o755)
        return bin_dir

    def write_config(self, manager, public_dir, nginx_compose_file, nginx_compose_dir, nginx_conf_dir):
        (manager / "config" / "openclaw-manager.env").write_text(
            textwrap.dedent(
                f"""
                OPENCLAW_PUBLIC_DIR={public_dir}
                NGINX_COMPOSE_FILE={nginx_compose_file}
                NGINX_COMPOSE_DIR={nginx_compose_dir}
                NGINX_USERS_CONF_DIR={nginx_conf_dir}
                OPENCLAW_TENANT_SUBNET_POOL=10.250.0.0/24
                OPENCLAW_TENANT_SUBNET_PREFIX=28
                NGINX_CONTAINER_NAME=openclaw-nginx
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def run_restore(self, manager, user_id, root):
        env = os.environ.copy()
        env["PATH"] = f"{root / 'bin'}:{env['PATH']}"
        env["DOCKER_LOG"] = str(root / "docker.log")
        env["METADATA_LOG"] = str(root / "metadata.log")
        return subprocess.run(
            ["bash", str(manager / "scripts" / "restore_user.sh"), user_id],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_restore_current_recycle_layout_restores_user_nginx_port_and_status(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            self.write_fake_docker(root)

            public_dir = root / "public"
            users_dir = public_dir / "users"
            recycle_dir = public_dir / "deleted" / "alice_20260705_010203"
            nginx_conf_dir = root / "nginx" / "conf"
            nginx_compose_dir = root / "nginx" / "compose"
            nginx_compose_file = nginx_compose_dir / "docker-compose.yml"
            (recycle_dir / "user").mkdir(parents=True)
            (recycle_dir / "nginx").mkdir(parents=True)
            users_dir.mkdir(parents=True)
            nginx_conf_dir.mkdir(parents=True)
            nginx_compose_dir.mkdir(parents=True)

            (recycle_dir / "user" / "docker-compose.yml").write_text(LEGACY_USER_COMPOSE, encoding="utf-8")
            (recycle_dir / "nginx" / "alice.conf").write_text(
                "server {\n"
                "  listen 30123 ssl;\n"
                "  location / {\n"
                "    proxy_pass http://172.20.0.7:18789;\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (public_dir / "users.csv").write_text(
                "user_id,port,created_at,status\nalice,30123,2026-07-05,deleted\n",
                encoding="utf-8",
            )
            nginx_compose_file.write_text(
                "services:\n  nginx:\n    image: nginx\n    ports:\n      - \"30015:30015\"\n    volumes: []\n",
                encoding="utf-8",
            )
            self.write_config(manager, public_dir, nginx_compose_file, nginx_compose_dir, nginx_conf_dir)

            result = self.run_restore(manager, "alice", root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((users_dir / "alice" / "docker-compose.yml").is_file())
            self.assertTrue((nginx_conf_dir / "alice.conf").is_file())
            restored_nginx = (nginx_conf_dir / "alice.conf").read_text(encoding="utf-8")
            self.assertIn("server openclaw_alice:18789 resolve;", restored_nginx)
            self.assertIn("proxy_pass http://agent_alice_1;", restored_nginx)
            self.assertIn('      - "30123:30123"', nginx_compose_file.read_text(encoding="utf-8"))
            self.assertIn("alice,30123,2026-07-05,active", (public_dir / "users.csv").read_text(encoding="utf-8"))
            self.assertTrue(any(path.name.startswith("restore-backup-") for path in recycle_dir.iterdir()))
            self.assertIn("set-instance-status --user-id alice --status active", (root / "metadata.log").read_text(encoding="utf-8"))
            self.assertIn("--port 30123", (root / "metadata.log").read_text(encoding="utf-8"))
            docker_log = (root / "docker.log").read_text(encoding="utf-8")
            self.assertIn("allocator|prepare --network openclaw-user-", docker_log)
            self.assertIn("compose up -d", docker_log)
            self.assertIn("exec openclaw-nginx nginx -t", docker_log)
            self.assertIn("exec openclaw-nginx nginx -s reload", docker_log)

    def test_restore_preflight_blocks_existing_user_dir(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            self.write_fake_docker(root)

            public_dir = root / "public"
            recycle_dir = public_dir / "deleted" / "alice_20260705_010203"
            nginx_conf_dir = root / "nginx" / "conf"
            nginx_compose_dir = root / "nginx" / "compose"
            nginx_compose_file = nginx_compose_dir / "docker-compose.yml"
            (public_dir / "users" / "alice").mkdir(parents=True)
            (recycle_dir / "user").mkdir(parents=True)
            (recycle_dir / "nginx").mkdir(parents=True)
            nginx_conf_dir.mkdir(parents=True)
            nginx_compose_dir.mkdir(parents=True)
            (recycle_dir / "user" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (recycle_dir / "nginx" / "alice.conf").write_text("listen 30123 ssl;\n", encoding="utf-8")
            nginx_compose_file.write_text("services:\n  nginx:\n    ports: []\n", encoding="utf-8")
            self.write_config(manager, public_dir, nginx_compose_file, nginx_compose_dir, nginx_conf_dir)

            result = self.run_restore(manager, "alice", root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("User already exists: alice", result.stdout + result.stderr)
            self.assertTrue((recycle_dir / "user" / "docker-compose.yml").is_file())

    def test_restore_preflight_requires_nginx_conf_for_current_layout(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            self.write_fake_docker(root)

            public_dir = root / "public"
            recycle_dir = public_dir / "deleted" / "alice_20260705_010203"
            nginx_conf_dir = root / "nginx" / "conf"
            nginx_compose_dir = root / "nginx" / "compose"
            nginx_compose_file = nginx_compose_dir / "docker-compose.yml"
            (recycle_dir / "user").mkdir(parents=True)
            nginx_conf_dir.mkdir(parents=True)
            nginx_compose_dir.mkdir(parents=True)
            (recycle_dir / "user" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (public_dir / "users.csv").write_text(
                "user_id,port,created_at,status\nalice,30123,2026-07-05,deleted\n",
                encoding="utf-8",
            )
            nginx_compose_file.write_text("services:\n  nginx:\n    ports: []\n", encoding="utf-8")
            self.write_config(manager, public_dir, nginx_compose_file, nginx_compose_dir, nginx_conf_dir)

            result = self.run_restore(manager, "alice", root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Recycle nginx config missing", result.stdout + result.stderr)
            self.assertTrue((recycle_dir / "user" / "docker-compose.yml").is_file())
            self.assertFalse((public_dir / "users" / "alice").exists())
            self.assertFalse((root / "docker.log").exists())

    def test_restore_legacy_layout_without_nginx_conf_still_succeeds(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            self.write_fake_docker(root)

            public_dir = root / "public"
            recycle_dir = public_dir / "deleted" / "alice_20260705_010203"
            nginx_conf_dir = root / "nginx" / "conf"
            nginx_compose_dir = root / "nginx" / "compose"
            nginx_compose_file = nginx_compose_dir / "docker-compose.yml"
            recycle_dir.mkdir(parents=True)
            (public_dir / "users").mkdir(parents=True)
            nginx_conf_dir.mkdir(parents=True)
            nginx_compose_dir.mkdir(parents=True)
            (recycle_dir / "docker-compose.yml").write_text(LEGACY_USER_COMPOSE, encoding="utf-8")
            (public_dir / "users.csv").write_text(
                "user_id,port,created_at,status\nalice,30123,2026-07-05,deleted\n",
                encoding="utf-8",
            )
            nginx_compose_file.write_text(
                "services:\n  nginx:\n    ports:\n      - \"30015:30015\"\n",
                encoding="utf-8",
            )
            self.write_config(manager, public_dir, nginx_compose_file, nginx_compose_dir, nginx_conf_dir)

            result = self.run_restore(manager, "alice", root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((public_dir / "users" / "alice" / "docker-compose.yml").is_file())
            self.assertIn(
                "nginx access may require manual config restore",
                result.stdout + result.stderr,
            )

if __name__ == "__main__":
    unittest.main()
