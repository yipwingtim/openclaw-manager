#!/usr/bin/env python3

import ipaddress
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


EXPECTED_KEY_FILE = "/run/secrets/hermes-auth-bridge-ed25519.pem"


def fail(message):
    print(f"[ERROR] {message}", file=sys.stderr)
    return False


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"{name} is required")
        return None
    if value == "/dev/null":
        fail(f"{name} must not use /dev/null")
        return None
    return value


def regular_file(name, value):
    path = Path(value)
    try:
        mode = path.lstat().st_mode
    except OSError:
        fail(f"{name} is not a readable file")
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or not os.access(path, os.R_OK):
        fail(f"{name} must be a readable regular file, not a symlink")
        return None
    return path


def load_certificate(path, name):
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        fail(f"{name} is not a valid PEM certificate")
        return None


def main():
    valid = True
    issuer_value = required("HERMES_AUTH_BRIDGE_ISSUER")
    key_host_value = required("HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE")
    key_file = required("HERMES_AUTH_BRIDGE_SIGNING_KEY_FILE")
    kid = required("HERMES_AUTH_BRIDGE_ACTIVE_KID")
    ca_value = required("HERMES_AUTH_BRIDGE_CA_HOST_FILE")
    cert_value = required("NGINX_SSL_CERT")
    valid = all((issuer_value, key_host_value, key_file, kid, ca_value, cert_value))

    parsed = urlparse(issuer_value or "")
    if parsed.scheme != "https" or not parsed.hostname:
        valid = fail("HERMES_AUTH_BRIDGE_ISSUER must be an HTTPS URL with a host") and valid
    else:
        try:
            socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        except (OSError, ValueError):
            valid = fail("HERMES_AUTH_BRIDGE_ISSUER host and port must resolve") and valid

    if key_file and key_file != EXPECTED_KEY_FILE:
        valid = fail(f"HERMES_AUTH_BRIDGE_SIGNING_KEY_FILE must be {EXPECTED_KEY_FILE}") and valid

    key_path = regular_file("HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE", key_host_value) if key_host_value else None
    if key_host_value and not key_path:
        valid = False
    if key_path:
        if key_path.stat().st_mode & 0o077:
            valid = fail("Hermes bridge signing key permissions must not be wider than 0600") and valid
        try:
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            if not isinstance(key, ed25519.Ed25519PrivateKey):
                valid = fail("Hermes bridge signing key must be Ed25519") and valid
        except TypeError:
            valid = fail("Hermes bridge signing key must be unencrypted PKCS#8 PEM") and valid
        except (OSError, ValueError):
            valid = fail("Hermes bridge signing key must be a valid unencrypted PKCS#8 PEM") and valid

    ca_path = regular_file("HERMES_AUTH_BRIDGE_CA_HOST_FILE", ca_value) if ca_value else None
    if ca_value and not ca_path:
        valid = False
    ca = load_certificate(ca_path, "HERMES_AUTH_BRIDGE_CA_HOST_FILE") if ca_path else None
    if ca_path and b"PRIVATE KEY" in ca_path.read_bytes():
        valid = fail("HERMES_AUTH_BRIDGE_CA_HOST_FILE must not contain a private key") and valid

    cert_path_value = cert_value
    if cert_value and cert_value.startswith("/etc/nginx/certs/"):
        cert_path_value = str(Path(os.environ.get("NGINX_CERTS_DIR", "/data/docker/nginx/certs")) / Path(cert_value).name)
    cert_path = regular_file("NGINX_SSL_CERT", cert_path_value) if cert_path_value else None
    if cert_path_value and not cert_path:
        valid = False
    leaf = load_certificate(cert_path, "NGINX_SSL_CERT") if cert_path else None
    if ca and leaf:
        host = parsed.hostname
        try:
            ipaddress.ip_address(host)
            identity_option = "-verify_ip"
        except ValueError:
            identity_option = "-verify_hostname"
        result = subprocess.run(
            ["openssl", "verify", "-CAfile", str(ca_path), "-untrusted", str(cert_path),
             identity_option, host, str(cert_path)],
            text=True, capture_output=True,
        )
        if result.returncode:
            valid = fail("NGINX_SSL_CERT chain or SAN does not validate for the issuer host") and valid

    if valid:
        print("[OK] Hermes UIS readiness checks passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
