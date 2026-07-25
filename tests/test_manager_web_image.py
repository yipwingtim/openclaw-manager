import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ManagerWebImageTests(unittest.TestCase):
    def test_dockerfile_contains_only_unprivileged_web_runtime(self):
        dockerfile = (ROOT_DIR / "services" / "manager-web" / "Dockerfile").read_text(
            encoding="utf-8"
        )

        for module in (
            "user_app.py", "admin_app.py", "web_common.py",
            "auth_providers.py", "control_client.py", "executor_client.py",
        ):
            with self.subTest(module=module):
                self.assertIn(f"COPY {module} .", dockerfile)
        for forbidden in ("docker-cli", "instance_adapters.py", "metadata_store.py"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, dockerfile)


if __name__ == "__main__":
    unittest.main()
