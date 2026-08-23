import importlib.util
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("inventory_openclaw_auth", SCRIPT_DIR / "inventory_openclaw_auth.py")
INVENTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INVENTORY)


class OpenClawAuthInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.public = self.root / "public"
        self.nginx = self.root / "nginx"
        self.db = self.public / "manager.db"
        self.public.mkdir()
        self.nginx.mkdir()
        with sqlite3.connect(self.db) as connection:
            connection.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username, normalized_username);
                CREATE TABLE instances (
                    public_id, legacy_user_id, instance_name, status, data_path,
                    nginx_conf_path, product, owner_user_id
                );
                INSERT INTO users VALUES (1, 'alice', 'alice');
            """)

    def tearDown(self):
        self.temp.cleanup()

    def add_instance(self, public_id, auth, *, compose="", nginx="", status="active", nginx_path=None):
        data = self.public / "users" / public_id
        (data / "config").mkdir(parents=True)
        (data / "config/openclaw.json").write_text(json.dumps({"gateway": auth}), encoding="utf-8")
        (data / "docker-compose.yml").write_text(compose, encoding="utf-8")
        conf = nginx_path or self.nginx / f"{public_id}.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(nginx, encoding="utf-8")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO instances VALUES (?,?,?,?,?,?,?,1)",
                (public_id, public_id, public_id, status, str(data), str(conf), "openclaw"),
            )

    def test_classifies_token_and_ready_trusted_proxy(self):
        self.add_instance("token-user", {"auth": {"mode": "token", "token": "secret"}}, nginx='server { listen 30101; location / { auth_basic "OpenClaw Login"; } }')
        self.add_instance(
            "proxy-user",
            {"auth": {"mode": "trusted-proxy", "password": "secret", "trustedProxy": {"userHeader": "x-forwarded-user"}}, "trustedProxies": ["172.30.0.2"]},
            nginx='''server { listen 30102;
    location / { auth_basic off; auth_request /_instance_auth;
      set $openclaw_upstream "openclaw_proxy-user:18789";
      auth_request_set $authenticated_user $upstream_http_x_openclaw_authenticated_user;
      proxy_set_header X-Forwarded-User $authenticated_user;
      proxy_set_header X-OpenClaw-Authenticated-By "openclaw-manager";
    }
    upstream instance_auth_proxy { server openclaw-instance-auth-proxy:8084 resolve; }
}''',
        )
        output = io.StringIO()
        self.assertEqual(INVENTORY.main(["--db", str(self.db), "--public-dir", str(self.public), "--nginx-dir", str(self.nginx)], output), 0)
        self.assertIn("needs-migration", output.getvalue())
        self.assertIn("ready", output.getvalue())
        self.assertIn("total=2 ready=1 needs-migration=1 inconsistent=0", output.getvalue())

    def test_reports_trusted_proxy_conflicts_and_csv(self):
        self.add_instance(
            "broken", {"auth": {"mode": "trusted-proxy"}, "trustedProxies": []},
            compose="OPENCLAW_GATEWAY_TOKEN=legacy", nginx="server { listen 30103; }",
        )
        output = io.StringIO()
        self.assertEqual(INVENTORY.main(["--db", str(self.db), "--public-dir", str(self.public), "--nginx-dir", str(self.nginx), "--format", "csv"], output), 1)
        self.assertIn("inconsistent", output.getvalue())
        self.assertIn("gateway_token_conflict", output.getvalue())
        self.assertIn("control_password_missing", output.getvalue())

    def test_stopped_instance_resolves_disabled_nginx_configuration(self):
        self.add_instance(
            "stopped", {"auth": {"mode": "token", "token": "secret"}},
            status="stopped", nginx='server { listen 30104; location / { auth_basic "OpenClaw Login"; } }',
            nginx_path=self.nginx / "_disabled/stopped.conf",
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE instances SET nginx_conf_path=?", (str(self.nginx / "stopped.conf"),))
        output = io.StringIO()
        self.assertEqual(INVENTORY.main([
            "--db", str(self.db), "--public-dir", str(self.public),
            "--nginx-dir", str(self.nginx),
        ], output), 0)
        self.assertNotIn("nginx_missing", output.getvalue())

    def test_trusted_proxy_with_basic_auth_is_pending_not_inconsistent(self):
        self.add_instance(
            "proxy-basic",
            {"auth": {"mode": "trusted-proxy", "password": "secret", "trustedProxy": {"userHeader": "x-forwarded-user"}}, "trustedProxies": ["172.30.0.2"]},
            nginx='''server { listen 30105;
    location / { auth_basic "OpenClaw Login"; auth_request /_instance_auth;
      set $openclaw_upstream "openclaw_proxy-basic:18789";
      auth_request_set $authenticated_user $upstream_http_x_openclaw_authenticated_user;
      proxy_set_header X-Forwarded-User $authenticated_user;
      proxy_set_header X-OpenClaw-Authenticated-By "openclaw-manager";
    }
    upstream instance_auth_proxy { server openclaw-instance-auth-proxy:8084 resolve; }
}''',
        )
        output = io.StringIO()
        self.assertEqual(INVENTORY.main([
            "--db", str(self.db), "--public-dir", str(self.public),
            "--nginx-dir", str(self.nginx),
        ], output), 0)
        self.assertIn("needs-migration", output.getvalue())
        self.assertIn("basic_auth_not_disabled", output.getvalue())
        self.assertIn("inconsistent=0", output.getvalue())

    def test_trusted_proxy_ignores_basic_auth_on_legacy_admin_redirect(self):
        self.add_instance(
            "proxy-admin",
            {"auth": {"mode": "trusted-proxy", "password": "secret", "trustedProxy": {"userHeader": "x-forwarded-user"}}, "trustedProxies": ["172.30.0.2"]},
            nginx='''upstream instance_auth_proxy { server openclaw-instance-auth-proxy:8084 resolve; }
server { listen 30106;
    location / { auth_request /_instance_auth;
      auth_request_set $authenticated_user $upstream_http_x_openclaw_authenticated_user;
      proxy_set_header X-Forwarded-User $authenticated_user;
      proxy_set_header X-OpenClaw-Authenticated-By "openclaw-manager";
    }
    location /admin/ { return 302 https://manager.example/;
      auth_basic "OpenClaw Login";
      auth_basic_user_file /etc/nginx/auth/users/proxy-admin/.htpasswd;
    }
}''',
        )
        output = io.StringIO()
        self.assertEqual(INVENTORY.main([
            "--db", str(self.db), "--public-dir", str(self.public),
            "--nginx-dir", str(self.nginx),
        ], output), 0)
        self.assertIn("disabled", output.getvalue())
        self.assertIn("total=1 ready=1 needs-migration=0 inconsistent=0", output.getvalue())

    def test_rejects_data_path_outside_managed_public_directory(self):
        outside = self.root / "outside"
        (outside / "config").mkdir(parents=True)
        (outside / "config/openclaw.json").write_text(
            json.dumps({"gateway": {"auth": {"mode": "token", "token": "secret"}}}),
            encoding="utf-8",
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO instances VALUES (?,?,?,?,?,?,?,1)",
                ("outside", "outside", "outside", "active", str(outside), None, "openclaw"),
            )
        output = io.StringIO()
        self.assertEqual(INVENTORY.main([
            "--db", str(self.db), "--public-dir", str(self.public),
            "--nginx-dir", str(self.nginx),
        ], output), 1)
        self.assertIn("config_unreadable", output.getvalue())

    def test_excludes_failed_instances_from_migration_inventory(self):
        self.add_instance(
            "active", {"auth": {"mode": "token", "token": "secret"}},
            nginx='server { listen 30106; location / { auth_basic "OpenClaw Login"; } }',
        )
        self.add_instance(
            "failed", {"auth": {"mode": "token", "token": "secret"}},
            status="failed",
            nginx='server { listen 30107; location / { auth_basic "OpenClaw Login"; } }',
        )
        output = io.StringIO()
        self.assertEqual(INVENTORY.main([
            "--db", str(self.db), "--public-dir", str(self.public),
            "--nginx-dir", str(self.nginx),
        ], output), 0)
        self.assertIn("total=1", output.getvalue())
        self.assertNotIn("failed  ", output.getvalue())


if __name__ == "__main__":
    unittest.main()
