#!/usr/bin/env python3

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
STORE_FILE = ROOT_DIR / "services" / "manager-web" / "metadata_store.py"
SCHEMA_FILE = ROOT_DIR / "db" / "schema.sql"


def load_store():
    spec = importlib.util.spec_from_file_location("identity_metadata_store", STORE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdentityInstanceMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "manager.db"
        self.store = load_store()
        self.store.initialize(self.db_file, SCHEMA_FILE)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_one_user_can_own_multiple_product_instances(self):
        user = self.store.create_user("Alice", db_file=self.db_file)

        openclaw = self.store.create_instance(
            owner_public_id=user["public_id"],
            product="openclaw",
            instance_name="Research assistant",
            runtime_identifier="openclaw_alice_primary",
            data_path="/data/instances/openclaw-a",
            db_file=self.db_file,
        )
        hermes = self.store.create_instance(
            owner_public_id=user["public_id"],
            product="hermes",
            instance_name="Lab Hermes",
            runtime_identifier="hermes_alice_lab",
            data_path="/data/instances/hermes-a",
            db_file=self.db_file,
        )

        instances = self.store.list_instances_for_user(
            user["public_id"], db_file=self.db_file
        )
        self.assertEqual(
            [(row["public_id"], row["product"]) for row in instances],
            [
                (openclaw["public_id"], "openclaw"),
                (hermes["public_id"], "hermes"),
            ],
        )
        self.assertTrue(all(row["owner_user_id"] == user["id"] for row in instances))

    def test_casefolded_username_collision_is_rejected(self):
        self.store.create_user("Alice", db_file=self.db_file)

        with self.assertRaisesRegex(ValueError, "normalized username collision"):
            self.store.create_user("alice", db_file=self.db_file)

    def test_runtime_identifier_is_globally_unique(self):
        alice = self.store.create_user("alice", db_file=self.db_file)
        bob = self.store.create_user("bob", db_file=self.db_file)
        self.store.create_instance(
            owner_public_id=alice["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_runtime_1",
            db_file=self.db_file,
        )

        with self.assertRaisesRegex(ValueError, "runtime identifier already exists"):
            self.store.create_instance(
                owner_public_id=bob["public_id"],
                product="openclaw",
                instance_name="Primary",
                runtime_identifier="openclaw_runtime_1",
                db_file=self.db_file,
            )

    def test_data_path_is_globally_unique(self):
        alice = self.store.create_user("alice", db_file=self.db_file)
        bob = self.store.create_user("bob", db_file=self.db_file)
        self.store.create_instance(
            owner_public_id=alice["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            data_path="/data/instances/shared",
            db_file=self.db_file,
        )

        with self.assertRaisesRegex(ValueError, "data path already exists"):
            self.store.create_instance(
                owner_public_id=bob["public_id"],
                product="openclaw",
                instance_name="Primary",
                runtime_identifier="openclaw_bob",
                data_path="/data/instances/shared",
                db_file=self.db_file,
            )

    def test_legacy_port_endpoint_tracks_instance_status(self):
        with self.store.connect(self.db_file) as conn:
            self.store.upsert_instance(user_id="alice", port=30021, conn=conn)
            self.store.upsert_instance(
                user_id="alice", port=30021, status="deleted", conn=conn
            )
            endpoint = conn.execute(
                "SELECT external_port, status FROM instance_endpoints"
            ).fetchone()

        self.assertEqual(tuple(endpoint), (30021, "inactive"))

    def test_partial_update_preserves_deleted_restore_state(self):
        with self.store.connect(self.db_file) as conn:
            self.store.upsert_instance(
                user_id="alice",
                status="deleted",
                restore_state="restorable",
                conn=conn,
            )
            self.store.upsert_instance(user_id="alice", status="deleted", conn=conn)
            restore_state = conn.execute(
                "SELECT restore_state FROM instances WHERE legacy_user_id = 'alice'"
            ).fetchone()[0]

        self.assertEqual(restore_state, "restorable")

    def test_legacy_identity_cannot_be_reassigned_by_username(self):
        alice = self.store.create_user("alice", db_file=self.db_file)
        self.store.create_user("bob", db_file=self.db_file)
        with self.store.connect(self.db_file) as conn:
            conn.execute(
                """
                INSERT INTO user_identities (user_id, provider, subject)
                VALUES (?, 'legacy', 'bob')
                """,
                (alice["id"],),
            )

        with self.assertRaisesRegex(ValueError, "legacy identity owner conflict"):
            with self.store.connect(self.db_file) as conn:
                self.store.upsert_instance(user_id="bob", conn=conn)


if __name__ == "__main__":
    unittest.main()
