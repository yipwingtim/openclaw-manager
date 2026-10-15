"""Exercise the real Bash allocator with a controlled PostgreSQL driver."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = '''
import os
class Connection:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def cursor(self): return self
    def execute(self, sql):
        assert "'failed'" in sql
        assert "'stopped'" in sql
    def fetchall(self): return [(40064,), (40065,)]
def connect(*args, **kwargs):
    if os.environ.get("FAIL_LOOKUP"):
        raise RuntimeError("sensitive connection details")
    return Connection()
'''

class PostgresPortAllocatorTests(unittest.TestCase):
    def allocate(self, *, fail=False, missing_url=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'psycopg.py').write_text(DRIVER)
            counter = root / 'ports.txt'
            counter.write_text('40064\n')
            env = {**os.environ, 'PYTHONPATH': directory,
                   'METADATA_DB_BACKEND': 'postgres',
                   'METADATA_DATABASE_URL': 'test-only',
                   'PORT_AVAILABILITY_COMMAND': 'true',
                   'PORT_LOCK_FILE': str(root / 'ports.lock'),
                   'FAIL_LOOKUP': '1' if fail else ''}
            if missing_url:
                env.pop('METADATA_DATABASE_URL')
            result = subprocess.run(
                ['bash', '-c', 'source "$1"; allocate_port "$2" 40064 40070',
                 'bash', str(ROOT / 'scripts/lib_port_allocator.sh'), str(counter)],
                env=env, capture_output=True, text=True)
            return result, counter.read_text()

    def test_skips_reserved_ports(self):
        result, counter = self.allocate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '40066')
        self.assertEqual(counter.strip(), '40067')

    def test_failed_lookup_stops_without_advancing_counter(self):
        for options in ({'fail': True}, {'missing_url': True}):
            with self.subTest(options=options):
                result, counter = self.allocate(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIn('PostgreSQL port lookup failed', result.stderr)
                self.assertNotIn('sensitive connection details', result.stderr)
                self.assertEqual(counter.strip(), '40064')

    def test_executor_dependency_matches_control(self):
        requirement = 'psycopg[binary]==3.2.9'
        for service in ('manager-control', 'manager-executor'):
            self.assertIn(requirement, (ROOT / 'services' / service / 'requirements.txt').read_text().splitlines())

    def test_runtime_validation_checks_executor_database_access(self):
        script = (ROOT / 'scripts/check_runtime_security.sh').read_text()
        self.assertIn('docker exec "$MANAGER_EXECUTOR_CONTAINER_NAME" python3', script)
        self.assertIn('SELECT port FROM instances LIMIT 0', script)
        self.assertIn('error "executor PostgreSQL port lookup is unavailable"', script)

if __name__ == '__main__':
    unittest.main()
