import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MANAGER_WEB_DIR = ROOT_DIR / "services" / "manager-web"
sys.path.insert(0, str(MANAGER_WEB_DIR))


def load_app():
    flask_stub = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}
            self.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)

        def route(self, *args, **kwargs):
            return lambda func: func

        get = route
        post = route

        def before_request(self, func):
            return func

        def context_processor(self, func):
            return func

    flask_stub.Flask = FakeFlask
    flask_stub.Response = object
    flask_stub.redirect = lambda *args, **kwargs: None
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.request = types.SimpleNamespace(headers={}, cookies={}, method="GET", path="/")
    flask_stub.send_file = lambda *args, **kwargs: None
    flask_stub.url_for = lambda endpoint, **kwargs: endpoint

    werkzeug_stub = types.ModuleType("werkzeug")
    werkzeug_utils_stub = types.ModuleType("werkzeug.utils")
    werkzeug_utils_stub.secure_filename = lambda value: value
    modules = {
        "flask": flask_stub,
        "werkzeug": werkzeug_stub,
        "werkzeug.utils": werkzeug_utils_stub,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    spec = importlib.util.spec_from_file_location("external_auth_app", MANAGER_WEB_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


class ExternalAuthFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_app()

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.module.PUBLIC_DIR = Path(self.temp_dir.name)
        self.module.MANAGER_DIR = ROOT_DIR
        self.module.metadata_store.DB_FILE = self.module.PUBLIC_DIR / "manager.db"
        self.module.metadata_store.initialize(schema_file=ROOT_DIR / "db" / "schema.sql")
        self.module.MANAGER_AUTH_PROVIDER = "campus-uis"
        self.module.MANAGER_SESSION_SECRET = "test-session-secret"
        self.module.MANAGER_COOKIE_SECURE = True
        self.module.OPENCLAW_INTERNAL_TOKEN = "internal-token"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_callback_requires_prelinked_active_identity(self):
        client = Mock()
        client.authorize_access_token.return_value = {"userinfo": {"sub": "external-1"}}
        config = {"provider": "campus-uis", "auth_type": "oidc", "subject_claim": "sub"}

        with patch.object(self.module, "get_external_auth", return_value=(client, config)):
            response = self.module.external_auth_callback()

        self.assertEqual(response[1], 403)

    def test_callback_creates_server_session_for_prelinked_identity(self):
        user = self.module.metadata_store.create_user("alice")
        self.module.metadata_store.upsert_identity(user["id"], "campus-uis", "external-1", "alice")
        client = Mock()
        client.authorize_access_token.return_value = {"userinfo": {"sub": "external-1"}}
        config = {"provider": "campus-uis", "auth_type": "oidc", "subject_claim": "sub"}

        with patch.object(
            self.module, "get_external_auth", return_value=(client, config)
        ), patch.object(self.module, "create_manager_session", return_value="session-created") as create_session:
            response = self.module.external_auth_callback()

        self.assertEqual(response, "session-created")
        create_session.assert_called_once_with(ANY, "campus-uis")

    def test_emergency_entry_requires_allowlisted_active_admin(self):
        user = self.module.metadata_store.create_user("breakglass")
        self.module.metadata_store.set_user_role(user["id"], "admin")
        self.module.MANAGER_EMERGENCY_USERS = {"breakglass"}

        with patch.object(
            self.module.request, "headers", {"X-Remote-User": "breakglass"}
        ), patch.object(self.module, "create_manager_session", return_value="emergency-created") as create_session:
            response = self.module.emergency_login()

        self.assertEqual(response, "emergency-created")
        create_session.assert_called_once_with(
            ANY, "campus-uis", session_kind="emergency"
        )

    def test_emergency_entry_fails_closed_without_internal_token(self):
        self.module.MANAGER_EMERGENCY_USERS = {"breakglass"}
        self.module.OPENCLAW_INTERNAL_TOKEN = ""

        with patch.object(
            self.module.request, "headers", {"X-Remote-User": "breakglass"}
        ):
            response = self.module.emergency_login()

        self.assertEqual(response[1], 403)


if __name__ == "__main__":
    unittest.main()
