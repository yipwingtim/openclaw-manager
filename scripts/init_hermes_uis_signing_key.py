#!/usr/bin/env python3

import argparse
import datetime
import os
import shutil
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def valid_key(path):
    try:
        return isinstance(
            serialization.load_pem_private_key(path.read_bytes(), password=None),
            ed25519.Ed25519PrivateKey,
        )
    except (OSError, TypeError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Initialize the Hermes UIS bridge signing key")
    parser.add_argument("--config", default="config/openclaw-manager.env")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--kid", required=True)
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config, key = Path(args.config), Path(args.key_file)

    try:
        key_mode = key.lstat().st_mode
    except FileNotFoundError:
        key_mode = None
    if key_mode is not None and (stat.S_ISLNK(key_mode) or not stat.S_ISREG(key_mode)):
        print("[ERROR] Existing signing key must be a regular file, not a symlink", file=sys.stderr)
        return 1
    if key_mode is not None and not valid_key(key):
        print("[ERROR] Existing signing key is invalid; refusing to overwrite it", file=sys.stderr)
        return 1
    if not args.apply:
        print(f"[PREVIEW] Would ensure an Ed25519 signing key and configure kid {args.kid}")
        return 0

    if key_mode is None:
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        contents = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key.parent.chmod(0o700)
        os.chown(key.parent, args.uid, args.gid)
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(contents)
    key.parent.chmod(0o700)
    key.chmod(0o600)
    os.chown(key.parent, args.uid, args.gid)
    os.chown(key, args.uid, args.gid)

    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    settings = {
        "HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE": str(key),
        "HERMES_AUTH_BRIDGE_SIGNING_KEY_FILE": "/run/secrets/hermes-auth-bridge-ed25519.pem",
        "HERMES_AUTH_BRIDGE_ACTIVE_KID": args.kid,
    }
    lines = existing.splitlines()
    changed = False
    for name, value in settings.items():
        replacement = f"{name}={value}"
        index = next((i for i, line in enumerate(lines) if line.startswith(f"{name}=")), None)
        if index is None:
            lines.append(replacement)
            changed = True
        elif lines[index] != replacement:
            lines[index] = replacement
            changed = True
    if changed:
        if config.exists():
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
            shutil.copy2(config, config.with_name(f"{config.name}.bak.{timestamp}"))
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("[OK] Hermes UIS bridge signing key is initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
