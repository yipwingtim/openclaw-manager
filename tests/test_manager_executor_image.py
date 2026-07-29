import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ManagerExecutorImageTests(unittest.TestCase):
    def test_image_installs_instance_creation_commands(self):
        dockerfile = (
            ROOT_DIR / "services" / "manager-executor" / "Dockerfile"
        ).read_text(encoding="utf-8")

        for package in ("apache2-utils", "iproute2"):
            with self.subTest(package=package):
                self.assertIn(package, dockerfile)


if __name__ == "__main__":
    unittest.main()
