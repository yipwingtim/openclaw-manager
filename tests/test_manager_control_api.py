#!/usr/bin/env python3

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT_DIR / "services" / "manager-control"
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
SCHEMA_FILE = ROOT_DIR / "db" / "schema.sql"


def load_control_app():
    flask_stub = types.ModuleType("flask")

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def get_json(self):
            return self.payload

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def get(self, *args, **kwargs):
            return lambda func: func

        put = get
        delete = get
        post = get
        patch = get

        def run(self, *args, **kwargs):
            pass

    flask_stub.Flask = FakeFlask
    flask_stub.g = types.SimpleNamespace()
    flask_stub.jsonify = lambda payload: FakeResponse(payload)
    flask_stub.request = types.SimpleNamespace(
        headers={},
        args={},
        get_json=lambda **kwargs: {},
    )
    werkzeug_security_stub = types.ModuleType("werkzeug.security")
    werkzeug_security_stub.check_password_hash = (
        lambda stored, provided: stored == f"hash:{provided}"
    )

    sys.path.insert(0, str(MANAGER_WEB_DIR))
    spec = importlib.util.spec_from_file_location(
        "manager_control_app", CONTROL_DIR / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    previous_flask = sys.modules.get("flask")
    previous_security = sys.modules.get("werkzeug.security")
    sys.modules["flask"] = flask_stub
    sys.modules["werkzeug.security"] = werkzeug_security_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_flask is None:
            del sys.modules["flask"]
        else:
            sys.modules["flask"] = previous_flask
        if previous_security is None:
            del sys.modules["werkzeug.security"]
        else:
            sys.modules["werkzeug.security"] = previous_security
    return module


def response_parts(result):
    if isinstance(result, tuple):
        return result
    return result, 200


class ManagerControlApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_control_app()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "manager.db"
        self.control.DB_FILE = self.db_file
        self.control.metadata_store.initialize(self.db_file, SCHEMA_FILE)
        self.user = self.control.metadata_store.create_user(
            "alice", db_file=self.db_file
        )
        self.env = patch.dict(
            os.environ,
            {
                "MANAGER_CONTROL_USER_WEB_TOKEN": "user-token",
                "MANAGER_CONTROL_ADMIN_WEB_TOKEN": "admin-token",
                "MANAGER_CONTROL_EXECUTOR_TOKEN": "executor-token",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_health_reports_schema_readiness_without_authentication(self):
        response, status = response_parts(self.control.health())

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": True,
                "schema_version": 5,
                "service_tokens_configured": True,
            },
        )

    def test_web_resolves_session_and_admin_service_rejects_user_role(self):
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.control.metadata_store.create_session(
            "session-hash",
            self.user["id"],
            "local",
            "csrf-token",
            expires_at,
            db_file=self.db_file,
        )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer user-token"},
        ), patch.object(
            self.control.request,
            "args",
            {"token_hash": "session-hash", "provider": "local"},
        ):
            resolved, resolved_status = response_parts(
                self.control.resolve_auth_session()
            )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ), patch.object(
            self.control.request,
            "args",
            {"token_hash": "session-hash", "provider": "local"},
        ):
            denied, denied_status = response_parts(
                self.control.resolve_auth_session()
            )

        self.assertEqual(resolved_status, 200)
        self.assertEqual(resolved.get_json()["user"]["username"], "alice")
        self.assertEqual(denied_status, 403)
        self.assertEqual(denied.get_json(), {"error": "administrator role is required"})

    def test_local_login_creates_session_without_exposing_password_hash(self):
        self.control.metadata_store.upsert_identity(
            self.user["id"], "local", "alice", "alice", db_file=self.db_file
        )
        self.control.metadata_store.set_local_credential(
            self.user["id"],
            "hash:correct-password",
            db_file=self.db_file,
        )
        payload = {
            "username": "alice",
            "password": "correct-password",
            "token_hash": "new-session-hash",
            "csrf_token": "csrf-token",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer user-token"},
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.local_auth_login())

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["user"]["username"], "alice")
        self.assertNotIn("password_hash", response.get_json()["user"])
        self.assertIsNotNone(
            self.control.metadata_store.get_session(
                "new-session-hash", db_file=self.db_file
            )
        )

    def test_executor_resolves_runtime_instance_only_after_actor_authorization(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            data_path="/srv/openclaw/alice",
            db_file=self.db_file,
        )
        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer executor-token"},
        ), patch.object(
            self.control.request,
            "args",
            {"actor_user_public_id": self.user["public_id"]},
        ):
            response, status = response_parts(
                self.control.executor_instance(instance["public_id"])
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json()["instance"]["runtime_identifier"],
            "openclaw_alice",
        )
        self.assertEqual(
            response.get_json()["instance"]["data_path"],
            "/srv/openclaw/alice",
        )

    def test_admin_lists_instances_from_control_database(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ):
            response, status = response_parts(self.control.admin_instances())

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json()["instances"][0]["public_id"],
            instance["public_id"],
        )

    def test_admin_lists_platform_users_without_sensitive_fields(self):
        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ):
            response, status = response_parts(self.control.admin_users())

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json()["users"],
            [{
                "public_id": self.user["public_id"],
                "username": "alice",
                "display_name": None,
                "role": "user",
                "status": "active",
            }],
        )

    def test_admin_reads_metadata_summary_without_sensitive_fields(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        with self.control.metadata_store.connect(self.db_file) as conn:
            conn.execute(
                """
                UPDATE instances
                SET legacy_user_id = 'alice', port = 41001,
                    openclaw_version = '2026.7.1', basic_auth_enabled = 1
                WHERE id = ?
                """,
                (instance["id"],),
            )
        with self.control.metadata_store.connect(self.db_file) as conn:
            self.control.metadata_store.record_operation(
                action="instance.start",
                status="success",
                actor_user_id=self.user["id"],
                instance_id=instance["id"],
                source_service="manager-admin-web",
                message="started",
                conn=conn,
            )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ):
            response, status = response_parts(self.control.admin_metadata())

        payload = response.get_json()
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["instances"], 1)
        self.assertEqual(payload["counts"]["operation_records"], 1)
        self.assertEqual(
            set(payload["counts"]),
            {"instances", "ports", "instance_credentials", "operation_records"},
        )
        self.assertEqual(payload["instances"][0]["public_id"], instance["public_id"])
        self.assertEqual(payload["instances"][0]["legacy_user_id"], "alice")
        self.assertEqual(payload["instances"][0]["version"], "2026.7.1")
        self.assertNotIn("runtime_identifier", payload["instances"][0])
        self.assertNotIn("data_path", payload["instances"][0])
        self.assertEqual(
            payload["operations"][0]["instance_public_id"], instance["public_id"]
        )

    def test_admin_creates_provisioning_instance_without_persisting_password(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        secret_dir = Path(self.temp_dir.name) / "secrets"
        payload = {
            "request_id": "create-1",
            "actor_user_public_id": self.user["public_id"],
            "owner_user_public_id": self.user["public_id"],
            "legacy_user_id": "alice-instance",
            "instance_name": "Alice instance",
            "product": "openclaw",
            "basic_auth_enabled": True,
            "basic_auth_password": "do-not-store-this",
        }
        with patch.object(self.control, "PROVISIONING_SECRET_DIR", secret_dir), patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.create_admin_instance())

        self.assertEqual(status, 202)
        body = response.get_json()
        self.assertEqual(body["instance"]["status"], "provisioning")
        self.assertNotIn("basic_auth_password", body["job"]["params"])
        secret_path = Path(body["job"]["params"]["secret_path"])
        self.assertEqual(secret_path.read_text(encoding="utf-8"), "do-not-store-this")
        self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"do-not-store-this", self.db_file.read_bytes())

        with patch.object(self.control, "PROVISIONING_SECRET_DIR", secret_dir), patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            repeated, repeated_status = response_parts(
                self.control.create_admin_instance()
            )
        self.assertEqual(repeated_status, 202)
        self.assertEqual(repeated.get_json()["job"]["request_id"], "create-1")
        self.assertEqual(len(list(secret_dir.iterdir())), 1)

    def test_admin_batch_creates_provisioning_instances_without_persisting_passwords(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        owner = self.control.metadata_store.create_user("bob", db_file=self.db_file)
        secret_dir = Path(self.temp_dir.name) / "secrets"
        payload = {
            "request_id": "batch-create-1",
            "actor_user_public_id": self.user["public_id"],
            "instances": [
                {
                    "owner_user_public_id": self.user["public_id"],
                    "legacy_user_id": "alice-one",
                    "instance_name": "Alice One",
                    "product": "openclaw",
                    "basic_auth_enabled": True,
                    "basic_auth_password": "alice-secret",
                },
                {
                    "owner_user_public_id": owner["public_id"],
                    "legacy_user_id": "bob-one",
                    "instance_name": "Bob One",
                    "product": "openclaw",
                    "basic_auth_enabled": False,
                    "basic_auth_password": "bob-secret",
                },
            ],
        }
        with patch.object(self.control, "PROVISIONING_SECRET_DIR", secret_dir), patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.create_instance_batch())
            repeated, repeated_status = response_parts(self.control.create_instance_batch())
            queried, queried_status = response_parts(
                self.control.get_instance_batch("batch-create-1")
            )

        self.assertEqual(status, 202)
        self.assertEqual(repeated_status, 202)
        self.assertEqual(queried_status, 200)
        body = response.get_json()
        self.assertEqual(body["parent"]["action"], "batch.create")
        self.assertEqual(len(body["children"]), 2)
        self.assertTrue(all(child["action"] == "instance.create" for child in body["children"]))
        self.assertTrue(all(child["params"] == {} for child in body["children"]))
        self.assertEqual(len(repeated.get_json()["children"]), 2)
        self.assertTrue(all(
            child["params"] == {} for child in queried.get_json()["children"]
        ))
        self.assertEqual(
            [item["status"] for item in self.control.metadata_store.list_instances(db_file=self.db_file)],
            ["provisioning", "provisioning"],
        )
        self.assertNotIn(b"alice-secret", self.db_file.read_bytes())
        self.assertNotIn(b"bob-secret", self.db_file.read_bytes())
        self.assertEqual(
            {path.read_text(encoding="utf-8") for path in secret_dir.iterdir()},
            {"alice-secret", "bob-secret"},
        )

    def test_admin_batch_rejects_invalid_owner_without_partial_resources(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        secret_dir = Path(self.temp_dir.name) / "secrets"
        payload = {
            "request_id": "batch-create-invalid-owner",
            "actor_user_public_id": self.user["public_id"],
            "instances": [{
                "owner_user_public_id": "missing-owner",
                "legacy_user_id": "alice-one",
                "instance_name": "Alice One",
                "product": "openclaw",
                "basic_auth_enabled": True,
                "basic_auth_password": "secret",
            }],
        }
        with patch.object(self.control, "PROVISIONING_SECRET_DIR", secret_dir), patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.create_instance_batch())

        self.assertEqual(status, 409)
        self.assertIn("active owner user not found", response.get_json()["error"])
        self.assertEqual(self.control.metadata_store.list_instances(db_file=self.db_file), [])
        self.assertIsNone(self.control.metadata_store.get_execution_job(
            "batch-create-invalid-owner", db_file=self.db_file
        ))
        self.assertEqual(list(secret_dir.iterdir()), [])

    def test_executor_finishes_created_instance_with_structured_result(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Alice instance", legacy_user_id="alice-instance",
            runtime_identifier="openclaw_alice-instance", status="provisioning",
            db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="create-1", actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"], action="instance.create",
            params={"secret_path": "/tmp/opaque"}, db_file=self.db_file,
        )
        self.control.metadata_store.update_execution_job(
            "create-1", "running", db_file=self.db_file
        )
        result = {
            "port": 41001,
            "version": "2026.6.6",
            "access_url": "https://example.test:41001",
            "admin_url": "https://example.test:41001/admin/",
            "basic_auth_password_ref": "nginx-auth:/etc/nginx/auth/users/alice-instance/.htpasswd",
            "openclaw_token": "runtime-token",
        }
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer executor-token"}
        ), patch.object(
            self.control.request, "get_json",
            return_value={"status": "succeeded", "output": "instance created", "result": result},
        ):
            response, status = response_parts(
                self.control.update_execution_job("create-1")
            )

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["job"]["output"], "instance created")
        stored = self.control.metadata_store.get_instance_by_public_id(
            instance["public_id"], db_file=self.db_file
        )
        self.assertEqual((stored["status"], stored["port"]), ("active", 41001))
        with self.control.metadata_store.connect(self.db_file) as conn:
            credentials = self.control.metadata_store.get_credentials(
                "alice-instance", conn=conn
            )
        self.assertEqual(credentials["openclaw_token"], "runtime-token")

    def test_health_does_not_create_a_missing_database(self):
        missing = Path(self.temp_dir.name) / "missing.db"
        self.control.DB_FILE = missing

        response, status = response_parts(self.control.health())

        self.assertEqual(status, 503)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "schema_version": None,
                "service_tokens_configured": True,
            },
        )
        self.assertFalse(missing.exists())

    def test_duplicate_or_incomplete_service_tokens_fail_closed(self):
        with patch.dict(
            os.environ,
            {
                "MANAGER_CONTROL_USER_WEB_TOKEN": "same-token",
                "MANAGER_CONTROL_ADMIN_WEB_TOKEN": "same-token",
                "MANAGER_CONTROL_EXECUTOR_TOKEN": "",
            },
            clear=False,
        ):
            health, health_status = response_parts(self.control.health())
            with patch.object(
                self.control.request,
                "headers",
                {"Authorization": "Bearer same-token"},
            ):
                protected, protected_status = response_parts(
                    self.control.user_instances(self.user["public_id"])
                )

        self.assertEqual(health_status, 503)
        self.assertFalse(health.get_json()["service_tokens_configured"])
        self.assertEqual(protected_status, 503)
        self.assertEqual(
            protected.get_json(),
            {"error": "manager-control service tokens are invalid"},
        )

    def test_instance_list_requires_a_valid_service_token(self):
        with patch.object(self.control.request, "headers", {}):
            missing, missing_status = response_parts(
                self.control.user_instances(self.user["public_id"])
            )
        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer wrong-token"},
        ):
            invalid, invalid_status = response_parts(
                self.control.user_instances(self.user["public_id"])
            )
        with patch.object(
            self.control.request,
            "headers",
            {
                "Authorization": "Bearer user-token",
                "X-Actor-User-Public-Id": self.user["public_id"],
            },
        ):
            valid, valid_status = response_parts(
                self.control.user_instances(self.user["public_id"])
            )

        self.assertEqual(missing_status, 401)
        self.assertEqual(invalid_status, 401)
        self.assertEqual(valid_status, 200)
        self.assertEqual(valid.get_json(), {"instances": []})

    def test_instance_list_exposes_only_portal_metadata(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            data_path="/data/docker/openclaw-public/users/alice",
            db_file=self.db_file,
        )

        with patch.object(
            self.control.request,
            "headers",
            {
                "Authorization": "Bearer user-token",
                "X-Actor-User-Public-Id": self.user["public_id"],
            },
        ):
            response, status = response_parts(
                self.control.user_instances(self.user["public_id"])
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json(),
            {
                "instances": [
                    {
                        "public_id": instance["public_id"],
                        "legacy_user_id": None,
                        "product": "openclaw",
                        "instance_name": "Primary",
                        "status": "active",
                        "version": None,
                        "access_url": None,
                        "access_role": "owner",
                        "created_at": instance["created_at"],
                        "updated_at": instance["updated_at"],
                    }
                ]
            },
        )

    def test_user_service_cannot_list_another_users_instances(self):
        other = self.control.metadata_store.create_user(
            "bob", db_file=self.db_file
        )

        with patch.object(
            self.control.request,
            "headers",
            {
                "Authorization": "Bearer user-token",
                "X-Actor-User-Public-Id": self.user["public_id"],
            },
        ):
            response, status = response_parts(
                self.control.user_instances(other["public_id"])
            )

        self.assertEqual(status, 403)
        self.assertEqual(
            response.get_json(),
            {"error": "user service cannot impersonate another user"},
        )

    def test_disabled_user_cannot_list_or_read_instances(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        with self.control.metadata_store.connect(self.db_file) as conn:
            conn.execute(
                "UPDATE users SET status = 'disabled' WHERE id = ?",
                (self.user["id"],),
            )
        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": self.user["public_id"],
        }

        with patch.object(self.control.request, "headers", headers):
            listed, listed_status = response_parts(
                self.control.user_instances(self.user["public_id"])
            )
            detail, detail_status = response_parts(
                self.control.get_instance(instance["public_id"])
            )

        self.assertEqual(listed_status, 403)
        self.assertEqual(
            listed.get_json(),
            {"error": "active actor user is required"},
        )
        self.assertEqual(detail_status, 404)
        self.assertEqual(detail.get_json(), {"error": "instance not found"})

    def test_viewer_reads_public_instance_metadata_and_outsider_cannot(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            data_path="/data/private/alice",
            db_file=self.db_file,
        )
        viewer = self.control.metadata_store.create_user(
            "viewer", db_file=self.db_file
        )
        outsider = self.control.metadata_store.create_user(
            "outsider", db_file=self.db_file
        )
        self.control.metadata_store.add_instance_member(
            instance["public_id"],
            viewer["public_id"],
            "viewer",
            db_file=self.db_file,
        )
        with self.control.metadata_store.connect(self.db_file) as conn:
            conn.execute(
                "UPDATE instances SET legacy_user_id = 'alice' WHERE id = ?",
                (instance["id"],),
            )

        with patch.object(
            self.control.request,
            "headers",
            {
                "Authorization": "Bearer user-token",
                "X-Actor-User-Public-Id": viewer["public_id"],
            },
        ):
            visible, visible_status = response_parts(
                self.control.get_instance(instance["public_id"])
            )
        with patch.object(
            self.control.request,
            "headers",
            {
                "Authorization": "Bearer user-token",
                "X-Actor-User-Public-Id": outsider["public_id"],
            },
        ):
            hidden, hidden_status = response_parts(
                self.control.get_instance(instance["public_id"])
            )

        self.assertEqual(visible_status, 200)
        self.assertEqual(visible.get_json()["instance"]["access_role"], "viewer")
        self.assertEqual(
            visible.get_json()["instance"]["legacy_user_id"],
            "alice",
        )
        self.assertNotIn("runtime_identifier", visible.get_json()["instance"])
        self.assertNotIn("data_path", visible.get_json()["instance"])
        self.assertEqual(hidden_status, 404)
        self.assertEqual(hidden.get_json(), {"error": "instance not found"})

    def test_owner_adds_member_by_platform_username(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        member = self.control.metadata_store.create_user(
            "Bob", db_file=self.db_file
        )
        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": self.user["public_id"],
        }

        with patch.object(self.control.request, "headers", headers):
            with patch.object(
                self.control.request,
                "get_json",
                return_value={"username": "bob", "role": "operator"},
            ):
                response, status = response_parts(
                    self.control.add_instance_member_by_username(
                        instance["public_id"]
                    )
                )

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json()["member"],
            {
                "user_public_id": member["public_id"],
                "username": "Bob",
                "display_name": None,
                "role": "operator",
            },
        )

    def test_owner_can_add_and_list_instance_manager(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        member = self.control.metadata_store.create_user(
            "bob", db_file=self.db_file
        )
        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": self.user["public_id"],
        }

        with patch.object(self.control.request, "headers", headers):
            with patch.object(
                self.control.request,
                "get_json",
                return_value={"role": "manager"},
            ):
                added, added_status = response_parts(
                    self.control.set_instance_member(
                        instance["public_id"],
                        member["public_id"],
                    )
                )
            listed, listed_status = response_parts(
                self.control.instance_members(instance["public_id"])
            )

        self.assertEqual(added_status, 200)
        self.assertEqual(added.get_json()["member"]["role"], "manager")
        self.assertEqual(listed_status, 200)
        self.assertEqual(
            listed.get_json(),
            {
                "members": [
                    {
                        "user_public_id": member["public_id"],
                        "username": "bob",
                        "display_name": None,
                        "role": "manager",
                    }
                ]
            },
        )

    def test_manager_can_manage_operators_and_viewers_but_not_managers(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        manager = self.control.metadata_store.create_user(
            "manager", db_file=self.db_file
        )
        operator = self.control.metadata_store.create_user(
            "operator", db_file=self.db_file
        )
        viewer = self.control.metadata_store.create_user(
            "viewer", db_file=self.db_file
        )
        self.control.metadata_store.add_instance_member(
            instance["public_id"],
            manager["public_id"],
            "manager",
            db_file=self.db_file,
        )
        self.control.metadata_store.add_instance_member(
            instance["public_id"],
            operator["public_id"],
            "operator",
            db_file=self.db_file,
        )

        headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": manager["public_id"],
        }
        with patch.object(self.control.request, "headers", headers):
            with patch.object(
                self.control.request,
                "get_json",
                return_value={"role": "viewer"},
            ):
                allowed, allowed_status = response_parts(
                    self.control.set_instance_member(
                        instance["public_id"],
                        viewer["public_id"],
                    )
                )
            with patch.object(
                self.control.request,
                "get_json",
                return_value={"role": "manager"},
            ):
                forbidden, forbidden_status = response_parts(
                    self.control.set_instance_member(
                        instance["public_id"],
                        operator["public_id"],
                    )
                )

        self.assertEqual(allowed_status, 200)
        self.assertEqual(allowed.get_json()["member"]["role"], "viewer")
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(
            forbidden.get_json(),
            {"error": "manager cannot manage manager members"},
        )

    def test_owner_can_remove_member_and_admin_can_read_audit_event(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        member = self.control.metadata_store.create_user(
            "bob", db_file=self.db_file
        )
        self.control.metadata_store.add_instance_member(
            instance["public_id"],
            member["public_id"],
            "operator",
            db_file=self.db_file,
        )
        user_headers = {
            "Authorization": "Bearer user-token",
            "X-Actor-User-Public-Id": self.user["public_id"],
        }

        with patch.object(self.control.request, "headers", user_headers):
            removed, removed_status = response_parts(
                self.control.remove_instance_member(
                    instance["public_id"],
                    member["public_id"],
                )
            )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ):
            audited, audited_status = response_parts(
                self.control.operation_events()
            )

        self.assertEqual(removed_status, 204)
        self.assertEqual(removed, "")
        self.assertEqual(audited_status, 200)
        event = audited.get_json()["operations"][0]
        self.assertEqual(event["actor_user_public_id"], self.user["public_id"])
        self.assertEqual(event["instance_public_id"], instance["public_id"])
        self.assertEqual(event["source_service"], "manager-user-web")
        self.assertEqual(event["action"], "instance_member.remove")
        self.assertEqual(event["status"], "success")

    def test_admin_creates_idempotent_execution_job_and_user_service_cannot(self):
        self.control.metadata_store.set_user_role(
            self.user["id"],
            "admin",
            db_file=self.db_file,
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        payload = {
            "request_id": "request-1",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.restart",
            "params": {"reason": "manual"},
        }

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ):
            with patch.object(
                self.control.request,
                "get_json",
                return_value=payload,
            ):
                created, created_status = response_parts(
                    self.control.create_execution_job()
                )
                repeated, repeated_status = response_parts(
                    self.control.create_execution_job()
                )
            with patch.object(
                self.control.request,
                "get_json",
                return_value={
                    **payload,
                    "action": "instance.stop",
                    "params": {},
                },
            ):
                conflict, conflict_status = response_parts(
                    self.control.create_execution_job()
                )
            with patch.object(
                self.control.request,
                "get_json",
                return_value={
                    **payload,
                    "request_id": "request-unsupported",
                    "action": "shell.run",
                    "params": {"command": "id"},
                },
            ):
                unsupported, unsupported_status = response_parts(
                    self.control.create_execution_job()
                )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer user-token"},
        ):
            with patch.object(
                self.control.request,
                "get_json",
                return_value={**payload, "request_id": "request-2"},
            ):
                forbidden, forbidden_status = response_parts(
                    self.control.create_execution_job()
                )

        expected = {
            "request_id": "request-1",
            "parent_request_id": None,
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.restart",
            "params": {"reason": "manual"},
            "status": "queued",
            "current_step": None,
            "error_summary": None,
            "output": None,
        }
        self.assertEqual(created_status, 200)
        self.assertEqual(created.get_json()["job"], expected)
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated.get_json()["job"], expected)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(
            conflict.get_json(),
            {"error": "request_id already used for another operation"},
        )
        self.assertEqual(unsupported_status, 400)
        self.assertEqual(
            unsupported.get_json(),
            {"error": "unsupported execution action"},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(
            forbidden.get_json(),
            {"error": "service is not allowed"},
        )

    def test_control_rejects_execution_action_not_supported_by_product(self):
        self.control.metadata_store.set_user_role(
            self.user["id"],
            "admin",
            db_file=self.db_file,
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="unknown-product",
            instance_name="Unsupported",
            runtime_identifier="unknown_alice",
            db_file=self.db_file,
        )
        payload = {
            "request_id": "request-unsupported-product",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.restart",
            "params": {},
        }

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.create_execution_job())

        self.assertEqual(status, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "instance product does not support restart"},
        )
        self.assertEqual(
            self.control.metadata_store.list_execution_jobs(
                limit=10,
                db_file=self.db_file,
            ),
            [],
        )

    def test_generic_execution_job_endpoint_rejects_instance_create(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        payload = {
            "request_id": "create-bypass",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.create",
            "params": {"secret_path": "/etc/shadow"},
        }

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.create_execution_job())

        self.assertEqual(status, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "unsupported execution action"},
        )
        self.assertEqual(
            self.control.metadata_store.list_execution_jobs(
                limit=10, db_file=self.db_file
            ),
            [],
        )

    def test_basic_auth_job_requires_boolean_and_updates_metadata_on_success(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        payload = {
            "request_id": "basic-auth-1",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.set_basic_auth",
            "params": {"enabled": False},
        }
        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ), patch.object(self.control.request, "get_json", return_value=payload):
            created, created_status = response_parts(
                self.control.create_execution_job()
            )
        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer executor-token"},
        ):
            claimed, claimed_status = response_parts(
                self.control.claim_execution_job()
            )
        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer executor-token"},
        ), patch.object(
            self.control.request,
            "get_json",
            return_value={"status": "succeeded", "output": "disabled"},
        ):
            updated, updated_status = response_parts(
                self.control.update_execution_job("basic-auth-1")
            )

        self.assertEqual(created_status, 200)
        self.assertEqual(created.get_json()["job"]["params"], {"enabled": False})
        self.assertEqual(claimed_status, 200)
        self.assertEqual(updated_status, 200)
        stored = self.control.metadata_store.get_instance_by_public_id(
            instance["public_id"], db_file=self.db_file
        )
        self.assertFalse(stored["basic_auth_enabled"])
        operation = self.control.metadata_store.list_operation_events(
            1, db_file=self.db_file
        )[0]
        self.assertEqual(operation["request_id"], "basic-auth-1")
        self.assertEqual(operation["action"], "instance.set_basic_auth")
        self.assertEqual(operation["status"], "success")
        self.assertEqual(operation["message"], "Basic Auth disabled")

        payload["request_id"] = "basic-auth-invalid"
        payload["params"] = {"enabled": "false"}
        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ), patch.object(self.control.request, "get_json", return_value=payload):
            invalid, invalid_status = response_parts(
                self.control.create_execution_job()
            )
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid.get_json(), {"error": "enabled must be a boolean"})

    def test_version_job_validates_params_and_updates_metadata_on_success(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        payload = {
            "request_id": "version-1",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.update_version",
            "params": {"version": "2026.7.28", "restore_model_provider": False},
        }
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            created, created_status = response_parts(self.control.create_execution_job())
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer executor-token"}
        ):
            self.control.claim_execution_job()
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer executor-token"}
        ), patch.object(
            self.control.request,
            "get_json",
            return_value={"status": "succeeded", "output": "updated"},
        ):
            updated, updated_status = response_parts(
                self.control.update_execution_job("version-1")
            )

        self.assertEqual(created_status, 200)
        self.assertEqual(updated_status, 200)
        stored = self.control.metadata_store.get_instance_by_public_id(
            instance["public_id"], db_file=self.db_file
        )
        self.assertEqual(stored["openclaw_version"], "2026.7.28")
        operation = self.control.metadata_store.list_operation_events(
            1, db_file=self.db_file
        )[0]
        self.assertEqual(operation["action"], "instance.update_version")
        self.assertEqual(operation["message"], "version=2026.7.28")

        payload["request_id"] = "version-invalid"
        payload["params"] = {"version": "bad/version", "restore_model_provider": False}
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            invalid, invalid_status = response_parts(self.control.create_execution_job())
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid.get_json(), {"error": "invalid version"})

    def test_skill_job_rejects_unconfigured_preset(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice", db_file=self.db_file,
        )
        payload = {
            "request_id": "skill-invalid", "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"], "action": "instance.install_skill",
            "params": {"skill_id": "untrusted"},
        }
        with patch.object(self.control.request, "headers", {"Authorization": "Bearer admin-token"}), patch.object(
            self.control.request, "get_json", return_value=payload
        ), patch.dict(os.environ, {"MANAGER_SKILL_PRESETS": "weather@1.0"}):
            invalid, invalid_status = response_parts(self.control.create_execution_job())
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid.get_json(), {"error": "invalid or unconfigured skill preset"})

    def test_model_provider_job_accepts_only_non_sensitive_validated_params(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        payload = {
            "request_id": "model-provider-1",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.set_model_provider",
            "params": {
                "model_provider_id": "openai",
                "model_id": "openai/gpt-5",
                "model_base_url": "https://models.example/v1",
                "model_alias": "GPT-5",
            },
        }
        headers = {"Authorization": "Bearer admin-token"}
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            response, status = response_parts(self.control.create_execution_job())

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["job"]["params"], payload["params"])
        payload["params"]["model_api_key"] = "must-not-be-accepted"
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            invalid, invalid_status = response_parts(self.control.create_execution_job())
        self.assertEqual(invalid_status, 400)
        self.assertIn("unsupported params", invalid.get_json()["error"])

    def test_skill_job_records_audit_on_success(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice", db_file=self.db_file,
        )
        payload = {
            "request_id": "skill-success", "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"], "action": "instance.install_skill",
            "params": {"skill_id": "weather@1.0"},
        }
        headers = {"Authorization": "Bearer admin-token"}
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ), patch.dict(os.environ, {"MANAGER_SKILL_PRESETS": "weather@1.0"}):
            self.control.create_execution_job()
        self.control.metadata_store.update_execution_job(
            "skill-success", "running", db_file=self.db_file
        )
        with patch.object(self.control.request, "headers", {"Authorization": "Bearer executor-token"}), patch.object(
            self.control.request, "get_json", return_value={"status": "succeeded", "output": "installed"}
        ):
            response, status = response_parts(self.control.update_execution_job("skill-success"))
        self.assertEqual(status, 200)
        operation = self.control.metadata_store.list_operation_events(1, db_file=self.db_file)[0]
        self.assertEqual(operation["action"], "instance.install_skill")
        self.assertEqual(operation["message"], "skill=weather@1.0")

    def test_device_job_records_audit_on_success(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice", db_file=self.db_file,
        )
        payload = {
            "request_id": "devices-success",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.refresh_devices",
            "params": {},
        }
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            self.control.create_execution_job()
        self.control.metadata_store.update_execution_job(
            "devices-success", "running", db_file=self.db_file
        )
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer executor-token"}
        ), patch.object(
            self.control.request,
            "get_json",
            return_value={"status": "succeeded", "output": "refreshed"},
        ):
            response, status = response_parts(
                self.control.update_execution_job("devices-success")
            )
        self.assertEqual(status, 200)
        operation = self.control.metadata_store.list_operation_events(
            1, db_file=self.db_file
        )[0]
        self.assertEqual(operation["action"], "instance.refresh_devices")
        self.assertEqual(operation["message"], "device cache refreshed")

    def test_admin_model_provider_batch_is_idempotent_and_non_sensitive(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instances = [
            self.control.metadata_store.create_instance(
                owner_public_id=self.user["public_id"], product="openclaw",
                instance_name=name, runtime_identifier=runtime, db_file=self.db_file,
            )
            for name, runtime in (("One", "openclaw_one"), ("Two", "openclaw_two"))
        ]
        payload = {
            "request_id": "model-provider-batch-1",
            "actor_user_public_id": self.user["public_id"],
            "instances": [
                {
                    "instance_public_id": instances[0]["public_id"],
                    "model_provider_id": "openai",
                    "model_id": "openai/gpt-5",
                    "model_base_url": "https://models.example/v1",
                    "model_alias": "GPT-5",
                },
                {
                    "instance_public_id": instances[1]["public_id"],
                    "model_provider_id": "anthropic",
                    "model_id": "anthropic/claude",
                    "model_base_url": "",
                    "model_alias": "Claude",
                },
            ],
        }
        headers = {"Authorization": "Bearer admin-token"}
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            created, created_status = response_parts(
                self.control.create_model_provider_batch()
            )
            repeated, repeated_status = response_parts(
                self.control.create_model_provider_batch()
            )

        self.assertEqual(created_status, 200)
        self.assertEqual(repeated_status, 200)
        self.assertEqual(created.get_json(), repeated.get_json())
        body = created.get_json()
        self.assertEqual(body["parent"]["action"], "batch.set_model_provider")
        self.assertEqual(len(body["children"]), 2)
        self.assertTrue(all(
            child["action"] == "instance.set_model_provider"
            for child in body["children"]
        ))
        self.assertNotIn("api_key", repr(body).lower())
        with patch.object(
            self.control.request, "headers", headers
        ):
            fetched, fetched_status = response_parts(
                self.control.get_model_provider_batch("model-provider-batch-1")
            )
        self.assertEqual(fetched_status, 200)
        self.assertEqual(fetched.get_json(), body)

    def test_admin_device_batch_creates_idempotent_parent_and_children(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instances = [
            self.control.metadata_store.create_instance(
                owner_public_id=self.user["public_id"], product="openclaw",
                instance_name=name, runtime_identifier=runtime, db_file=self.db_file,
            )
            for name, runtime in (("One", "openclaw_one"), ("Two", "openclaw_two"))
        ]
        payload = {
            "request_id": "device-batch-1",
            "actor_user_public_id": self.user["public_id"],
            "action": "preview",
            "instance_public_ids": [item["public_id"] for item in instances],
        }
        headers = {"Authorization": "Bearer admin-token"}
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            created, created_status = response_parts(self.control.create_device_batch())
            repeated, repeated_status = response_parts(self.control.create_device_batch())

        created_json = created.get_json()
        self.assertEqual(created_status, 200)
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated.get_json(), created_json)
        self.assertEqual(created_json["parent"]["action"], "batch.device_preview")
        self.assertEqual(created_json["parent"]["status"], "succeeded")
        self.assertEqual(len(created_json["children"]), 2)
        self.assertTrue(all(
            job["action"] == "instance.refresh_devices"
            and job["parent_request_id"] == "device-batch-1"
            and job["summary"] == "queued"
            for job in created_json["children"]
        ))
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ):
            fetched, fetched_status = response_parts(
                self.control.get_device_batch("device-batch-1")
            )
        self.assertEqual(fetched_status, 200)
        self.assertEqual(fetched.get_json(), created_json)

        approve_payload = {
            **payload,
            "request_id": "device-batch-approve",
            "action": "approve",
        }
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=approve_payload
        ):
            approved, approved_status = response_parts(
                self.control.create_device_batch()
            )
        self.assertEqual(approved_status, 200)
        self.assertTrue(all(
            job["action"] == "instance.approve_latest_device"
            for job in approved.get_json()["children"]
        ))

    def test_admin_device_batch_rejects_invalid_target_atomically(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="One", runtime_identifier="openclaw_one", db_file=self.db_file,
        )
        payload = {
            "request_id": "device-batch-invalid",
            "actor_user_public_id": self.user["public_id"],
            "action": "approve",
            "instance_public_ids": [instance["public_id"], "missing-instance"],
        }
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.create_device_batch())

        self.assertEqual(status, 409)
        self.assertIn("instance not found", response.get_json()["error"])
        self.assertIsNone(self.control.metadata_store.get_execution_job(
            "device-batch-invalid", db_file=self.db_file
        ))

    def test_admin_action_batch_creates_lifecycle_and_skill_children(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instances = [
            self.control.metadata_store.create_instance(
                owner_public_id=self.user["public_id"], product="openclaw",
                instance_name=name, runtime_identifier=runtime, db_file=self.db_file,
            )
            for name, runtime in (("One", "openclaw_one"), ("Two", "openclaw_two"))
        ]
        instance_ids = [item["public_id"] for item in instances]
        headers = {"Authorization": "Bearer admin-token"}
        lifecycle = {
            "request_id": "action-batch-start", "actor_user_public_id": self.user["public_id"],
            "action": "start", "instance_public_ids": instance_ids,
        }
        skill = {
            **lifecycle, "request_id": "action-batch-skill", "action": "install_skill",
            "skill_id": "weather@1.0",
        }
        with patch.dict(self.control.os.environ, {"MANAGER_SKILL_PRESETS": "weather@1.0"}), \
             patch.object(self.control.request, "headers", headers), \
             patch.object(self.control.request, "get_json", side_effect=[lifecycle, lifecycle, skill]):
            started, started_status = response_parts(self.control.create_action_batch())
            repeated, repeated_status = response_parts(self.control.create_action_batch())
            installed, installed_status = response_parts(self.control.create_action_batch())

        self.assertEqual(started_status, 200)
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated.get_json(), started.get_json())
        self.assertEqual(installed_status, 200)
        self.assertTrue(all(
            child["action"] == "instance.start" and child["params"] == {}
            for child in started.get_json()["children"]
        ))
        self.assertTrue(all(
            child["action"] == "instance.install_skill"
            and child["params"] == {"skill_id": "weather@1.0"}
            for child in installed.get_json()["children"]
        ))
        with patch.object(self.control.request, "headers", headers):
            fetched, fetched_status = response_parts(
                self.control.get_action_batch("action-batch-start")
            )
        self.assertEqual(fetched_status, 200)
        self.assertEqual(fetched.get_json(), started.get_json())

    def test_admin_action_batch_rejects_invalid_target_atomically(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="One", runtime_identifier="openclaw_one", db_file=self.db_file,
        )
        payload = {
            "request_id": "action-batch-invalid",
            "actor_user_public_id": self.user["public_id"], "action": "stop",
            "instance_public_ids": [instance["public_id"], "missing-instance"],
        }
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer admin-token"}
        ), patch.object(self.control.request, "get_json", return_value=payload):
            response, status = response_parts(self.control.create_action_batch())

        self.assertEqual(status, 409)
        self.assertIn("instance not found", response.get_json()["error"])
        self.assertIsNone(self.control.metadata_store.get_execution_job(
            "action-batch-invalid", db_file=self.db_file
        ))

    def test_retention_jobs_validate_state_and_block_other_instance_jobs(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice", db_file=self.db_file,
        )
        payload = {
            "request_id": "delete-1",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.delete",
            "params": {},
        }
        headers = {"Authorization": "Bearer admin-token"}
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            created, delete_status = response_parts(self.control.create_execution_job())
            repeated, repeated_status = response_parts(self.control.create_execution_job())
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json",
            return_value={**payload, "request_id": "start-1", "action": "instance.start"},
        ):
            blocked, blocked_status = response_parts(self.control.create_execution_job())

        self.assertEqual(delete_status, 200)
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated.get_json()["job"], created.get_json()["job"])
        self.assertEqual(blocked_status, 409)
        self.assertIn("another instance task", blocked.get_json()["error"])

        with self.control.metadata_store.connect(self.db_file) as conn:
            conn.execute(
                "UPDATE instances SET status = 'deleted', restore_state = 'incomplete' WHERE public_id = ?",
                (instance["public_id"],),
            )
        payload.update(request_id="restore-1", action="instance.restore")
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            invalid, invalid_status = response_parts(self.control.create_execution_job())
        self.assertEqual(invalid_status, 409)
        self.assertEqual(invalid.get_json(), {"error": "instance is not restorable"})

    def test_retention_job_records_request_linked_audit(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice", db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="delete-success", actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"], action="instance.delete",
            params={}, db_file=self.db_file,
        )
        self.control.metadata_store.update_execution_job(
            "delete-success", "running", db_file=self.db_file
        )
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer executor-token"}
        ), patch.object(
            self.control.request, "get_json",
            return_value={"status": "succeeded", "output": "deleted"},
        ):
            _, status = response_parts(self.control.update_execution_job("delete-success"))

        operation = self.control.metadata_store.list_operation_events(
            1, db_file=self.db_file
        )[0]
        self.assertEqual(status, 200)
        self.assertEqual(operation["request_id"], "delete-success")
        self.assertEqual(operation["action"], "instance.delete")
        self.assertEqual(operation["message"], "instance deleted")

    def test_latest_device_approval_rejects_an_active_job(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice", db_file=self.db_file,
        )
        payload = {
            "request_id": "devices-approve-1",
            "actor_user_public_id": self.user["public_id"],
            "instance_public_id": instance["public_id"],
            "action": "instance.approve_latest_device",
            "params": {},
        }
        headers = {"Authorization": "Bearer admin-token"}
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            _, first_status = response_parts(self.control.create_execution_job())
        payload["request_id"] = "devices-approve-2"
        with patch.object(self.control.request, "headers", headers), patch.object(
            self.control.request, "get_json", return_value=payload
        ):
            second, second_status = response_parts(self.control.create_execution_job())

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)
        self.assertEqual(
            second.get_json(),
            {"error": "approve_latest_device is already running"},
        )

    def test_latest_device_approval_without_pending_request_is_audit_skipped(self):
        self.control.metadata_store.set_user_role(
            self.user["id"], "admin", db_file=self.db_file
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice", db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="devices-none", actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"],
            action="instance.approve_latest_device", params={}, db_file=self.db_file,
        )
        self.control.metadata_store.update_execution_job(
            "devices-none", "running", db_file=self.db_file
        )
        with patch.object(
            self.control.request, "headers", {"Authorization": "Bearer executor-token"}
        ), patch.object(
            self.control.request, "get_json",
            return_value={"status": "succeeded", "output": "No pending device request found."},
        ):
            _, status = response_parts(self.control.update_execution_job("devices-none"))

        operation = self.control.metadata_store.list_operation_events(
            1, db_file=self.db_file
        )[0]
        self.assertEqual(status, 200)
        self.assertEqual(operation["status"], "skipped")
        self.assertEqual(operation["message"], "No pending device request found.")

    def test_executor_updates_job_and_admin_reads_current_state(self):
        self.control.metadata_store.set_user_role(
            self.user["id"],
            "admin",
            db_file=self.db_file,
        )
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="request-1",
            actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"],
            action="instance.restart",
            params={},
            db_file=self.db_file,
        )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer executor-token"},
        ):
            with patch.object(
                self.control.request,
                "get_json",
                return_value={"status": "running", "current_step": "stopping"},
            ):
                updated, updated_status = response_parts(
                    self.control.update_execution_job("request-1")
                )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer admin-token"},
        ):
            fetched, fetched_status = response_parts(
                self.control.get_execution_job("request-1")
            )

        self.assertEqual(updated_status, 200)
        self.assertEqual(updated.get_json()["job"]["status"], "running")
        self.assertEqual(updated.get_json()["job"]["current_step"], "stopping")
        self.assertEqual(fetched_status, 200)
        self.assertEqual(fetched.get_json(), updated.get_json())

    def test_executor_lists_queued_jobs_for_serial_processing(self):
        self.control.metadata_store.set_user_role(
            self.user["id"],
            "admin",
            db_file=self.db_file,
        )
        for request_id in ("request-1", "request-2"):
            self.control.metadata_store.create_execution_job(
                request_id=request_id,
                actor_user_id=self.user["id"],
                action="instance.restart",
                params={},
                db_file=self.db_file,
            )
        self.control.metadata_store.update_execution_job(
            "request-1",
            "running",
            db_file=self.db_file,
        )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer executor-token"},
        ):
            with patch.object(
                self.control.request,
                "args",
                {"limit": "1"},
            ):
                response, status = response_parts(
                    self.control.list_execution_jobs()
                )

        self.assertEqual(status, 200)
        jobs = response.get_json()["jobs"]
        self.assertEqual([job["request_id"] for job in jobs], ["request-2"])
        self.assertEqual(jobs[0]["status"], "queued")

    def test_executor_claims_queued_job_once_with_server_resolved_instance(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="request-1",
            actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"],
            action="instance.start",
            params={},
            db_file=self.db_file,
        )

        with patch.object(
            self.control.request,
            "headers",
            {"Authorization": "Bearer executor-token"},
        ):
            claimed, claimed_status = response_parts(
                self.control.claim_execution_job()
            )
            empty, empty_status = response_parts(
                self.control.claim_execution_job()
            )

        self.assertEqual(claimed_status, 200)
        self.assertEqual(claimed.get_json()["job"]["status"], "running")
        self.assertEqual(
            claimed.get_json()["instance"],
            {
                "public_id": instance["public_id"],
                "legacy_user_id": None,
                "product": "openclaw",
                "runtime_identifier": "openclaw_alice",
                "data_path": None,
                "status": "active",
                "restore_state": "not_applicable",
                "access_role": None,
            },
        )
        self.assertEqual(empty_status, 204)
        self.assertEqual(empty, "")
        self.assertEqual(
            self.control.metadata_store.get_execution_job(
                "request-1", db_file=self.db_file
            )["status"],
            "running",
        )

    def test_executor_does_not_claim_another_job_while_one_is_running(self):
        for request_id in ("request-1", "request-2"):
            self.control.metadata_store.create_execution_job(
                request_id=request_id,
                actor_user_id=self.user["id"],
                action="instance.restart",
                params={},
                db_file=self.db_file,
            )
        self.control.metadata_store.update_execution_job(
            "request-1", "running", db_file=self.db_file
        )

        job, instance = self.control.metadata_store.claim_next_execution_job(
            db_file=self.db_file
        )

        self.assertIsNone(job)
        self.assertIsNone(instance)

    def test_executor_does_not_claim_job_after_its_instance_is_deleted(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="request-1",
            actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"],
            action="instance.start",
            params={},
            db_file=self.db_file,
        )
        with self.control.metadata_store.connect(self.db_file) as conn:
            conn.execute("DELETE FROM instances WHERE public_id = ?", (instance["public_id"],))

        job, runtime_instance = self.control.metadata_store.claim_next_execution_job(
            db_file=self.db_file
        )

        self.assertIsNone(job)
        self.assertIsNone(runtime_instance)
        self.assertEqual(
            self.control.metadata_store.get_execution_job(
                "request-1", db_file=self.db_file
            )["status"],
            "failed",
        )

    def test_executor_reclaims_job_after_stale_heartbeat(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"],
            product="openclaw",
            instance_name="Primary",
            runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="request-1",
            actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"],
            action="instance.start",
            params={},
            db_file=self.db_file,
        )
        self.control.metadata_store.update_execution_job(
            "request-1", "running", db_file=self.db_file
        )
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        ).isoformat()
        with self.control.metadata_store.connect(self.db_file) as conn:
            conn.execute(
                "UPDATE execution_jobs SET heartbeat_at = ? WHERE request_id = ?",
                (stale, "request-1"),
            )

        job, _ = self.control.metadata_store.claim_next_execution_job(
            stale_seconds=1, db_file=self.db_file
        )

        self.assertEqual(job["request_id"], "request-1")
        self.assertEqual(job["status"], "running")

    def test_executor_does_not_reclaim_interrupted_retention_job(self):
        instance = self.control.metadata_store.create_instance(
            owner_public_id=self.user["public_id"], product="openclaw",
            instance_name="Primary", runtime_identifier="openclaw_alice",
            db_file=self.db_file,
        )
        self.control.metadata_store.create_execution_job(
            request_id="delete-1", actor_user_id=self.user["id"],
            instance_public_id=instance["public_id"], action="instance.delete",
            params={}, db_file=self.db_file,
        )
        self.control.metadata_store.update_execution_job(
            "delete-1", "running", db_file=self.db_file
        )
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        ).isoformat()
        with self.control.metadata_store.connect(self.db_file) as conn:
            conn.execute(
                "UPDATE execution_jobs SET heartbeat_at = ? WHERE request_id = ?",
                (stale, "delete-1"),
            )

        job, runtime_instance = self.control.metadata_store.claim_next_execution_job(
            stale_seconds=1, db_file=self.db_file
        )

        stored = self.control.metadata_store.get_execution_job(
            "delete-1", db_file=self.db_file
        )
        self.assertIsNone(job)
        self.assertIsNone(runtime_instance)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("manual confirmation", stored["error_summary"])
        operation = self.control.metadata_store.list_operation_events(
            1, db_file=self.db_file
        )[0]
        self.assertEqual(operation["request_id"], "delete-1")
        self.assertEqual(operation["status"], "failed")


if __name__ == "__main__":
    unittest.main()
