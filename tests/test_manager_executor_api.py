#!/usr/bin/env python3

import importlib.util
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
EXECUTOR_DIR = ROOT_DIR / "services" / "manager-executor"
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"


def load_executor_api():
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

        post = get
        delete = get

    flask_stub.Flask = FakeFlask
    flask_stub.jsonify = lambda payload: FakeResponse(payload)
    flask_stub.request = types.SimpleNamespace(headers={}, files={}, get_json=lambda **kwargs: {})
    flask_stub.send_file = lambda *args, **kwargs: None
    werkzeug_stub = types.ModuleType("werkzeug")
    werkzeug_utils_stub = types.ModuleType("werkzeug.utils")
    werkzeug_utils_stub.secure_filename = lambda value: value

    sys.path.insert(0, str(MANAGER_WEB_DIR))
    sys.path.insert(0, str(EXECUTOR_DIR))
    spec = importlib.util.spec_from_file_location(
        "manager_executor_api",
        EXECUTOR_DIR / "api.py",
    )
    module = importlib.util.module_from_spec(spec)
    previous_flask = sys.modules.get("flask")
    previous_werkzeug = sys.modules.get("werkzeug")
    previous_werkzeug_utils = sys.modules.get("werkzeug.utils")
    sys.modules["flask"] = flask_stub
    sys.modules["werkzeug"] = werkzeug_stub
    sys.modules["werkzeug.utils"] = werkzeug_utils_stub
    try:
        spec.loader.exec_module(module)
    finally:
        for name, previous in (
            ("flask", previous_flask),
            ("werkzeug", previous_werkzeug),
            ("werkzeug.utils", previous_werkzeug_utils),
        ):
            if previous is None:
                del sys.modules[name]
            else:
                sys.modules[name] = previous
    return module


class ManagerExecutorApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load_executor_api()

    def test_upload_rejects_product_without_file_capability_before_reading_file(self):
        instance = {
            "product": "evoscientist",
            "access_role": "owner",
            "data_path": "/should/not/be/used",
        }
        with patch.object(
            self.api,
            "runtime_instance",
            return_value=(instance, None),
        ), patch.object(self.api, "resolve_instance_file") as resolve_file:
            response, status = self.api.upload_file("instance-1")

        self.assertEqual(status, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "instance product does not support file upload"},
        )
        resolve_file.assert_not_called()

    def test_snapshot_rejects_unknown_product_without_constructing_adapter(self):
        instance = {"product": "unknown", "access_role": "owner"}
        with patch.object(
            self.api,
            "runtime_instance",
            return_value=(instance, None),
        ), patch.object(self.api, "get_adapter") as get_adapter:
            response, status = self.api.snapshot("instance-1")

        self.assertEqual(status, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "instance product does not support status"},
        )
        get_adapter.assert_not_called()

    def test_admin_instance_statuses_returns_normalized_runtime_statuses(self):
        self.api.request.headers = {
            "Authorization": "Bearer admin-token",
            "X-Actor-User-Public-Id": "admin-1",
        }
        self.api.request.get_json = lambda **kwargs: {
            "instance_public_ids": ["instance-1", "instance-2"]
        }
        with patch.dict(self.api.TOKENS, {"admin": "admin-token", "user": "user-token"}), patch.object(
            self.api.CONTROL, "get_runtime_instance",
            side_effect=[{"product": "openclaw"}, {"product": "openclaw"}],
        ), patch.object(self.api, "get_adapter") as get_adapter:
            get_adapter.return_value.status.side_effect = ["Up", "STOPPED"]
            response = self.api.admin_instance_statuses()

        self.assertEqual(response.get_json()["statuses"], [
            {"instance_public_id": "instance-1", "status": "running"},
            {"instance_public_id": "instance-2", "status": "stopped"},
        ])

    def test_admin_instance_statuses_rejects_user_service_token(self):
        self.api.request.headers = {"Authorization": "Bearer user-token"}
        with patch.dict(self.api.TOKENS, {"admin": "admin-token", "user": "user-token"}):
            response, status = self.api.admin_instance_statuses()

        self.assertEqual(status, 403)
        self.assertEqual(response.get_json(), {"error": "admin service token is required"})

    def test_upload_rejects_target_replacement_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            escaped = root / "escaped.md"
            target = uploads / "race.md"
            instance = {"product": "openclaw", "access_role": "owner", "data_path": str(root)}

            class Upload:
                filename = "race.md"

                def save(self, destination):
                    destination.write(b"attacker")

            def resolve_with_swap(*_args):
                target.symlink_to(escaped)
                return target

            self.api.request.files = {"file": Upload()}
            with patch.object(self.api, "runtime_instance", return_value=(instance, None)), patch.object(
                self.api, "resolve_instance_file", side_effect=resolve_with_swap
            ):
                response, status = self.api.upload_file("instance-1")

            self.assertEqual(status, 409)
            self.assertFalse(escaped.exists())

    def test_download_rejects_target_replacement_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            escaped = root / "escaped.md"
            escaped.write_text("secret", encoding="utf-8")
            target = workspace / "race.md"
            target.write_text("safe", encoding="utf-8")
            instance = {"product": "openclaw", "access_role": "owner", "data_path": str(root)}

            def resolve_with_swap(*_args):
                target.unlink()
                target.symlink_to(escaped)
                return target

            with patch.object(self.api, "runtime_instance", return_value=(instance, None)), patch.object(
                self.api, "resolve_instance_file", side_effect=resolve_with_swap
            ), patch.object(self.api, "send_file", side_effect=lambda path, **_: Path(path).read_text()):
                response = self.api.download_file("instance-1", "workspace", "race.md")

            self.assertEqual(response[1], 404)

    def test_upload_success_writes_supported_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            instance = {"product": "openclaw", "access_role": "owner", "data_path": str(root)}

            class Upload:
                filename = "notes.md"

                def save(self, destination):
                    destination.write(b"notes")

            self.api.request.files = {"file": Upload()}
            with patch.object(self.api, "runtime_instance", return_value=(instance, None)):
                response, status = self.api.upload_file("instance-1")

            self.assertEqual(status, 201)
            self.assertEqual((root / "uploads" / "notes.md").read_bytes(), b"notes")

    def test_upload_write_failure_removes_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            instance = {"product": "openclaw", "access_role": "owner", "data_path": str(root)}

            class Upload:
                filename = "broken.md"

                def save(self, destination):
                    raise RuntimeError("injected write failure")

            self.api.request.files = {"file": Upload()}
            with patch.object(self.api, "runtime_instance", return_value=(instance, None)):
                response, status = self.api.upload_file("instance-1")

            self.assertEqual(status, 500)
            self.assertFalse((root / "uploads" / "broken.md").exists())

    def test_upload_fails_closed_without_atomic_open_support(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            instance = {"product": "openclaw", "access_role": "owner", "data_path": str(root)}
            upload = types.SimpleNamespace(filename="notes.md", save=lambda _: None)
            self.api.request.files = {"file": upload}
            with patch.object(self.api, "runtime_instance", return_value=(instance, None)), patch.object(
                self.api.os, "supports_dir_fd", set()
            ):
                response, status = self.api.upload_file("instance-1")

            self.assertEqual(status, 500)

    def test_download_success_opens_supported_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "notes.md"
            target.write_text("notes", encoding="utf-8")
            instance = {"product": "openclaw", "access_role": "owner", "data_path": str(root)}
            with patch.object(self.api, "runtime_instance", return_value=(instance, None)), patch.object(
                self.api, "send_file", side_effect=lambda path, **_: path.read()
            ):
                response = self.api.download_file("instance-1", "workspace", "notes.md")

            self.assertEqual(response, b"notes")

    def test_download_fails_closed_when_fd_identity_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "notes.md"
            target.write_text("notes", encoding="utf-8")
            instance = {"product": "openclaw", "access_role": "owner", "data_path": str(root)}
            with patch.object(self.api, "runtime_instance", return_value=(instance, None)), patch.object(
                self.api.os, "readlink", side_effect=OSError("identity unavailable")
            ):
                response = self.api.download_file("instance-1", "workspace", "notes.md")

            self.assertEqual(response[1], 404)


if __name__ == "__main__":
    unittest.main()
