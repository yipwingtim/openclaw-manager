import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "services" / "manager-control"))
from hermes_auth_bridge import BridgeStore, SigningKeys, pkce_challenge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "manager.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript((ROOT / "db" / "schema.sql").read_text())
            conn.execute("INSERT INTO users(public_id,username,normalized_username) VALUES('user-1','u','u')")
            conn.execute("INSERT INTO instances(public_id,owner_user_id,product,instance_name,runtime_identifier) VALUES('instance-1',1,'hermes','h','h')")
            conn.execute(
                "INSERT INTO user_sessions(token_hash,user_id,provider,csrf_token,expires_at,created_at,last_seen_at) "
                "VALUES('session',1,'campus-uis','csrf','2099-01-01','2026-01-01','2026-01-01')"
            )
        self.store = BridgeStore(self.db)
        self.secret = "s" * 32
        self.store.create_client(1, "client-1", self.secret, "https://h/auth/callback")

    def tearDown(self):
        self.temp.cleanup()

    def grant(self):
        return self.store.issue_grant(
            client_id="client-1", instance_id=1, user_id=1, session_id="session",
            redirect_uri="https://h/auth/callback", code_challenge=pkce_challenge("v" * 43),
        )

    def redeem(self, code):
        return self.store.redeem(
            code=code, client_id="client-1", secret=self.secret,
            redirect_uri="https://h/auth/callback", verifier="v" * 43,
        )

    def test_pkce_and_single_use(self):
        code = self.grant()
        principal = self.redeem(code)
        self.assertEqual((principal.user_id, principal.instance_id), ("user-1", "instance-1"))
        with self.assertRaisesRegex(ValueError, "invalid_grant"):
            self.redeem(code)

    def test_wrong_pkce_does_not_consume_grant(self):
        code = self.grant()
        with self.assertRaisesRegex(ValueError, "invalid_grant"):
            self.store.redeem(code=code, client_id="client-1", secret=self.secret,
                              redirect_uri="https://h/auth/callback", verifier="w" * 43)
        self.assertEqual(self.redeem(code).client_id, "client-1")

    def test_grant_requires_matching_active_manager_session(self):
        with self.assertRaisesRegex(ValueError, "invalid client"):
            self.store.issue_grant(
                client_id="client-1", instance_id=1, user_id=1,
                session_id="missing", redirect_uri="https://h/auth/callback",
                code_challenge=pkce_challenge("v" * 43),
            )

    def test_grant_requires_instance_authorization(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO users(public_id,username,normalized_username) "
                "VALUES('user-2','u2','u2')"
            )
            conn.execute(
                "INSERT INTO user_sessions(token_hash,user_id,provider,csrf_token,expires_at,created_at,last_seen_at) "
                "VALUES('session-2',2,'campus-uis','csrf','2099-01-01','2026-01-01','2026-01-01')"
            )
        with self.assertRaisesRegex(ValueError, "invalid client"):
            self.store.issue_grant(
                client_id="client-1", instance_id=1, user_id=2,
                session_id="session-2", redirect_uri="https://h/auth/callback",
                code_challenge=pkce_challenge("v" * 43),
            )
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO instance_members(instance_id,user_id,role) "
                "VALUES(1,2,'viewer')"
            )
        code = self.store.issue_grant(
            client_id="client-1", instance_id=1, user_id=2,
            session_id="session-2", redirect_uri="https://h/auth/callback",
            code_challenge=pkce_challenge("v" * 43),
        )
        self.assertTrue(code)

    def test_client_requires_active_hermes_instance(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO instances(public_id,owner_user_id,product,instance_name,runtime_identifier) "
                "VALUES('openclaw-1',1,'openclaw','o','o')"
            )
        with self.assertRaisesRegex(ValueError, "invalid Hermes instance"):
            self.store.create_client(2, "bad", "b" * 32, "https://h/auth/callback")

    def test_client_requires_exact_https_callback(self):
        for redirect_uri in (
            "http://h/auth/callback", "https:///auth/callback",
            "https://user@h/auth/callback", "https://h/auth/callback?next=x",
        ):
            with self.subTest(redirect_uri=redirect_uri):
                with self.assertRaisesRegex(ValueError, "invalid client metadata"):
                    self.store.create_client(1, "bad", "b" * 32, redirect_uri)

    def test_grant_requires_s256_challenge_and_uis_session(self):
        with self.assertRaisesRegex(ValueError, "invalid grant metadata"):
            self.store.issue_grant(
                client_id="client-1", instance_id=1, user_id=1,
                session_id="session", redirect_uri="https://h/auth/callback",
                code_challenge="!" * 43,
            )
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE user_sessions SET provider = 'local'")
        with self.assertRaisesRegex(ValueError, "invalid client"):
            self.grant()

    def test_grant_is_expired_at_exact_expiry_time(self):
        code = self.store.issue_grant(
            client_id="client-1", instance_id=1, user_id=1, session_id="session",
            redirect_uri="https://h/auth/callback",
            code_challenge=pkce_challenge("v" * 43), ttl=60, now=100,
        )
        with self.assertRaisesRegex(ValueError, "invalid_grant"):
            self.store.redeem(
                code=code, client_id="client-1", secret=self.secret,
                redirect_uri="https://h/auth/callback", verifier="v" * 43,
                now=160,
            )

    def test_malformed_verifier_is_generic_invalid_grant(self):
        with self.assertRaisesRegex(ValueError, "^invalid_grant$"):
            self.store.redeem(
                code=self.grant(), client_id="client-1", secret=self.secret,
                redirect_uri="https://h/auth/callback", verifier="short",
            )

    def test_client_secret_rotation_and_revocation(self):
        code = self.grant()
        new_secret = "n" * 32
        self.store.rotate_client_secret("client-1", new_secret, now=10)
        with self.assertRaisesRegex(ValueError, "invalid_grant"):
            self.redeem(code)
        principal = self.store.redeem(
            code=code, client_id="client-1", secret=new_secret,
            redirect_uri="https://h/auth/callback", verifier="v" * 43,
        )
        self.assertEqual(principal.client_id, "client-1")
        self.store.revoke_client("client-1", now=11)
        with self.assertRaisesRegex(ValueError, "invalid client"):
            self.grant()

    def test_concurrent_redeem_only_once(self):
        code = self.grant()
        barrier = threading.Barrier(2)
        results = []
        def redeem():
            barrier.wait()
            try:
                self.redeem(code)
                results.append("ok")
            except ValueError:
                results.append("invalid")
        threads = [threading.Thread(target=redeem) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(results, ["ok", "invalid"])

    def test_access_token_and_key_rotation(self):
        old_file = Path(self.temp.name) / "old.pem"
        new_file = Path(self.temp.name) / "new.pem"
        old_file.write_bytes(SigningKeys.generate_private_key_pem())
        new_file.write_bytes(SigningKeys.generate_private_key_pem())
        keys = SigningKeys.from_pem_files(
            {"old": old_file, "new": new_file}, "old"
        )
        old = keys.active_kid
        token = keys.issue_access_token(self.redeem(self.grant()), "https://manager/auth/hermes", now=100)
        header, payload, signature = token.split(".")
        decode = lambda value: json.loads(base64.urlsafe_b64decode(value + "=="))
        self.assertEqual(decode(header)["alg"], "EdDSA")
        self.assertEqual(decode(payload)["exp"], 1000)
        jwk = keys.jwks()["keys"][0]
        Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(jwk["x"] + "==")
        ).verify(base64.urlsafe_b64decode(signature + "=="), f"{header}.{payload}".encode())
        keys.activate("new")
        self.assertEqual({key["kid"] for key in keys.jwks()["keys"]}, {old, "new"})
        keys.retire(old)
        self.assertEqual([key["kid"] for key in keys.jwks()["keys"]], ["new"])
        with self.assertRaisesRegex(ValueError, "cannot retire active"):
            keys.retire("new")
        with self.assertRaisesRegex(ValueError, "not loaded"):
            keys.activate("missing")

    def test_signing_keys_require_configured_active_persistent_key(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            SigningKeys()
        key_file = Path(self.temp.name) / "key.pem"
        key_file.write_bytes(SigningKeys.generate_private_key_pem())
        with self.assertRaisesRegex(ValueError, "active signing kid"):
            SigningKeys.from_pem_files({"loaded": key_file}, "missing")


if __name__ == "__main__":
    unittest.main()
