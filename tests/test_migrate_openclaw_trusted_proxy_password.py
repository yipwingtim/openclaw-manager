import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/migrate_openclaw_trusted_proxy_password.py"


def load_script():
    spec = importlib.util.spec_from_file_location("migrate_openclaw_trusted_proxy_password", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrustedProxyPasswordMigrationTests(unittest.TestCase):
    def test_dry_run_and_apply_add_password_without_token(self):
        module = load_script()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            module.PUBLIC_DIR = root / "public"
            module.DB_FILE = module.PUBLIC_DIR / "manager.db"
            path = module.PUBLIC_DIR / "instances/openclaw/id/config/openclaw.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"gateway": {"auth": {"mode": "trusted-proxy"}}}), encoding="utf-8")
            with sqlite3.connect(module.DB_FILE) as db:
                db.execute("CREATE TABLE instances (public_id, legacy_user_id, data_path, status, product)")
                db.execute("INSERT INTO instances VALUES ('id', 'alice', ?, 'active', 'openclaw')", (str(path.parent.parent),))
            self.assertEqual(module.main([]), 0)
            self.assertFalse(json.loads(path.read_text())["gateway"]["auth"].get("password"))
            self.assertEqual(module.main(["--apply"]), 0)
            auth = json.loads(path.read_text())["gateway"]["auth"]
            self.assertTrue(auth["password"])
            self.assertNotIn("token", auth)
            self.assertTrue(list((module.PUBLIC_DIR / ".manager-auth-backups").iterdir()))


if __name__ == "__main__":
    unittest.main()
