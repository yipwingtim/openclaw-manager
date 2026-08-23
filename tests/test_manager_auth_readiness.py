import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_manager_auth_readiness.py"


class ManagerAuthReadinessTests(unittest.TestCase):
    def run_check(self, values):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", **values}
        return subprocess.run(
            ["python3", str(SCRIPT)], text=True, capture_output=True, env=env
        )

    def test_local_provider_passes(self):
        result = self.run_check({"MANAGER_AUTH_PROVIDER": "local"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[OK] Manager auth provider=local", result.stdout)

    def test_external_provider_without_fallback_fails(self):
        result = self.run_check({
            "MANAGER_AUTH_PROVIDER": "company-sso",
            "MANAGER_AUTH_TYPE": "oauth2",
            "MANAGER_OAUTH_CLIENT_ID": "client",
            "MANAGER_OAUTH_CLIENT_SECRET": "secret",
            "MANAGER_OAUTH_AUTHORIZE_URL": "https://login.example.test/authorize",
            "MANAGER_OAUTH_TOKEN_URL": "https://login.example.test/token",
            "MANAGER_OAUTH_USERINFO_URL": "https://login.example.test/userinfo",
            "MANAGER_OAUTH_SUBJECT_CLAIM": "uid",
            "MANAGER_OAUTH_REDIRECT_URI": "https://manager.example.test/auth/callback",
            "MANAGER_SESSION_SECRET": "session-secret",
        })

        self.assertEqual(result.returncode, 1)
        self.assertIn("fallback_or_emergency", result.stdout)


if __name__ == "__main__":
    unittest.main()
