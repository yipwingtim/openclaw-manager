import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT_DIR / "config" / "openclaw-manager.env.example"
COMPOSE_FILE = ROOT_DIR / "services" / "docker-compose.yml"


class ConfigurableRuntimePathTests(unittest.TestCase):
    def test_public_runtime_paths_follow_configured_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "agent-public"
            env_file = Path(temp_dir) / "openclaw-manager.env"
            env_file.write_text(
                ENV_EXAMPLE.read_text(encoding="utf-8").replace(
                    "OPENCLAW_PUBLIC_DIR=/data/docker/openclaw-public",
                    f"OPENCLAW_PUBLIC_DIR={runtime_root}",
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; printf "%s\\n" "$OPENCLAW_PUBLIC_DIR"; '
                    'printf "%s\\n" "${PORT_FILE-unset}" "${USERS_CSV-unset}" '
                    '"${METADATA_DB_FILE-unset}" '
                    '"${OPENCLAW_TENANT_NETWORK_LOCK_FILE-unset}" '
                    '"${MODEL_PROXY_TOKEN_DIR-unset}"',
                    "bash",
                    str(env_file),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )

        self.assertEqual(
            result.stdout.splitlines(),
            [
                str(runtime_root),
                "unset",
                "unset",
                "unset",
                "unset",
                "unset",
            ],
        )

        for path in (
            ROOT_DIR / "scripts" / "bootstrap_runtime.sh",
            ROOT_DIR / "scripts" / "check_bootstrap_readiness.sh",
            ROOT_DIR / "services" / "manager-control" / "app.py",
            ROOT_DIR / "services" / "manager-web" / "metadata_store.py",
            ROOT_DIR / "services" / "model-proxy" / "app.py",
        ):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("OPENCLAW_PUBLIC_DIR", source)

    def test_legacy_scripts_load_runtime_paths_from_manager_config(self):
        for relative_path in (
            "scripts/list_users.sh",
            "scripts/update_allowed_origins.sh",
            "scripts/set_model_provider.sh",
            "scripts/batch_set_model_provider.sh",
        ):
            source = (ROOT_DIR / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertIn("OPENCLAW_PUBLIC_DIR", source)

        list_users = (ROOT_DIR / "scripts" / "list_users.sh").read_text(
            encoding="utf-8"
        )
        update_origins = (
            ROOT_DIR / "scripts" / "update_allowed_origins.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn('BASE_DIR="/data/docker/openclaw-public"', list_users)
        self.assertNotIn('BASE_DIR="/data/docker/openclaw-public"', update_origins)

    def test_model_proxy_mount_follows_runtime_root_with_optional_override(self):
        compose = COMPOSE_FILE.read_text(encoding="utf-8")
        derived_path = (
            "${MODEL_PROXY_TOKEN_DIR:-"
            "${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}/model-proxy-tokens}"
        )
        self.assertIn(f"{derived_path}:{derived_path}:ro", compose)

    def test_bootstrap_and_readiness_select_metadata_backend(self):
        for relative_path in (
            "scripts/bootstrap_runtime.sh",
            "scripts/check_bootstrap_readiness.sh",
            "scripts/init_metadata_db.sh",
        ):
            source = (ROOT_DIR / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertIn('METADATA_DB_BACKEND="${METADATA_DB_BACKEND:-sqlite}"', source)
                self.assertIn('METADATA_DATABASE_URL', source)


if __name__ == "__main__":
    unittest.main()
