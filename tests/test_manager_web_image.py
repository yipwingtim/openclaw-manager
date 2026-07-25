import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ManagerWebImageTests(unittest.TestCase):
    def test_dockerfile_copies_all_local_runtime_modules(self):
        dockerfile = (ROOT_DIR / "services" / "manager-web" / "Dockerfile").read_text(
            encoding="utf-8"
        )

        for module in ("app.py", "auth_providers.py", "control_client.py", "instance_adapters.py", "metadata_store.py"):
            with self.subTest(module=module):
                self.assertIn(f"COPY {module} .", dockerfile)


if __name__ == "__main__":
    unittest.main()
