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

    def test_product_logos_are_packaged_and_used_by_instance_list(self):
        manager_web = ROOT_DIR / "services" / "manager-web"
        dockerfile = (manager_web / "Dockerfile").read_text(encoding="utf-8")
        template = (manager_web / "templates" / "my_instances.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("COPY static ./static", dockerfile)
        for product, filename in (
            ("openclaw", "openclaw.svg"),
            ("hermes", "hermes.png"),
            ("evoscientist", "evoscientist.png"),
        ):
            with self.subTest(product=product):
                logo = manager_web / "static" / "products" / filename
                self.assertTrue(logo.is_file())
                self.assertIn(f"products/{filename}", template)
        self.assertIn("instance-product-logo-fallback", template)


if __name__ == "__main__":
    unittest.main()
