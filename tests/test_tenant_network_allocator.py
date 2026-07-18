#!/usr/bin/env python3

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ALLOCATOR = ROOT_DIR / "scripts" / "tenant_network_allocator.py"


class TenantNetworkAllocatorTests(unittest.TestCase):
    def make_env(self, root, docker_script, ip_output="[]"):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(docker_script, encoding="utf-8")
        (bin_dir / "ip").write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{ip_output}'\n", encoding="utf-8"
        )
        for command in ("docker", "ip"):
            (bin_dir / command).chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["DOCKER_LOG"] = str(root / "docker.log")
        return env

    def command(self, root, *extra):
        return [
            "python3", str(ALLOCATOR), "ensure",
            "--network", "openclaw-user-alice",
            "--pool", "10.250.0.0/24",
            "--subnet-prefix", "28",
            "--lock-file", str(root / "tenant-network.lock"),
            *extra,
        ]

    def test_skips_excluded_and_existing_subnets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_env(root, textwrap.dedent("""
                #!/bin/sh
                printf '%s\n' "$*" >> "$DOCKER_LOG"
                if [ "$1" = network ] && [ "$2" = inspect ] && [ "$3" = openclaw-user-alice ]; then
                  exit 1
                fi
                if [ "$1" = network ] && [ "$2" = ls ]; then
                  printf 'existing-network\n'
                  exit 0
                fi
                if [ "$1" = network ] && [ "$2" = inspect ] && [ "$3" = existing-network ]; then
                  printf '[{"Labels":{"com.openclaw.tenant-network":"existing-network"},"IPAM":{"Config":[{"Subnet":"10.250.0.16/28"}]}}]\n'
                  exit 0
                fi
                exit 0
            """).lstrip())
            result = subprocess.run(
                self.command(root, "--exclude", "10.250.0.0/28"),
                text=True, capture_output=True, env=env, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            log = (root / "docker.log").read_text(encoding="utf-8")
            self.assertIn(
                "network create --driver bridge --subnet 10.250.0.32/28 "
                "--label com.openclaw.tenant-network=openclaw-user-alice openclaw-user-alice",
                log,
            )

    def test_rejects_pool_overlapping_host_route(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_env(
                root, "#!/bin/sh\nexit 1\n",
                '[{"dst":"10.250.0.0/24","dev":"eth0"}]',
            )
            result = subprocess.run(
                self.command(root), text=True, capture_output=True, env=env, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("overlaps host route", result.stderr)

    def test_rejects_existing_network_outside_managed_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_env(root, textwrap.dedent("""
                #!/bin/sh
                if [ "$1" = network ] && [ "$2" = inspect ]; then
                  printf '[{"Labels":{"com.openclaw.tenant-network":"openclaw-user-alice"},"IPAM":{"Config":[{"Subnet":"192.168.64.0/20"}]}}]\n'
                  exit 0
                fi
                exit 1
            """).lstrip())
            result = subprocess.run(
                self.command(root), text=True, capture_output=True, env=env, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside configured tenant pool", result.stderr)

    def test_rejects_existing_network_without_matching_owner_label(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_env(root, textwrap.dedent("""
                #!/bin/sh
                if [ "$1" = network ] && [ "$2" = inspect ]; then
                  printf '[{"Labels":{},"IPAM":{"Config":[{"Subnet":"10.250.0.0/28"}]}}]\n'
                  exit 0
                fi
                if [ "$1" = network ] && [ "$2" = ls ]; then
                  exit 0
                fi
                exit 1
            """).lstrip())
            result = subprocess.run(
                self.command(root), text=True, capture_output=True, env=env, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not owned", result.stderr)

    def test_plan_is_read_only_and_assigns_all_requested_networks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_env(root, textwrap.dedent("""
                #!/bin/sh
                printf '%s\n' "$*" >> "$DOCKER_LOG"
                if [ "$1" = network ] && [ "$2" = inspect ]; then
                  exit 1
                fi
                if [ "$1" = network ] && [ "$2" = ls ]; then
                  exit 0
                fi
                exit 0
            """).lstrip())
            result = subprocess.run(
                [
                    "python3", str(ALLOCATOR), "plan",
                    "--network", "openclaw-user-alice",
                    "--network", "openclaw-user-bob",
                    "--pool", "10.250.0.0/24",
                    "--subnet-prefix", "28",
                ],
                text=True, capture_output=True, env=env, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "openclaw-user-alice\t10.250.0.0/28\tcreate",
                    "openclaw-user-bob\t10.250.0.16/28\tcreate",
                ],
            )
            log = (root / "docker.log").read_text(encoding="utf-8")
            self.assertNotIn("network create", log)
            self.assertFalse((root / "tenant-network.lock").exists())

    def test_prepare_checks_capacity_before_creating_any_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_env(root, textwrap.dedent("""
                #!/bin/sh
                printf '%s\n' "$*" >> "$DOCKER_LOG"
                if [ "$1" = network ] && [ "$2" = inspect ]; then
                  exit 1
                fi
                if [ "$1" = network ] && [ "$2" = ls ]; then
                  exit 0
                fi
                exit 0
            """).lstrip())
            result = subprocess.run(
                [
                    "python3", str(ALLOCATOR), "prepare",
                    "--network", "openclaw-user-alice",
                    "--network", "openclaw-user-bob",
                    "--pool", "10.250.0.0/28",
                    "--subnet-prefix", "28",
                    "--lock-file", str(root / "tenant-network.lock"),
                ],
                text=True, capture_output=True, env=env, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not have capacity", result.stderr)
            log = (root / "docker.log").read_text(encoding="utf-8")
            self.assertNotIn("network create", log)


    def test_prepare_rolls_back_networks_created_before_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_env(root, textwrap.dedent("""
                #!/bin/sh
                printf '%s\n' "$*" >> "$DOCKER_LOG"
                if [ "$1" = network ] && [ "$2" = inspect ]; then
                  exit 1
                fi
                if [ "$1" = network ] && [ "$2" = ls ]; then
                  exit 0
                fi
                if [ "$1" = network ] && [ "$2" = create ]; then
                  case "$*" in
                    *openclaw-user-bob) exit 1 ;;
                  esac
                fi
                exit 0
            """).lstrip())
            result = subprocess.run(
                [
                    "python3", str(ALLOCATOR), "prepare",
                    "--network", "openclaw-user-alice",
                    "--network", "openclaw-user-bob",
                    "--pool", "10.250.0.0/24",
                    "--subnet-prefix", "28",
                    "--lock-file", str(root / "tenant-network.lock"),
                ],
                text=True, capture_output=True, env=env, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            log = (root / "docker.log").read_text(encoding="utf-8")
            first_create = (
                "network create --driver bridge --subnet 10.250.0.0/28 "
                "--label com.openclaw.tenant-network=openclaw-user-alice openclaw-user-alice"
            )
            second_create = (
                "network create --driver bridge --subnet 10.250.0.16/28 "
                "--label com.openclaw.tenant-network=openclaw-user-bob openclaw-user-bob"
            )
            self.assertIn(first_create, log)
            self.assertIn(second_create, log)
            self.assertIn("network rm openclaw-user-alice", log)


if __name__ == "__main__":
    unittest.main()
