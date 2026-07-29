import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ManagerExecutorImageTests(unittest.TestCase):
    def test_image_installs_htpasswd_for_instance_creation(self):
        dockerfile = (
            ROOT_DIR / "services" / "manager-executor" / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertIn("apache2-utils", dockerfile)


if __name__ == "__main__":
    unittest.main()
