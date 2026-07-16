#!/usr/bin/env python3

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
NETWORK_HELPER = ROOT_DIR / "scripts" / "lib_tenant_network.sh"
COMPOSE_TEMPLATE = ROOT_DIR / "templates" / "docker-compose.tpl.yml"


class TenantNetworkIsolationTests(unittest.TestCase):
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
