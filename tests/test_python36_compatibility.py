import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOST_RUNTIME_FILES = [
    *sorted((ROOT / "scripts").glob("*.py")),
    ROOT / "scripts" / "update_manager_auth.sh",
    ROOT / "services" / "manager-control" / "hermes_auth_bridge.py",
    ROOT / "services" / "manager-web" / "auth_providers.py",
    ROOT / "services" / "manager-web" / "instance_adapters.py",
    ROOT / "services" / "manager-web" / "metadata_store.py",
    ROOT / "services" / "manager-web" / "product_capabilities.py",
]

FORBIDDEN_PYTHON36_APIS = (
    "capture_output=True",
    "text=True",
    "missing_ok=True",
    ".removeprefix(",
    ".removesuffix(",
    ".subnet_of(",
    ".supernet_of(",
    "from dataclasses import",
    "from zoneinfo import",
)


class Python36CompatibilityTests(unittest.TestCase):
    def test_host_runtime_closure_avoids_newer_python_apis(self):
        for path in HOST_RUNTIME_FILES:
            source = path.read_text()
            for api in FORBIDDEN_PYTHON36_APIS:
                self.assertNotIn(api, source, "%s uses %s" % (path, api))

    def test_host_sqlite_calls_convert_path_objects_to_strings(self):
        for path in HOST_RUNTIME_FILES:
            source = path.read_text()
            for line_number, line in enumerate(source.splitlines(), 1):
                if "sqlite3.connect(" not in line:
                    continue
                argument = line.split("sqlite3.connect(", 1)[1].lstrip()
                self.assertTrue(
                    argument.startswith(('str(', 'f"', "f'", '":memory:"', "':memory:'")),
                    "%s:%s passes a possible PathLike to sqlite3.connect" % (
                        path, line_number,
                    ),
                )

    def test_tenant_network_helper_uses_python36_subprocess_options(self):
        source = (ROOT / "scripts" / "lib_tenant_network.sh").read_text()
        self.assertNotIn("capture_output=True", source)
        self.assertNotIn("text=True", source)
        self.assertGreaterEqual(source.count("universal_newlines=True"), 6)
        self.assertGreaterEqual(source.count("stdout=subprocess.PIPE"), 6)
        self.assertGreaterEqual(source.count("stderr=subprocess.PIPE"), 6)

    def test_tenant_network_allocator_does_not_use_python37_subparser_required(self):
        source = (ROOT / "scripts" / "tenant_network_allocator.py").read_text()
        self.assertNotIn("add_subparsers(dest=\"command\", required=True)", source)
        self.assertIn('if not getattr(args, "command", None):', source)

    def test_tenant_network_allocator_subprocess_calls_support_python36(self):
        source = (ROOT / "scripts" / "tenant_network_allocator.py").read_text()
        self.assertNotIn("capture_output=True", source)
        self.assertNotIn("text=True", source)
        self.assertIn("universal_newlines=True", source)
        self.assertIn("stdout=subprocess.PIPE", source)
        self.assertIn("stderr=subprocess.PIPE", source)

    def test_tenant_network_allocator_does_not_use_python37_network_helpers(self):
        source = (ROOT / "scripts" / "tenant_network_allocator.py").read_text()
        self.assertNotIn(".subnet_of(", source)
        self.assertNotIn(".supernet_of(", source)

    def test_metadata_cli_does_not_use_python37_subparser_required(self):
        source = (ROOT / "scripts" / "metadata_cli.py").read_text()
        self.assertNotIn("add_subparsers(dest=\"command\", required=True)", source)
        self.assertIn('if not getattr(args, "command", None):', source)

    def test_metadata_consistency_tests_support_python36(self):
        source = (ROOT / "tests" / "test_upgrade_metadata_consistency.py").read_text()
        self.assertNotIn("capture_output=True", source)
        self.assertNotIn("text=True", source)
        self.assertIn('env["METADATA_DB_BACKEND"] = "sqlite"', source)
        self.assertIn('env.pop("METADATA_DATABASE_URL", None)', source)


if __name__ == "__main__":
    unittest.main()
