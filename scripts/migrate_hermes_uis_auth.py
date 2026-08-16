#!/usr/bin/env python3
"""Explicitly switch one existing Hermes instance to Manager UIS auth."""
import argparse
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "services" / "manager-web"))
sys.path.insert(0, str(ROOT_DIR / "services" / "manager-control"))
from hermes_auth_bridge import BridgeStore, verify_client_secret
from instance_adapters import stage_hermes_plugin


BASIC_KEYS = {
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
}


def load_instance(db_file, public_id):
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM instances WHERE public_id = ? AND product = 'hermes' "
            "AND status IN ('active', 'stopped')", (public_id,),
        ).fetchone()
    return dict(row) if row else None


def bridge_env(
    old_text, *, issuer, client_id, client_secret, instance_id, redirect_uri
):
    kept = [
        line for line in old_text.splitlines()
        if line.split("=", 1)[0] not in BASIC_KEYS
        and not line.startswith("HERMES_UIS_BRIDGE_")
    ]
    kept.extend([
        f"HERMES_UIS_BRIDGE_ISSUER={issuer}",
        f"HERMES_UIS_BRIDGE_CLIENT_ID={client_id}",
        f"HERMES_UIS_BRIDGE_CLIENT_SECRET={client_secret}",
        f"HERMES_UIS_BRIDGE_INSTANCE_ID={instance_id}",
        f"HERMES_UIS_BRIDGE_REDIRECT_URI={redirect_uri}",
    ])
    return "\n".join(kept) + "\n"


def env_values(text):
    return {
        key: value
        for line in text.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def run(command):
    return subprocess.run(command, text=True, capture_output=True, check=False)


def apply(instance, db_file, issuer):
    data_path = Path(instance["data_path"])
    env_file = data_path / ".env"
    config_file = data_path / "config.yaml"
    plugin = data_path / "plugins" / "campus-uis-bridge"
    source = ROOT_DIR / "templates" / "hermes" / "plugins" / "campus-uis-bridge"
    if not env_file.is_file() or not config_file.is_file() or not source.is_dir():
        raise RuntimeError("Hermes instance files or provider template are incomplete")
    parsed = urllib.parse.urlsplit(instance.get("access_url") or "")
    if parsed.scheme != "https" or not parsed.hostname or not parsed.port:
        raise RuntimeError("Hermes access_url must be an explicit HTTPS host and port")
    redirect_uri = f"https://{parsed.hostname}:{parsed.port}/auth/callback"
    old_env, old_config = env_file.read_bytes(), config_file.read_bytes()
    current_env = env_values(old_env.decode())
    with BridgeStore(db_file).connect() as conn:
        existing_client = conn.execute(
            "SELECT client_id, client_secret_hash, redirect_uri, revoked_at "
            "FROM hermes_auth_clients WHERE instance_id = ?", (instance["id"],),
        ).fetchone()
    if existing_client:
        client_id = current_env.get("HERMES_UIS_BRIDGE_CLIENT_ID", "")
        client_secret = current_env.get("HERMES_UIS_BRIDGE_CLIENT_SECRET", "")
        if (
            existing_client[3] is not None
            or existing_client[0] != client_id
            or existing_client[2] != redirect_uri
            or not verify_client_secret(client_secret, existing_client[1])
        ):
            raise RuntimeError("existing Hermes Bridge client does not match instance config")
    else:
        client_id = secrets.token_urlsafe(24)
        client_secret = secrets.token_urlsafe(48)
    backup = data_path.parent.parent / ".manager-auth-backups" / (
        "hermes-uis-" + instance["public_id"] + "-"
        + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    (backup / ".env").write_bytes(old_env)
    (backup / ".env").chmod(0o600)
    (backup / "config.yaml").write_bytes(old_config)
    (backup / "config.yaml").chmod(0o600)
    old_plugin = plugin.parent / f".{plugin.name}.pre-uis"
    if old_plugin.exists():
        raise RuntimeError(f"rollback path already exists: {old_plugin}")
    created_client = False
    plugin_parent_created = not plugin.parent.exists()
    plugin_parent_stat = (
        plugin.parent.stat(follow_symlinks=False)
        if not plugin_parent_created else None
    )
    try:
        if plugin.exists():
            shutil.move(plugin, old_plugin)
        plugin.parent.mkdir(parents=True, exist_ok=True)
        data_stat = data_path.stat()
        stage_hermes_plugin(source, plugin, data_stat.st_uid, data_stat.st_gid)
        env_file.write_text(bridge_env(
            old_env.decode(), issuer=issuer, client_id=client_id,
            client_secret=client_secret, instance_id=instance["public_id"],
            redirect_uri=redirect_uri,
        ), encoding="utf-8")
        env_file.chmod(0o600)
        enabled = run([
            "docker", "exec", instance["runtime_identifier"],
            "hermes", "plugins", "enable", "campus-uis-bridge",
        ])
        if enabled.returncode != 0:
            raise RuntimeError(enabled.stderr.strip() or "could not enable Hermes UIS provider")
        if existing_client is None:
            BridgeStore(db_file).create_client(
                instance["id"], client_id, client_secret, redirect_uri,
            )
            created_client = True
        restarted = run(["docker", "restart", instance["runtime_identifier"]])
        if restarted.returncode != 0:
            raise RuntimeError(restarted.stderr.strip() or "could not restart Hermes")
        if old_plugin.exists():
            shutil.rmtree(old_plugin)
        print(f"[INFO] Backup: {backup}")
    except Exception:
        env_file.write_bytes(old_env)
        config_file.write_bytes(old_config)
        shutil.rmtree(plugin, ignore_errors=True)
        if old_plugin.exists():
            shutil.move(old_plugin, plugin)
        if plugin_parent_created:
            try:
                plugin.parent.rmdir()
            except OSError:
                pass
        elif plugin_parent_stat is not None:
            os.chown(
                plugin.parent, plugin_parent_stat.st_uid, plugin_parent_stat.st_gid,
                follow_symlinks=False,
            )
            plugin.parent.chmod(plugin_parent_stat.st_mode & 0o777)
        if created_client:
            with BridgeStore(db_file).connect() as conn:
                conn.execute("DELETE FROM hermes_auth_clients WHERE instance_id = ?", (instance["id"],))
        run(["docker", "restart", instance["runtime_identifier"]])
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--instance", required=True, help="exact Hermes instance public UUID")
    parser.add_argument("--issuer", default=os.environ.get("HERMES_AUTH_BRIDGE_ISSUER", ""))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    instance = load_instance(args.db, args.instance)
    if instance is None:
        parser.error("active or stopped Hermes instance not found")
    issuer = args.issuer.strip().rstrip("/")
    if not issuer.startswith("https://"):
        parser.error("--issuer must use HTTPS")
    print(f"[PLAN] switch Hermes instance {instance['public_id']} to campus-uis-bridge")
    if not args.apply:
        print("[INFO] Preview completed; no files, clients, or containers were changed")
        return 0
    if os.geteuid() != 0:
        parser.error("--apply must be run as root")
    try:
        apply(instance, args.db, issuer)
    except Exception as exc:
        print(f"[ERROR] migration failed and was rolled back: {exc}", file=sys.stderr)
        return 1
    print("[INFO] Hermes UIS authentication enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
