#!/usr/bin/env python3

import datetime
import ipaddress
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_hermes_uis_readiness.py"
INIT = ROOT / "scripts" / "init_hermes_uis_signing_key.py"


def write_tls_pair(root, host="localhost", *, include_san=True, ip_san=False):
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_name).public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if include_san:
        san = x509.IPAddress(ipaddress.ip_address(host)) if ip_san else x509.DNSName(host)
        builder = builder.add_extension(x509.SubjectAlternativeName([san]), critical=False)
    leaf = builder.sign(ca_key, hashes.SHA256())
    ca_path, leaf_path = root / "ca.crt", root / "leaf.crt"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    return ca_path, leaf_path


def write_signing_key(path, *, encrypted=False, rsa_key=False):
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if rsa_key else ed25519.Ed25519PrivateKey.generate()
    )
    encryption = serialization.BestAvailableEncryption(b"secret") if encrypted else serialization.NoEncryption()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption))
    path.chmod(0o600)


class HermesUisReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ca, self.leaf = write_tls_pair(self.root)
        self.key = self.root / "bridge.pem"
        write_signing_key(self.key)

    def tearDown(self):
        self.temp.cleanup()

    def run_checker(self, **overrides):
        env = {
            "HERMES_AUTH_BRIDGE_ISSUER": "https://localhost:30015/auth/hermes",
            "HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE": str(self.key),
            "HERMES_AUTH_BRIDGE_SIGNING_KEY_FILE": "/run/secrets/hermes-auth-bridge-ed25519.pem",
            "HERMES_AUTH_BRIDGE_ACTIVE_KID": "current",
            "HERMES_AUTH_BRIDGE_CA_HOST_FILE": str(self.ca),
            "NGINX_SSL_CERT": str(self.leaf),
        }
        env.update(overrides)
        return subprocess.run([sys.executable, str(CHECKER)], env={**os.environ, **env}, text=True, capture_output=True)

    def test_accepts_valid_configuration_without_printing_secret_material(self):
        result = self.run_checker(SENSITIVE_PASSWORD="must-not-appear")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Hermes UIS readiness checks passed", result.stdout)
        self.assertNotIn("must-not-appear", result.stdout + result.stderr)

    def test_rejects_missing_values_and_dev_null_fallbacks(self):
        for name in ("HERMES_AUTH_BRIDGE_ISSUER", "HERMES_AUTH_BRIDGE_ACTIVE_KID", "HERMES_AUTH_BRIDGE_CA_HOST_FILE", "HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE"):
            with self.subTest(name=name):
                result = self.run_checker(**{name: ""})
                self.assertNotEqual(result.returncode, 0)
        result = self.run_checker(HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE="/dev/null")
        self.assertNotEqual(result.returncode, 0)

    def test_validates_active_kid_in_multi_key_configuration(self):
        valid = self.run_checker(
            HERMES_AUTH_BRIDGE_SIGNING_KEYS=(
                "current=/run/secrets/hermes-auth-bridge-ed25519.pem"
            )
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        for configured in (
            "old=/run/secrets/old.pem",
            "old=/run/secrets/old.pem,current=/run/secrets/hermes-auth-bridge-ed25519.pem",
        ):
            with self.subTest(configured=configured):
                invalid = self.run_checker(HERMES_AUTH_BRIDGE_SIGNING_KEYS=configured)
                self.assertNotEqual(invalid.returncode, 0)

    def test_rejects_invalid_signing_keys_and_permissions(self):
        cases = (({"rsa_key": True}, "Ed25519"), ({"encrypted": True}, "unencrypted"))
        for options, message in cases:
            with self.subTest(options=options):
                write_signing_key(self.key, **options)
                result = self.run_checker()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        write_signing_key(self.key)
        self.key.chmod(0o644)
        self.assertNotEqual(self.run_checker().returncode, 0)

    def test_rejects_symlinked_signing_key(self):
        link = self.root / "linked.pem"
        link.symlink_to(self.key)
        self.assertNotEqual(
            self.run_checker(HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE=str(link)).returncode,
            0,
        )

    def test_rejects_ca_with_private_key_invalid_ca_and_chain_mismatch(self):
        self.ca.write_bytes(self.ca.read_bytes() + self.key.read_bytes())
        self.assertNotEqual(self.run_checker().returncode, 0)
        self.ca.write_text("not a certificate", encoding="utf-8")
        self.assertNotEqual(self.run_checker().returncode, 0)
        ca_a, leaf_a = write_tls_pair(self.root / "a")
        ca_b, _ = write_tls_pair(self.root / "b")
        self.ca, self.leaf = ca_b, leaf_a
        self.assertNotEqual(self.run_checker().returncode, 0)

    def test_rejects_certificate_without_matching_san(self):
        _, wrong_leaf = write_tls_pair(self.root, "wrong.example")
        result = self.run_checker(NGINX_SSL_CERT=str(wrong_leaf))
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_invalid_certificate_and_missing_san(self):
        self.leaf.write_text("not a certificate", encoding="utf-8")
        self.assertNotEqual(self.run_checker().returncode, 0)
        _, no_san = write_tls_pair(self.root, include_san=False)
        self.assertNotEqual(self.run_checker(NGINX_SSL_CERT=str(no_san)).returncode, 0)

    def test_checks_ip_san(self):
        self.ca, self.leaf = write_tls_pair(self.root, "127.0.0.1", ip_san=True)
        good = self.run_checker(HERMES_AUTH_BRIDGE_ISSUER="https://127.0.0.1/auth/hermes")
        self.assertEqual(good.returncode, 0, good.stderr)
        bad = self.run_checker(HERMES_AUTH_BRIDGE_ISSUER="https://127.0.0.2/auth/hermes")
        self.assertNotEqual(bad.returncode, 0)

    def test_maps_nested_nginx_certificate_path(self):
        nested = self.root / "certs" / "site"
        nested.mkdir(parents=True)
        target = nested / "fullchain.pem"
        target.write_bytes(self.leaf.read_bytes())
        result = self.run_checker(
            NGINX_SSL_CERT="/etc/nginx/certs/site/fullchain.pem",
            NGINX_CERTS_DIR=str(self.root / "certs"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class HermesUisSigningKeyInitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "manager.env"
        self.key = self.root / "secrets" / "bridge.pem"
        self.config.write_text("PUBLIC_HOST=manager.example.test\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_init(self, *args):
        return subprocess.run([
            sys.executable, str(INIT), "--config", str(self.config),
            "--key-file", str(self.key), "--kid", "current", *args,
        ], text=True, capture_output=True)

    def test_preview_does_not_write(self):
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.key.exists())
        self.assertEqual(self.config.read_text(), "PUBLIC_HOST=manager.example.test\n")

    def test_apply_generates_pkcs8_key_updates_config_and_is_idempotent(self):
        first = self.run_init("--apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        original = self.key.read_bytes()
        loaded = serialization.load_pem_private_key(original, password=None)
        self.assertIsInstance(loaded, ed25519.Ed25519PrivateKey)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(list(self.root.glob("manager.env.bak.*")))
        config = self.config.read_text()
        self.assertIn(f"HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE={self.key}", config)
        self.assertIn("HERMES_AUTH_BRIDGE_ACTIVE_KID=current", config)
        second = self.run_init("--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.key.read_bytes(), original)

    def test_apply_secures_existing_parent_before_creating_key(self):
        self.key.parent.mkdir(mode=0o755)
        result = self.run_init("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.key.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)

    def test_apply_refuses_to_overwrite_existing_invalid_file(self):
        self.key.parent.mkdir()
        self.key.write_text("do not overwrite", encoding="utf-8")
        result = self.run_init("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.key.read_text(), "do not overwrite")

    def test_apply_refuses_symlinked_key(self):
        target = self.root / "target.pem"
        write_signing_key(target)
        self.key.parent.mkdir()
        self.key.symlink_to(target)
        result = self.run_init("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_apply_preserves_existing_valid_key_and_tightens_permissions(self):
        self.key.parent.mkdir()
        write_signing_key(self.key)
        original = self.key.read_bytes()
        self.key.chmod(0o644)
        result = self.run_init("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.key.read_bytes(), original)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)

    def test_apply_backs_up_and_replaces_stale_config_values(self):
        self.config.write_text(
            "HERMES_AUTH_BRIDGE_ACTIVE_KID=old\n"
            "HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE=/old/key.pem\n",
            encoding="utf-8",
        )
        result = self.run_init("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.config.read_text()
        self.assertIn("HERMES_AUTH_BRIDGE_ACTIVE_KID=current", config)
        self.assertIn(f"HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE={self.key}", config)
        self.assertNotIn("HERMES_AUTH_BRIDGE_ACTIVE_KID=old", config)
        self.assertTrue(list(self.root.glob("manager.env.bak.*")))


if __name__ == "__main__":
    unittest.main()
