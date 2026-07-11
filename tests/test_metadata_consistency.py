#!/usr/bin/env python3

import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
CHECKER = runpy.run_path(str(ROOT_DIR / "scripts" / "check_metadata_consistency.py"))
Reporter = CHECKER["Reporter"]
check_deleted_recycle_dirs = CHECKER["check_deleted_recycle_dirs"]
check_user = CHECKER["check_user"]


class MetadataConsistencyTests(unittest.TestCase):
    def configure_paths(self, root):
        conf_dir = root / "nginx" / "conf"
        auth_dir = root / "nginx" / "auth"
        conf_dir.mkdir(parents=True)
        auth_dir.mkdir(parents=True)
        check_user.__globals__["NGINX_USERS_CONF_DIR"] = conf_dir
        check_user.__globals__["NGINX_AUTH_DIR"] = auth_dir
        return conf_dir

    def write_user(self, root, user_id):
        user_dir = root / "users" / user_id
        user_dir.mkdir(parents=True)
        (user_dir / "docker-compose.yml").write_text(
            "services:\n"
            f"  openclaw-{user_id}:\n"
            f"    container_name: openclaw_{user_id}\n"
            "    networks:\n"
            "      - agent-net\n",
            encoding="utf-8",
        )
        return user_dir

    def write_dynamic_conf(self, path, user_id, port=30123):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"upstream agent_{user_id}_1 {{\n"
            f"    zone agent_{user_id}_1 64k;\n"
            "    resolver 127.0.0.11 valid=10s ipv6=off;\n"
            f"    server openclaw_{user_id}:18789 resolve;\n"
            "}\n"
            "server {\n"
            f"    listen {port} ssl;\n"
            "    location / {\n"
            f"        proxy_pass http://agent_{user_id}_1;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )

    def test_disabled_nginx_config_is_checked_without_missing_error(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conf_dir = self.configure_paths(root)
            self.write_dynamic_conf(conf_dir / "_disabled" / "alice.conf", "alice")
            user_dir = self.write_user(root, "alice")
            reporter = Reporter()

            check_user(
                "alice",
                user_dir,
                {"alice": {"status": "active", "port": 30123}},
                {"alice": {"status": "stopped", "port": 30123}},
                {30123: {"status": "allocated", "user_id": "alice"}},
                reporter,
            )

            codes = {issue.code for issue in reporter.issues}
            self.assertNotIn("nginx_conf_missing", codes)
            self.assertNotIn("nginx_upstream_not_dynamic", codes)

    def test_active_and_disabled_nginx_configs_report_conflict(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conf_dir = self.configure_paths(root)
            self.write_dynamic_conf(conf_dir / "alice.conf", "alice")
            self.write_dynamic_conf(conf_dir / "_disabled" / "alice.conf", "alice")
            user_dir = self.write_user(root, "alice")
            reporter = Reporter()

            check_user(
                "alice",
                user_dir,
                {"alice": {"status": "active", "port": 30123}},
                {"alice": {"status": "active", "port": 30123}},
                {30123: {"status": "allocated", "user_id": "alice"}},
                reporter,
            )

            self.assertIn(
                "nginx_conf_multiple_locations",
                {issue.code for issue in reporter.issues},
            )

    def test_legacy_recycle_missing_nginx_is_warning(self):
        with TemporaryDirectory() as temp_dir:
            recycle_dir = Path(temp_dir) / "alice_20260711_120000"
            recycle_dir.mkdir()
            (recycle_dir / "docker-compose.yml").write_text(
                "services:\n  app:\n",
                encoding="utf-8",
            )
            reporter = Reporter()

            check_deleted_recycle_dirs(
                [{"user_id": "alice", "path": recycle_dir}],
                reporter,
            )

            nginx_issues = [
                issue for issue in reporter.issues
                if issue.code == "deleted_recycle_nginx_conf_missing"
            ]
            self.assertEqual(len(nginx_issues), 1)
            self.assertEqual(nginx_issues[0].level, "WARN")

    def test_current_recycle_missing_nginx_remains_error(self):
        with TemporaryDirectory() as temp_dir:
            recycle_dir = Path(temp_dir) / "alice_20260711_120000"
            user_dir = recycle_dir / "user"
            user_dir.mkdir(parents=True)
            (user_dir / "docker-compose.yml").write_text(
                "services:\n  app:\n",
                encoding="utf-8",
            )
            reporter = Reporter()

            check_deleted_recycle_dirs(
                [{"user_id": "alice", "path": recycle_dir}],
                reporter,
            )

            nginx_issues = [
                issue for issue in reporter.issues
                if issue.code == "deleted_recycle_nginx_conf_missing"
            ]
            self.assertEqual(len(nginx_issues), 1)
            self.assertEqual(nginx_issues[0].level, "ERROR")


if __name__ == "__main__":
    unittest.main()

