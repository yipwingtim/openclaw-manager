#!/usr/bin/env python3

import runpy
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
CHECKER = runpy.run_path(str(ROOT_DIR / "scripts" / "check_metadata_consistency.py"))
Reporter = CHECKER["Reporter"]
check_deleted_recycle_dirs = CHECKER["check_deleted_recycle_dirs"]
check_global = CHECKER["check_global"]
check_user = CHECKER["check_user"]
load_db = CHECKER["load_db"]


class MetadataConsistencyTests(unittest.TestCase):
    def test_detect_nginx_conf_reports_instance_auth(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "evo.conf"
            path.write_text(
                "upstream auth { server openclaw-instance-auth-proxy:8084 resolve; }\n"
                "server { location / { auth_request /_instance_auth; proxy_pass http://evo; } }\n",
                encoding="utf-8",
            )

            self.assertTrue(CHECKER["detect_nginx_conf"](path)["instance_auth"])

    def test_stopped_instance_allows_released_port(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conf_dir = self.configure_paths(root)
            user_dir = self.write_user(root, "alice")
            self.write_dynamic_conf(conf_dir / "_disabled" / "alice.conf", "alice", port=30123)
            reporter = Reporter()

            check_user(
                "alice",
                user_dir,
                {"alice": {"status": "stopped", "port": 30123}},
                {
                    "alice": {
                        "product": "openclaw",
                        "status": "stopped",
                        "port": 30123,
                        "container_name": "openclaw_alice",
                    }
                },
                {30123: {"status": "released", "user_id": None}},
                reporter,
            )

            self.assertNotIn("metadata_port_row_mismatch", {issue.code for issue in reporter.issues})

    def test_active_hermes_skips_openclaw_resource_checks(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.configure_paths(root)
            data_dir = root / "hermes" / "Hermes-guwei"
            data_dir.mkdir(parents=True)
            reporter = Reporter()

            check_user(
                "Hermes-guwei",
                data_dir,
                {},
                {
                    "Hermes-guwei": {
                        "product": "hermes",
                        "status": "active",
                        "container_name": "hermes_Hermes-guwei",
                    }
                },
                {},
                reporter,
            )

            self.assertEqual(reporter.issues, [])

    def test_failed_openclaw_skips_runtime_resource_checks(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.configure_paths(root)
            data_dir = root / "instances" / "openclaw" / "instance-1"
            data_dir.mkdir(parents=True)
            reporter = Reporter()

            check_user(
                "failed-openclaw",
                data_dir,
                {},
                {
                    "failed-openclaw": {
                        "product": "openclaw",
                        "status": "failed",
                        "container_name": "openclaw_failed-openclaw",
                    }
                },
                {},
                reporter,
            )

            self.assertEqual(reporter.issues, [])

    def test_global_check_uses_metadata_data_path_for_active_hermes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "hermes" / "Hermes-guwei"
            data_dir.mkdir(parents=True)
            reporter = Reporter()
            check_global.__globals__["NGINX_COMPOSE_FILE"] = root / "missing-compose.yml"
            check_global.__globals__["PORT_FILE"] = root / "missing-ports.txt"

            check_global(
                {},
                {},
                {"Hermes-guwei": {"legacy_user_id": "Hermes-guwei", "status": "active", "data_path": str(data_dir)}},
                [],
                reporter,
            )

            self.assertNotIn("metadata_dir_missing", {issue.code for issue in reporter.issues})

    def test_global_check_ignores_missing_data_for_failed_instances(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reporter = Reporter()
            check_global.__globals__["NGINX_COMPOSE_FILE"] = root / "missing-compose.yml"
            check_global.__globals__["PORT_FILE"] = root / "missing-ports.txt"

            check_global(
                {},
                {},
                {
                    "failed-openclaw": {"legacy_user_id": "failed-openclaw", "status": "failed", "data_path": str(root / "gone")},
                    "failed-hermes": {"legacy_user_id": "failed-hermes", "status": "failed", "data_path": None},
                },
                [],
                reporter,
            )

            self.assertNotIn("metadata_dir_missing", {issue.code for issue in reporter.issues})

    def test_global_check_reports_missing_data_for_active_instance(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reporter = Reporter()
            check_global.__globals__["NGINX_COMPOSE_FILE"] = root / "missing-compose.yml"
            check_global.__globals__["PORT_FILE"] = root / "missing-ports.txt"

            check_global(
                {},
                {},
                {"alice": {"legacy_user_id": "alice", "status": "active", "data_path": str(root / "gone")}},
                [],
                reporter,
            )

            self.assertIn("metadata_dir_missing", {issue.code for issue in reporter.issues})

    def test_new_instance_data_path_is_checked_as_instance_directory(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conf_dir = self.configure_paths(root)
            instance_dir = root / "instances" / "openclaw" / "instance-1"
            self.write_user(root, "alice")
            instance_dir.mkdir(parents=True)
            (instance_dir / "docker-compose.yml").write_text(
                "services:\n  openclaw-alice:\n"
                "    container_name: openclaw_alice\n"
                "    networks:\n      - tenant-net\n"
                "networks:\n  tenant-net:\n",
                encoding="utf-8",
            )
            reporter = Reporter()
            check_user(
                "alice",
                instance_dir,
                {},
                {
                    "alice": {
                        "legacy_user_id": "alice",
                        "public_id": "instance-1",
                        "product": "openclaw",
                        "status": "active",
                        "container_name": "openclaw_alice",
                    }
                },
                {},
                reporter,
            )

            self.assertNotIn("metadata_orphan_dir", {issue.code for issue in reporter.issues})

    def test_new_instances_without_legacy_ids_are_not_overwritten(self):
        with TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "manager.db"
            with sqlite3.connect(db_file) as conn:
                conn.executescript(
                    (ROOT_DIR / "db" / "schema.sql").read_text(encoding="utf-8")
                )
                conn.execute(
                    "INSERT INTO users (public_id, username, normalized_username) VALUES ('u1', 'alice', 'alice')"
                )
                owner_id = conn.execute("SELECT id FROM users").fetchone()[0]
                for public_id, runtime in (("i1", "runtime-1"), ("i2", "runtime-2")):
                    conn.execute(
                        """
                        INSERT INTO instances (
                            public_id, owner_user_id, product, instance_name,
                            runtime_identifier
                        ) VALUES (?, ?, 'openclaw', ?, ?)
                        """,
                        (public_id, owner_id, public_id, runtime),
                    )
            reporter = Reporter()

            instances, _ = load_db(db_file, reporter)

            self.assertEqual(set(instances), {"@i1", "@i2"})
            self.assertEqual(reporter.issues, [])

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
            "      - tenant-net\n"
            "networks:\n"
            "  tenant-net:\n"
            f"    name: openclaw-user-{user_id}\n",
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

    def test_current_recycle_missing_nginx_is_warning(self):
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
            self.assertEqual(nginx_issues[0].level, "WARN")

    def test_consumed_recycle_with_restore_backup_is_ignored(self):
        with TemporaryDirectory() as temp_dir:
            recycle_dir = Path(temp_dir) / "alice_20260711_120000"
            backup_dir = recycle_dir / "restore-backup-20260711_121000"
            backup_dir.mkdir(parents=True)
            (backup_dir / "docker-compose.yml").write_text(
                "services:\n  nginx:\n",
                encoding="utf-8",
            )
            (recycle_dir / "nginx").mkdir()
            reporter = Reporter()

            check_deleted_recycle_dirs(
                [{"user_id": "alice", "path": recycle_dir}],
                reporter,
            )

            self.assertEqual(reporter.issues, [])

    def test_empty_recycle_reports_one_incomplete_warning(self):
        with TemporaryDirectory() as temp_dir:
            recycle_dir = Path(temp_dir) / "alice_20260711_120000"
            recycle_dir.mkdir()
            reporter = Reporter()

            check_deleted_recycle_dirs(
                [{"user_id": "alice", "path": recycle_dir}],
                reporter,
            )

            self.assertEqual(len(reporter.issues), 1)
            self.assertEqual(reporter.issues[0].level, "WARN")
            self.assertEqual(
                reporter.issues[0].code,
                "deleted_recycle_incomplete",
            )

    def test_evoscientist_user_uses_product_specific_checks(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conf_dir = self.configure_paths(root)
            user_dir = root / "users" / "alice"
            (user_dir / "workspace").mkdir(parents=True)
            (user_dir / "evoscientist-data").mkdir()
            (conf_dir / "alice.conf").write_text(
                "upstream agent_alice_1 {\n"
                "    zone agent_alice_1 64k;\n"
                "    resolver 127.0.0.11 valid=10s ipv6=off;\n"
                "    server evoscientist_alice:4716 resolve;\n"
                "}\n"
                "server {\n"
                "    listen 40062 ssl;\n"
                "    location / { proxy_pass http://agent_alice_1; }\n"
                "}\n",
                encoding="utf-8",
            )
            reporter = Reporter()

            check_user(
                "alice",
                user_dir,
                {},
                {
                    "alice": {
                        "product": "evoscientist",
                        "status": "active",
                        "port": 40062,
                        "container_name": "evoscientist_alice",
                    }
                },
                {40062: {"status": "allocated", "user_id": "alice"}},
                reporter,
            )

            codes = {issue.code for issue in reporter.issues}
            self.assertNotIn("container_name_mismatch", codes)
            self.assertNotIn("compose_missing_agent_net", codes)
            self.assertNotIn("nginx_upstream_not_dynamic", codes)

    def test_deleted_evoscientist_skips_active_resource_checks(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            user_dir = root / "users" / "deleted-evo"
            user_dir.mkdir(parents=True)
            reporter = Reporter()
            check_user(
                "deleted-evo", user_dir, {}, {
                    "deleted-evo": {"product": "evoscientist", "status": "deleted",
                                    "public_id": "evo-public", "container_name": "evoscientist_deleted-evo"}
                }, {}, reporter,
            )
            self.assertEqual(reporter.issues, [])

    def test_evoscientist_uses_public_id_ingress_config(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conf_dir = self.configure_paths(root)
            user_dir = root / "users" / "alice"
            (user_dir / "workspace").mkdir(parents=True)
            (user_dir / "evoscientist-data").mkdir()
            ingress = root / "deleted" / "evoscientist" / "evo-public.nginx.conf"
            ingress.parent.mkdir(parents=True)
            ingress.write_text("""upstream evosci_ui_40062 {
    zone evosci_ui_40062 64k;
    resolver 127.0.0.11 valid=10s ipv6=off;
    server evoscientist_alice:4716 resolve;
}
server { listen 40062 ssl; location / { proxy_pass http://evosci_ui_40062; } }
""", encoding="utf-8")
            reporter = Reporter()
            original_public_dir = check_user.__globals__["OPENCLAW_PUBLIC_DIR"]
            check_user.__globals__["OPENCLAW_PUBLIC_DIR"] = root
            try:
                check_user("alice", user_dir, {}, {
                    "alice": {"product": "evoscientist", "status": "active", "public_id": "evo-public",
                               "port": 40062, "container_name": "evoscientist_alice"}
                }, {40062: {"status": "allocated", "user_id": "alice"}}, reporter)
            finally:
                check_user.__globals__["OPENCLAW_PUBLIC_DIR"] = original_public_dir
            codes = {issue.code for issue in reporter.issues}
            self.assertNotIn("nginx_conf_missing", codes)

    def test_legacy_evoscientist_layout_is_detected_without_product_metadata(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.configure_paths(root)
            user_dir = root / "users" / "legacy-evo"
            (user_dir / "workspace").mkdir(parents=True)
            (user_dir / "evoscientist-data").mkdir()
            reporter = Reporter()
            check_user("legacy-evo", user_dir, {}, {}, {}, reporter)
            codes = {issue.code for issue in reporter.issues}
            self.assertNotIn("container_name_mismatch", codes)
            self.assertNotIn("compose_missing_tenant_net", codes)

    def test_deleted_legacy_evo_name_skips_openclaw_checks(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.configure_paths(root)
            user_dir = root / "users" / "Evo_jwen_002"
            user_dir.mkdir(parents=True)
            reporter = Reporter()
            check_user(
                "Evo_jwen_002", user_dir,
                {"Evo_jwen_002": {"status": "deleted"}}, {}, {}, reporter,
            )
            codes = {issue.code for issue in reporter.issues}
            self.assertNotIn("container_name_mismatch", codes)
            self.assertNotIn("nginx_conf_missing", codes)
            self.assertNotIn("htpasswd_missing", codes)

    def test_orphan_user_dir_only_reports_warning(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.configure_paths(root)
            user_dir = root / "users" / "old-evo"
            user_dir.mkdir(parents=True)
            reporter = Reporter()
            check_user("old-evo", user_dir, {}, {}, {}, reporter)
            self.assertEqual(
                [(issue.level, issue.code) for issue in reporter.issues],
                [("WARN", "metadata_orphan_dir")],
            )

    def test_evo_ingress_443_does_not_override_external_port(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.configure_paths(root)
            user_dir = root / "users" / "alice"
            (user_dir / "workspace").mkdir(parents=True)
            (user_dir / "evoscientist-data").mkdir()
            ingress = root / "deleted" / "evoscientist" / "evo-public.nginx.conf"
            ingress.parent.mkdir(parents=True)
            ingress.write_text("server { listen 443 ssl; }", encoding="utf-8")
            reporter = Reporter()
            original = check_user.__globals__["OPENCLAW_PUBLIC_DIR"]
            check_user.__globals__["OPENCLAW_PUBLIC_DIR"] = root
            try:
                check_user("alice", user_dir, {}, {
                    "alice": {"product": "evoscientist", "status": "active", "public_id": "evo-public",
                               "port": 40087, "container_name": "evoscientist_alice"}
                }, {40087: {"status": "allocated", "user_id": "alice"}}, reporter)
            finally:
                check_user.__globals__["OPENCLAW_PUBLIC_DIR"] = original
            self.assertNotIn("metadata_port_mismatch", {issue.code for issue in reporter.issues})


if __name__ == "__main__":
    unittest.main()
