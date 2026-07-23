#!/usr/bin/env python3

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
MIGRATION = ROOT_DIR / "scripts" / "migrate_identity_instance_model.py"
CSV_IMPORT = ROOT_DIR / "scripts" / "import_users_csv_to_metadata_db.sh"
V1_SCHEMA = ROOT_DIR / "tests" / "fixtures" / "metadata_schema_v1.sql"


class IdentityInstanceMigrationTests(unittest.TestCase):
    def make_v1_database(self, root, rows):
        db_file = root / "manager.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(V1_SCHEMA.read_text(encoding="utf-8"))
            for user_id, status, container_name, port in rows:
                conn.execute(
                    """
                    INSERT INTO instances (
                        user_id, product, port, status, container_name, data_path
                    ) VALUES (?, 'openclaw', ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        port,
                        status,
                        container_name,
                        f"/data/docker/openclaw-public/users/{user_id}",
                    ),
                )
                instance_id = conn.execute(
                    "SELECT id FROM instances WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO instance_credentials (user_id, openclaw_token) VALUES (?, ?)",
                    (user_id, f"token-{user_id}"),
                )
                if port is not None:
                    conn.execute(
                        "INSERT INTO ports (port, user_id, status) VALUES (?, ?, ?)",
                        (port, user_id, "released" if status == "deleted" else "allocated"),
                    )
                conn.execute(
                    """
                    INSERT INTO operation_records (actor, action, user_id, status)
                    VALUES (?, 'create_instance', ?, 'success')
                    """,
                    (user_id, user_id),
                )
                self.assertGreater(instance_id, 0)
        return db_file

    def run_migration(self, db_file, *args):
        return subprocess.run(
            ["python3", str(MIGRATION), "--db", str(db_file), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_reports_plan_without_changing_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v1_database(
                Path(temp_dir), [("alice", "active", "openclaw_alice", 30021)]
            )

            result = self.run_migration(db_file, "--dry-run")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("users=1 instances=1 deleted=0", result.stdout)
            with sqlite3.connect(db_file) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(instances)")
                }
            self.assertIn("user_id", columns)
            self.assertNotIn("owner_user_id", columns)

    def test_casefold_username_collision_blocks_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v1_database(
                Path(temp_dir),
                [
                    ("Alice", "active", "openclaw_Alice", 30021),
                    ("alice", "stopped", "openclaw_alice", 30022),
                ],
            )

            result = self.run_migration(db_file, "--dry-run")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("normalized username collision", result.stderr)

    def test_apply_migrates_active_and_deleted_instances_with_relations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v1_database(
                Path(temp_dir),
                [
                    ("alice", "active", "openclaw_alice", 30021),
                    ("bob", "deleted", "openclaw_bob", 30022),
                ],
            )
            recycle_user = Path(temp_dir) / "deleted" / "bob_20260722_120000" / "user"
            recycle_user.mkdir(parents=True)
            (recycle_user / "docker-compose.yml").write_text(
                "services:\n  openclaw-bob:\n    container_name: openclaw_bob\n",
                encoding="utf-8",
            )
            recycle_nginx = recycle_user.parent / "nginx"
            recycle_nginx.mkdir()
            (recycle_nginx / "bob.conf").write_text(
                "server {\n    listen 30022;\n}\n", encoding="utf-8"
            )

            result = self.run_migration(
                db_file,
                "--apply",
                "--no-backup",
                "--public-dir",
                temp_dir,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                conn.row_factory = sqlite3.Row
                users = conn.execute(
                    "SELECT username, normalized_username FROM users ORDER BY username"
                ).fetchall()
                instances = conn.execute(
                    """
                    SELECT legacy_user_id, owner_user_id, public_id,
                           runtime_identifier, status, restore_state, data_path
                    FROM instances ORDER BY legacy_user_id
                    """
                ).fetchall()
                credential = conn.execute(
                    "SELECT instance_id, openclaw_token FROM instance_credentials "
                    "WHERE openclaw_token = 'token-bob'"
                ).fetchone()
                released_port = conn.execute(
                    "SELECT instance_id, status FROM ports WHERE port = 30022"
                ).fetchone()
                operation = conn.execute(
                    "SELECT actor_user_id, instance_id FROM operation_records "
                    "WHERE user_id = 'bob'"
                ).fetchone()
                endpoints = conn.execute(
                    "SELECT instance_id, endpoint_type, external_port, status "
                    "FROM instance_endpoints ORDER BY external_port"
                ).fetchall()

            self.assertEqual(
                [(row["username"], row["normalized_username"]) for row in users],
                [("alice", "alice"), ("bob", "bob")],
            )
            self.assertEqual(len(instances), 2)
            self.assertTrue(all(row["owner_user_id"] for row in instances))
            self.assertTrue(all(len(row["public_id"]) == 36 for row in instances))
            self.assertEqual(instances[1]["status"], "deleted")
            self.assertEqual(instances[1]["restore_state"], "restorable")
            self.assertEqual(
                instances[1]["data_path"],
                str(recycle_user),
            )
            self.assertTrue(credential["instance_id"])
            self.assertEqual(credential["openclaw_token"], "token-bob")
            self.assertEqual(released_port["status"], "released")
            self.assertTrue(operation["actor_user_id"])
            self.assertTrue(operation["instance_id"])
            self.assertEqual(
                [(row["endpoint_type"], row["external_port"], row["status"]) for row in endpoints],
                [("legacy_port", 30021, "active"), ("legacy_port", 30022, "inactive")],
            )

            repeated = self.run_migration(
                db_file,
                "--apply",
                "--no-backup",
                "--public-dir",
                temp_dir,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertIn("already at schema version 3", repeated.stdout)

    def test_deleted_instance_without_recycle_payload_is_marked_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = self.make_v1_database(
                Path(temp_dir), [("alice", "deleted", "openclaw_alice", 30021)]
            )

            result = self.run_migration(
                db_file,
                "--apply",
                "--no-backup",
                "--public-dir",
                temp_dir,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("restorable=0 incomplete=1", result.stdout)
            with sqlite3.connect(db_file) as conn:
                restore_state = conn.execute(
                    "SELECT restore_state FROM instances WHERE legacy_user_id = 'alice'"
                ).fetchone()[0]
            self.assertEqual(restore_state, "incomplete")

    def test_dry_run_rejects_orphan_relation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "active", "openclaw_alice", 30021)]
            )
            with sqlite3.connect(db_file) as conn:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute(
                    "INSERT INTO instance_credentials (user_id, openclaw_token) VALUES ('missing', 'token')"
                )

            result = self.run_migration(db_file, "--dry-run")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("orphan instance_credentials", result.stderr)

    def test_latest_incomplete_recycle_payload_is_not_replaced_by_older_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "deleted", "openclaw_alice", 30021)]
            )
            older = root / "deleted" / "alice_older"
            (older / "user").mkdir(parents=True)
            (older / "nginx").mkdir()
            (older / "user" / "docker-compose.yml").write_text("services: {}\n")
            (older / "nginx" / "alice.conf").write_text("server {}\n")
            newer = root / "deleted" / "alice_newer"
            (newer / "user").mkdir(parents=True)
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))

            result = self.run_migration(
                db_file, "--apply", "--no-backup", "--public-dir", temp_dir
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                row = conn.execute(
                    "SELECT restore_state, data_path FROM instances WHERE legacy_user_id = 'alice'"
                ).fetchone()
            self.assertEqual(row[0], "incomplete")
            self.assertNotEqual(row[1], str(older / "user"))

    def test_legacy_recycle_uses_users_csv_port_not_metadata_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "deleted", "openclaw_alice", None)]
            )
            recycle = root / "deleted" / "alice_legacy"
            recycle.mkdir(parents=True)
            (recycle / "docker-compose.yml").write_text("services: {}\n")
            (root / "users.csv").write_text(
                "user_id,port,created_at,status\nalice,30021,2026-07-22,deleted\n"
            )

            result = self.run_migration(
                db_file, "--apply", "--no-backup", "--public-dir", temp_dir
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                row = conn.execute(
                    "SELECT restore_state, data_path FROM instances WHERE legacy_user_id = 'alice'"
                ).fetchone()
            self.assertEqual(row, ("restorable", str(recycle)))

    def test_legacy_recycle_is_incomplete_when_only_metadata_has_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "deleted", "openclaw_alice", 30021)]
            )
            recycle = root / "deleted" / "alice_legacy"
            recycle.mkdir(parents=True)
            (recycle / "docker-compose.yml").write_text("services: {}\n")

            result = self.run_migration(
                db_file, "--apply", "--no-backup", "--public-dir", temp_dir
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                restore_state = conn.execute(
                    "SELECT restore_state FROM instances WHERE legacy_user_id = 'alice'"
                ).fetchone()[0]
            self.assertEqual(restore_state, "incomplete")

    def test_current_recycle_rejects_nginx_listen_syntax_restore_cannot_parse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "deleted", "openclaw_alice", 30021)]
            )
            recycle = root / "deleted" / "alice_current"
            (recycle / "user").mkdir(parents=True)
            (recycle / "nginx").mkdir()
            (recycle / "user" / "docker-compose.yml").write_text("services: {}\n")
            (recycle / "nginx" / "alice.conf").write_text(
                "server {\n    listen 30021 ssl http2;\n}\n"
            )

            result = self.run_migration(
                db_file, "--apply", "--no-backup", "--public-dir", temp_dir
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                restore_state = conn.execute(
                    "SELECT restore_state FROM instances WHERE legacy_user_id = 'alice'"
                ).fetchone()[0]
            self.assertEqual(restore_state, "incomplete")

    def test_current_recycle_rejects_multiline_listen_directive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "deleted", "openclaw_alice", 30021)]
            )
            recycle = root / "deleted" / "alice_current"
            (recycle / "user").mkdir(parents=True)
            (recycle / "nginx").mkdir()
            (recycle / "user" / "docker-compose.yml").write_text("services: {}\n")
            (recycle / "nginx" / "alice.conf").write_text(
                "server {\n    listen\n      30021 ssl;\n}\n"
            )

            result = self.run_migration(
                db_file, "--apply", "--no-backup", "--public-dir", temp_dir
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                restore_state = conn.execute(
                    "SELECT restore_state FROM instances WHERE legacy_user_id = 'alice'"
                ).fetchone()[0]
            self.assertEqual(restore_state, "incomplete")

    def test_apply_creates_readable_v1_backup_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "active", "openclaw_alice", 30021)]
            )

            result = self.run_migration(db_file, "--apply", "--public-dir", temp_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            backups = list(root.glob("manager.db.pre-v2-*.bak"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                legacy_user = conn.execute(
                    "SELECT user_id FROM instances"
                ).fetchone()[0]
            self.assertEqual(version, 1)
            self.assertEqual(legacy_user, "alice")

    def test_csv_import_marks_deleted_current_layout_as_restorable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            public_dir = root / "public"
            recycle = public_dir / "deleted" / "alice_20260722_120000"
            (recycle / "user").mkdir(parents=True)
            (recycle / "nginx").mkdir()
            (recycle / "user" / "docker-compose.yml").write_text("services: {}\n")
            (recycle / "nginx" / "alice.conf").write_text(
                "server {\n    listen 30021;\n}\n"
            )
            users_csv = public_dir / "users.csv"
            users_csv.write_text(
                "user_id,port,created_at,status\nalice,30021,2026-07-22,deleted\n"
            )
            db_file = public_dir / "manager.db"
            env = os.environ.copy()
            env.update(
                {
                    "OPENCLAW_PUBLIC_DIR": str(public_dir),
                    "USERS_CSV": str(users_csv),
                    "METADATA_DB_FILE": str(db_file),
                    "NGINX_USERS_CONF_DIR": str(root / "nginx"),
                }
            )

            result = subprocess.run(
                ["bash", str(CSV_IMPORT)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(db_file) as conn:
                row = conn.execute(
                    "SELECT restore_state, data_path FROM instances WHERE legacy_user_id = 'alice'"
                ).fetchone()
            self.assertEqual(row, ("restorable", str(recycle / "user")))

    def test_csv_import_refuses_v1_database_without_mutating_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_file = self.make_v1_database(
                root, [("alice", "active", "openclaw_alice", 30021)]
            )
            users_csv = root / "users.csv"
            users_csv.write_text(
                "user_id,port,created_at,status\nalice,30021,2026-07-22,active\n"
            )
            env = os.environ.copy()
            env.update(
                {
                    "OPENCLAW_PUBLIC_DIR": str(root),
                    "USERS_CSV": str(users_csv),
                    "METADATA_DB_FILE": str(db_file),
                }
            )

            result = subprocess.run(
                ["bash", str(CSV_IMPORT)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Metadata schema v1 requires", result.stderr)
            with sqlite3.connect(db_file) as conn:
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                users_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
                ).fetchone()
            self.assertEqual(version, 1)
            self.assertIsNone(users_table)


if __name__ == "__main__":
    unittest.main()
