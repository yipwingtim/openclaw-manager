"""Storage and token primitives for the Hermes authentication bridge."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pathlib import Path

PKCE_VERIFIER_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
DUMMY_SECRET_HASH = (
    "scrypt$16384$8$1$MDAwMDAwMDAwMDAwMDAwMA$"
    "RteE2S7cV9VCRcUqP7s5NB_4o1aM2kBJ6LWzGsP0g7TYmQSj9AJbqQmZbHAFdSMs"
    "I2PbfwgVg1ZW9qEuIvjO9A"
)


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def sha256(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def pkce_challenge(verifier):
    if not 43 <= len(verifier) <= 128 or not set(verifier) <= PKCE_VERIFIER_CHARS:
        raise ValueError("invalid PKCE verifier")
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def valid_redirect_uri(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and parsed.path.endswith("/auth/callback")
        )
    except (TypeError, ValueError):
        return False


def hash_client_secret(secret):
    if len(secret) < 32:
        raise ValueError("client secret must contain at least 32 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt$16384$8$1${b64url(salt)}${b64url(digest)}"


def verify_client_secret(secret, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
            return False
        actual = hashlib.scrypt(
            secret.encode(), salt=base64.urlsafe_b64decode(salt + "=="),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(b64url(actual), expected)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class BridgePrincipal:
    user_id: str
    instance_id: str
    client_id: str


class BridgeStore:
    def __init__(self, db_file):
        self.db_file = db_file

    def connect(self):
        conn = sqlite3.connect(self.db_file, timeout=10, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def create_client(self, instance_id, client_id, secret, redirect_uri):
        if not client_id or not valid_redirect_uri(redirect_uri):
            raise ValueError("invalid client metadata")
        with self.connect() as conn:
            instance = conn.execute(
                "SELECT 1 FROM instances WHERE id = ? AND product = 'hermes' "
                "AND status = 'active'", (instance_id,),
            ).fetchone()
            if not instance:
                raise ValueError("invalid Hermes instance")
            conn.execute(
                "INSERT INTO hermes_auth_clients "
                "(instance_id, client_id, client_secret_hash, redirect_uri) "
                "VALUES (?, ?, ?, ?)",
                (instance_id, client_id, hash_client_secret(secret), redirect_uri),
            )

    def rotate_client_secret(self, client_id, secret, now=None):
        now = int(time.time() if now is None else now)
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE hermes_auth_clients SET client_secret_hash = ?, "
                "rotated_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                (hash_client_secret(secret), now, client_id),
            ).rowcount
        if changed != 1:
            raise ValueError("invalid client")

    def revoke_client(self, client_id, now=None):
        now = int(time.time() if now is None else now)
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE hermes_auth_clients SET revoked_at = ? "
                "WHERE client_id = ? AND revoked_at IS NULL", (now, client_id),
            ).rowcount
        if changed != 1:
            raise ValueError("invalid client")

    def issue_grant(self, *, client_id, instance_id, user_id, session_id,
                    redirect_uri, code_challenge, ttl=60, now=None):
        now = int(time.time() if now is None else now)
        if not PKCE_CHALLENGE_RE.fullmatch(code_challenge) or ttl <= 0:
            raise ValueError("invalid grant metadata")
        code = secrets.token_urlsafe(32)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT c.id, c.redirect_uri FROM hermes_auth_clients c "
                "JOIN instances i ON i.id = c.instance_id "
                "JOIN users u ON u.id = ? "
                "JOIN user_sessions s ON s.token_hash = ? AND s.user_id = u.id "
                "WHERE c.client_id = ? AND c.instance_id = ? "
                "AND c.revoked_at IS NULL AND i.product = 'hermes' "
                "AND i.status = 'active' AND u.status = 'active' "
                "AND s.provider = 'campus-uis' AND s.expires_at > datetime('now') "
                "AND (u.role = 'admin' OR i.owner_user_id = u.id OR EXISTS ("
                "SELECT 1 FROM instance_members m "
                "WHERE m.instance_id = i.id AND m.user_id = u.id))",
                (user_id, session_id, client_id, instance_id),
            ).fetchone()
            if not row or row[1] != redirect_uri:
                raise ValueError("invalid client")
            conn.execute(
                "INSERT INTO hermes_auth_grants "
                "(code_hash, client_id, instance_id, user_id, manager_session_id, "
                "redirect_uri, code_challenge, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sha256(code), row[0], instance_id, user_id, session_id,
                 redirect_uri, code_challenge, now, now + ttl),
            )
        return code

    def redeem(self, *, code, client_id, secret, redirect_uri, verifier, now=None):
        now = int(time.time() if now is None else now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT g.code_hash, i.public_id, u.public_id, g.redirect_uri, "
                "g.code_challenge, g.expires_at, g.consumed_at, c.client_id, "
                "c.client_secret_hash "
                "FROM hermes_auth_grants g "
                "JOIN hermes_auth_clients c ON c.id = g.client_id "
                "JOIN instances i ON i.id = g.instance_id "
                "JOIN users u ON u.id = g.user_id "
                "WHERE g.code_hash = ? AND c.client_id = ? "
                "AND g.instance_id = c.instance_id AND i.product = 'hermes' "
                "AND c.revoked_at IS NULL AND i.status = 'active' "
                "AND u.status = 'active'",
                (sha256(code), client_id),
            ).fetchone()
            secret_valid = verify_client_secret(
                secret, row[8] if row else DUMMY_SECRET_HASH
            )
            try:
                challenge_valid = hmac.compare_digest(
                    row[4] if row else "", pkce_challenge(verifier)
                )
            except (TypeError, ValueError, UnicodeError):
                challenge_valid = False
            valid = (
                row and secret_valid and not row[6] and int(row[5]) > now
                and row[3] == redirect_uri and challenge_valid
            )
            if not valid:
                conn.rollback()
                raise ValueError("invalid_grant")
            changed = conn.execute(
                "UPDATE hermes_auth_grants SET consumed_at = ? "
                "WHERE code_hash = ? AND consumed_at IS NULL", (now, row[0]),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise ValueError("invalid_grant")
            conn.commit()
            return BridgePrincipal(row[2], row[1], row[7])


class SigningKeys:
    def __init__(self, keys=None, active_kid=None):
        self.keys = dict(keys or {})
        self.active_kid = active_kid
        if not self.keys:
            raise ValueError("at least one signing key is required")
        if active_kid not in self.keys:
            raise ValueError("active signing kid is not loaded")

    @classmethod
    def from_pem_files(cls, key_files, active_kid):
        keys = {}
        for kid, path in key_files.items():
            key = serialization.load_pem_private_key(
                Path(path).read_bytes(), password=None,
            )
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"signing key {kid!r} is not Ed25519")
            keys[kid] = key
        return cls(keys, active_kid)

    @staticmethod
    def generate_private_key_pem():
        return Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def activate(self, kid):
        if kid not in self.keys:
            raise ValueError("signing key is not loaded")
        self.active_kid = kid

    def retire(self, kid):
        if kid == self.active_kid:
            raise ValueError("cannot retire active signing key")
        if self.keys.pop(kid, None) is None:
            raise ValueError("unknown signing key")

    def issue_access_token(self, principal, issuer, ttl=900, now=None):
        now = int(time.time() if now is None else now)
        return self.sign({
            "iss": issuer, "aud": principal.client_id, "sub": principal.user_id,
            "instance_id": principal.instance_id, "provider": "campus-uis",
            "iat": now, "exp": now + ttl, "jti": secrets.token_urlsafe(16),
        })

    def sign(self, claims):
        header = {"alg": "EdDSA", "kid": self.active_kid, "typ": "JWT"}
        encoded = ".".join(
            b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode())
            for part in (header, claims)
        )
        return encoded + "." + b64url(self.keys[self.active_kid].sign(encoded.encode()))

    def jwks(self):
        return {"keys": [
            {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig",
             "kid": kid, "x": b64url(key.public_key().public_bytes(
                 encoding=serialization.Encoding.Raw,
                 format=serialization.PublicFormat.Raw,
             ))}
            for kid, key in self.keys.items()
        ]}
