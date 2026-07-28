import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT_DIR / "services" / "docker-compose.yml"


class WebServiceSplitTests(unittest.TestCase):
    def test_user_and_admin_web_have_separate_unprivileged_services(self):
        compose = COMPOSE_FILE.read_text(encoding="utf-8")

        self.assertIn("  manager-user-web:\n", compose)
        self.assertIn("  manager-admin-web:\n", compose)
        self.assertIn("  manager-web:\n", compose)

        user_block = compose.split("  manager-user-web:\n", 1)[1].split(
            "  manager-admin-web:\n", 1
        )[0]
        admin_block = compose.split("  manager-admin-web:\n", 1)[1].split(
            "  manager-web:\n", 1
        )[0]
        legacy_admin_block = compose.split("  manager-web:\n", 1)[1].split(
            "  pdf-extract-text:\n", 1
        )[0]
        for name, block in (("user", user_block), ("admin", admin_block)):
            with self.subTest(service=name):
                self.assertNotIn("/var/run/docker.sock", block)
                self.assertNotIn("OPENCLAW_PUBLIC_DIR", block)
                self.assertNotIn("NGINX_USERS_CONF_DIR", block)
                self.assertNotIn("NGINX_AUTH_DIR", block)
                self.assertNotIn("NGINX_COMPOSE_DIR", block)

        self.assertIn("MANAGER_WEB_ROLE: user", user_block)
        self.assertIn("MANAGER_WEB_ROLE: admin", admin_block)
        self.assertIn("healthcheck:", user_block)
        self.assertIn("healthcheck:", admin_block)
        self.assertIn("container_name: openclaw-manager-web", legacy_admin_block)
        self.assertIn("dockerfile: Dockerfile.legacy", legacy_admin_block)
        self.assertIn("/var/run/docker.sock", legacy_admin_block)
        self.assertNotIn("ports:", legacy_admin_block)

        executor_api_block = compose.split("  manager-executor-api:\n", 1)[1].split(
            "  manager-user-web:\n", 1
        )[0]
        executor_block = compose.split("  manager-executor:\n", 1)[1].split(
            "  manager-executor-api:\n", 1
        )[0]
        self.assertIn("NGINX_AUTH_DIR", executor_block)
        self.assertIn("NGINX_AUTH_DIR:-/data/docker/nginx/auth}:ro", executor_block)
        self.assertIn("healthcheck:", executor_api_block)
        self.assertNotIn("NGINX_AUTH_DIR", executor_api_block)
        self.assertNotIn("NGINX_USERS_CONF_DIR", executor_api_block)

    def test_split_web_entrypoints_do_not_access_metadata_or_runtime(self):
        for filename in ("user_app.py", "admin_app.py"):
            source = (ROOT_DIR / "services" / "manager-web" / filename).read_text(
                encoding="utf-8"
            )
            with self.subTest(filename=filename):
                self.assertNotIn("metadata_store", source)
                self.assertNotIn("instance_adapters", source)
                self.assertNotIn("subprocess", source)

    def test_nginx_routes_admin_and_user_web_separately(self):
        template = (
            ROOT_DIR / "templates" / "nginx" / "manager-web.conf.tpl"
        ).read_text(encoding="utf-8")

        self.assertIn("server openclaw-manager-user-web:8080 resolve;", template)
        self.assertIn("server openclaw-manager-web:8080 resolve;", template)
        self.assertIn("location ^~ /admin", template)
        self.assertIn("proxy_pass http://manager_legacy_admin_backend;", template)
        self.assertIn("proxy_pass http://manager_user_web_backend;", template)

    def test_user_web_keeps_legacy_instance_admin_entry(self):
        source = (
            ROOT_DIR / "services" / "manager-web" / "user_app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('@app.get("/instance-admin/")', source)
        self.assertIn('request.headers.get("X-OpenClaw-User"', source)

    def test_external_admin_login_uses_the_single_registered_callback(self):
        source = (ROOT_DIR / "services" / "manager-web" / "admin_app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('return redirect("/login")', source)
        self.assertNotIn('"/admin/auth/callback"', source)

    def test_admin_local_login_is_exempt_from_authenticated_csrf_check(self):
        source = (ROOT_DIR / "services" / "manager-web" / "web_common.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('{"/login", "/admin/login"}', source)

    def test_legacy_admin_has_namespaced_auth_routes(self):
        source = (ROOT_DIR / "services" / "manager-web" / "app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('@app.get("/admin/login")', source)
        self.assertIn('@app.post("/admin/login")', source)
        self.assertIn('@app.post("/admin/logout")', source)
        self.assertIn('@app.get("/health")', source)

    def test_legacy_admin_redirects_split_instance_entry(self):
        source = (ROOT_DIR / "services" / "manager-web" / "app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('@app.get("/admin/instances")', source)
        self.assertIn('return redirect(url_for("admin_users"))', source)


if __name__ == "__main__":
    unittest.main()
