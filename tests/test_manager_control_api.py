#!/usr/bin/env python3

import importlib.util
import os
import sys
import tempfile
import types
import unittest
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

    sys.path.insert(0, str(MANAGER_WEB_DIR))
    spec = importlib.util.spec_from_file_location(
        "manager_control_app", CONTROL_DIR / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    previous_flask = sys.modules.get("flask")
    sys.modules["flask"] = flask_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_flask is None:
            del sys.modules["flask"]
        else:
            sys.modules["flask"] = previous_flask
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
                "schema_version": 4,
                "service_tokens_configured": True,
            },
        )

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
        self.assertNotIn("runtime_identifier", visible.get_json()["instance"])
        self.assertNotIn("data_path", visible.get_json()["instance"])
        self.assertEqual(hidden_status, 404)
        self.assertEqual(hidden.get_json(), {"error": "instance not found"})

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


if __name__ == "__main__":
    unittest.main()
