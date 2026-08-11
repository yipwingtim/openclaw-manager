#!/usr/bin/env python3

import argparse
import json
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
DB_FILE = Path(os.environ.get("METADATA_DB_FILE", PUBLIC_DIR / "manager.db"))


def instances():
    with sqlite3.connect(DB_FILE) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT public_id, legacy_user_id, data_path, status
            FROM instances
            WHERE product = 'openclaw' AND status IN ('active', 'stopped')
            ORDER BY public_id
            """
        ).fetchall()


def config_path(instance):
    data_path = instance["data_path"]
    if not data_path:
        raise ValueError("metadata data_path is missing")
    path = Path(data_path).resolve() / "config" / "openclaw.json"
    public_root = PUBLIC_DIR.resolve()
    relative = path.relative_to(public_root)
    if len(relative.parts) < 4 or relative.parts[:2] != ("instances", "openclaw"):
        raise ValueError(f"data_path is outside managed OpenClaw instances: {data_path}")
    return path


def add_password(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    auth = config.setdefault("gateway", {}).setdefault("auth", {})
    if auth.get("mode") != "trusted-proxy":
        return False
    if auth.get("password"):
        return False
    auth["password"] = secrets.token_hex(24)
    temporary = path.with_name(f".{path.name}.trusted-proxy.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Add local CLI password to trusted-proxy OpenClaw instances")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    pending = []
    for instance in instances():
        try:
            path = config_path(instance)
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[ERROR] {instance['public_id']}: {exc}")
            continue
        auth = config.get("gateway", {}).get("auth", {})
        if auth.get("mode") == "trusted-proxy" and not auth.get("password"):
            pending.append((instance, path))
            print(f"[PLAN] {instance['public_id']}: {path}")
    if not args.apply:
        print(f"[INFO] {len(pending)} config(s) require migration; rerun with --apply")
        return 0
    if not pending:
        print("[INFO] 0 config(s) require migration")
        return 0
    backup = PUBLIC_DIR / ".manager-auth-backups" / (
        "trusted-proxy-password-" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    backup.mkdir(parents=True, exist_ok=False)
    for instance, path in pending:
        shutil.copy2(path, backup / f"{instance['public_id']}.openclaw.json")
    try:
        for instance, path in pending:
            if add_password(path):
                print(f"[OK] {instance['public_id']}: password added")
    except Exception:
        for instance, path in pending:
            original = backup / f"{instance['public_id']}.openclaw.json"
            shutil.copy2(original, path)
        raise
    print(f"[INFO] Backup: {backup}")
    print("[INFO] Restart each migrated OpenClaw instance during an approved maintenance window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
