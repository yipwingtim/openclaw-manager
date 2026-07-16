#!/usr/bin/env python3

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
NETWORK_HELPER = ROOT_DIR / "scripts" / "lib_tenant_network.sh"
MIGRATION_SCRIPT = ROOT_DIR / "scripts" / "migrate_tenant_networks.sh"
COMPOSE_TEMPLATE = ROOT_DIR / "templates" / "docker-compose.tpl.yml"


class TenantNetworkIsolationTests(unittest.TestCase):
    def test_migration_preserves_running_stopped_and_paused_states(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "manager"
            scripts_dir = manager / "scripts"
            config_dir = manager / "config"
            public_dir = root / "public"
            bin_dir = root / "bin"
            scripts_dir.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            bin_dir.mkdir()
            shutil.copy2(NETWORK_HELPER, scripts_dir / NETWORK_HELPER.name)
            shutil.copy2(MIGRATION_SCRIPT, scripts_dir / MIGRATION_SCRIPT.name)

            (config_dir / "openclaw-manager.env").write_text(
                f"OPENCLAW_PUBLIC_DIR={public_dir}\n",
                encoding="utf-8",
            )
            legacy_compose = textwrap.dedent(
                """
                services:
                  app:
                    networks:
                      - agent-net
                networks:
                  agent-net:
                    external: true
                """
            ).lstrip()
            for user_id in ("alice", "bob", "carol"):
                user_dir = public_dir / "users" / user_id
                user_dir.mkdir(parents=True)
                (user_dir / "docker-compose.yml").write_text(
                    legacy_compose,
                    encoding="utf-8",
                )

            fake_docker = bin_dir / "docker"
            fake_docker.write_text(
                textwrap.dedent(
                    """
                    #!/bin/sh
                    printf '%s|%s\n' "$PWD" "$*" >> "$DOCKER_LOG"
                    if [ "$1" = "ps" ]; then
                      printf '%s\n' openclaw_alice openclaw_bob openclaw_carol
                    elif [ "$1" = "inspect" ] && [ "$2" = "-f" ]; then
                      case "$4:$3" in
                        openclaw_alice:*Running*) printf 'true\n' ;;
                        openclaw_bob:*Running*) printf 'false\n' ;;
                        openclaw_carol:*Running*) printf 'true\n' ;;
                        openclaw_carol:*Paused*) printf 'true\n' ;;
                        *:*Paused*) printf 'false\n' ;;
                      esac
                    elif [ "$1" = "inspect" ] && [ "$2" = "--format" ]; then
                      case "$3" in
                        openclaw_alice) printf 'openclaw-user-alice\n' ;;
                        openclaw_bob) printf 'openclaw-user-bob\n' ;;
                        openclaw_carol) printf 'openclaw-user-carol\n' ;;
                      esac
                    fi
                    exit 0
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["DOCKER_LOG"] = str(root / "docker.log")

            result = subprocess.run(
                ["bash", str(scripts_dir / MIGRATION_SCRIPT.name)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            docker_log = (root / "docker.log").read_text(encoding="utf-8")
            self.assertEqual(docker_log.count("|ps -a --format {{.Names}}"), 3)
            self.assertIn("/users/alice|compose up -d --force-recreate", docker_log)
            self.assertIn("/users/bob|compose create --force-recreate", docker_log)
            self.assertNotIn("/users/bob|compose up", docker_log)
            self.assertIn("unpause openclaw_carol", docker_log)
            self.assertIn("/users/carol|compose up -d --force-recreate", docker_log)
            self.assertIn("pause openclaw_carol", docker_log)

    def test_compose_template_uses_per_tenant_network(self):
        template = COMPOSE_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("- tenant-net", template)
        self.assertIn("name: {{TENANT_NETWORK}}", template)
        self.assertNotIn("- agent-net", template)

    def test_legacy_compose_is_migrated_to_named_tenant_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            compose_file = Path(temp_dir) / "docker-compose.yml"
            compose_file.write_text(
                textwrap.dedent(
                    """
                    services:
                      openclaw-alice:
                        networks:
                          - agent-net
                    networks:
                      agent-net:
                        external: true
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; ensure_tenant_compose_network "$2" "$3"',
                    "bash",
                    str(NETWORK_HELPER),
                    str(compose_file),
                    "openclaw-user-alice",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            migrated = compose_file.read_text(encoding="utf-8")
            self.assertIn("- tenant-net", migrated)
            self.assertIn("name: openclaw-user-alice", migrated)
            self.assertNotIn("agent-net", migrated)

    def test_empty_container_list_is_safe_with_pipefail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_docker = Path(temp_dir) / "docker"
            fake_docker.write_text(
                "#!/bin/sh\nexit 0\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp_dir}:{env['PATH']}"

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -euo pipefail; source "$1"; '
                    'connect_shared_services_to_tenant_networks nginx proxy',
                    "bash",
                    str(NETWORK_HELPER),
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
