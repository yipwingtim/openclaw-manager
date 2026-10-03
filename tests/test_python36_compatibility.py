import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Python36CompatibilityTests(unittest.TestCase):
    def test_tenant_network_helper_uses_python36_subprocess_options(self):
        source = (ROOT / "scripts" / "lib_tenant_network.sh").read_text()
        self.assertNotIn("capture_output=True", source)
        self.assertNotIn("text=True", source)
        self.assertGreaterEqual(source.count("universal_newlines=True"), 6)
        self.assertGreaterEqual(source.count("stdout=subprocess.PIPE"), 6)
        self.assertGreaterEqual(source.count("stderr=subprocess.PIPE"), 6)


if __name__ == "__main__":
    unittest.main()
