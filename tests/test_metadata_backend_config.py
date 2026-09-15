import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "services/manager-web/metadata_store.py"


def load(**env):
    clean_env = os.environ.copy()
    clean_env.pop("METADATA_DB_BACKEND", None)
    clean_env.pop("METADATA_DATABASE_URL", None)
    clean_env.update(env)
    with patch.dict(os.environ, clean_env, clear=True):
        spec = importlib.util.spec_from_file_location("metadata_store_test", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class MetadataBackendConfigTests(unittest.TestCase):
    def test_sqlite_remains_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load(METADATA_DB_FILE=str(Path(temp_dir) / "manager.db"))
            module.initialize(schema_file=ROOT / "db/schema.sql")
            with module.connect() as conn:
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_postgres_connection_is_explicitly_blocked(self):
        module = load(METADATA_DB_BACKEND="postgres")
        with self.assertRaisesRegex(RuntimeError, "upcoming schema"):
            with module.connect():
                pass

    def test_postgres_schema_initialization_is_explicitly_blocked(self):
        module = load(
            METADATA_DB_BACKEND="postgres",
            METADATA_DATABASE_URL="postgresql://unused",
        )
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            module.initialize()


if __name__ == "__main__":
    unittest.main()
