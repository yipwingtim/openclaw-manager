#!/usr/bin/env python3

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
MANAGER_DIR = SCRIPT_DIR.parent
CONFIG_FILE = MANAGER_DIR / "config" / "openclaw-manager.env"
METADATA_STORE_FILE = MANAGER_DIR / "services" / "manager-web" / "metadata_store.py"


def load_env_file(path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_metadata_store():
    spec = importlib.util.spec_from_file_location("metadata_store", METADATA_STORE_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load metadata_store from {METADATA_STORE_FILE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


load_env_file(CONFIG_FILE)
os.environ.setdefault("OPENCLAW_MANAGER_DIR", str(MANAGER_DIR))
OPENCLAW_PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
NGINX_USERS_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
os.environ.setdefault("METADATA_SCHEMA_FILE", str(MANAGER_DIR / "db" / "schema.sql"))
os.environ.setdefault("METADATA_DB_FILE", str(OPENCLAW_PUBLIC_DIR / "manager.db"))

metadata_store = load_metadata_store()


def bool_arg(value):
    normalized = (value or "").strip().lower()
    if normalized in {"true", "yes", "y", "1", "on", "enabled"}:
        return True
    if normalized in {"false", "no", "n", "0", "off", "disabled"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def detect_port(user_id):
    nginx_conf = NGINX_USERS_CONF_DIR / f"{user_id}.conf"
    if nginx_conf.is_file():
        for line in nginx_conf.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = re.match(r"^\s*listen\s+([0-9]+)\b", line)
            if match:
                return int(match.group(1))
    return None


def detect_openclaw_version(user_id):
    compose_file = OPENCLAW_PUBLIC_DIR / "users" / user_id / "docker-compose.yml"
    if not compose_file.is_file():
        return None
    text = compose_file.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"image:\s*ghcr\.io/openclaw/openclaw:([^\s]+)", text)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def detect_basic_auth_enabled(user_id):
    nginx_conf = NGINX_USERS_CONF_DIR / f"{user_id}.conf"
    if not nginx_conf.is_file():
        return None
    text = nginx_conf.read_text(encoding="utf-8", errors="ignore")
    if "auth_basic off;" in text:
        return False
    if 'auth_basic "OpenClaw Login";' in text:
        return True
    return None


def build_access_url(port):
    if not port or not PUBLIC_HOST:
        return None
    return f"https://{PUBLIC_HOST}:{port}"


def initialize_metadata():
    metadata_store.initialize(schema_file=MANAGER_DIR / "db" / "schema.sql")


def upsert_instance(
    *,
    user_id,
    status=None,
    port=None,
    openclaw_version=None,
    basic_auth_enabled=None,
    deleted_at=None,
    conn=None,
):
    existing = metadata_store.get_instance(user_id, conn=conn) or {}
    resolved_port = port if port is not None else detect_port(user_id)
    if resolved_port is None:
        resolved_port = existing.get("port")

    resolved_version = openclaw_version or existing.get("openclaw_version") or detect_openclaw_version(user_id)
    detected_basic_auth = detect_basic_auth_enabled(user_id)
    if basic_auth_enabled is None:
        if detected_basic_auth is None:
            basic_auth_enabled = existing.get("basic_auth_enabled", 1) != 0
        else:
            basic_auth_enabled = detected_basic_auth

    access_url = existing.get("access_url") or build_access_url(resolved_port)
    admin_url = existing.get("admin_url") or (access_url.rstrip("/") + "/admin/" if access_url else None)

    metadata_store.upsert_instance(
        user_id=user_id,
        product=existing.get("product") or "openclaw",
        port=resolved_port,
        status=status or existing.get("status") or "active",
        openclaw_version=resolved_version,
        basic_auth_enabled=basic_auth_enabled,
        container_name=existing.get("container_name") or f"openclaw_{user_id}",
        access_url=access_url,
        admin_url=admin_url,
        data_path=existing.get("data_path") or str(OPENCLAW_PUBLIC_DIR / "users" / user_id),
        nginx_conf_path=existing.get("nginx_conf_path") or str(NGINX_USERS_CONF_DIR / f"{user_id}.conf"),
        deleted_at=deleted_at if deleted_at is not None else existing.get("deleted_at"),
        conn=conn,
    )
    return resolved_port


def record_operation(args):
    initialize_metadata()
    metadata_store.record_operation(
        action=args.action,
        status=args.status,
        actor=args.actor,
        user_id=args.user_id,
        message=args.message,
        finished_at=metadata_store.utc_now(),
    )


def create_instance(args):
    initialize_metadata()
    with metadata_store.connect() as conn:
        port = upsert_instance(
            user_id=args.user_id,
            status="active",
            port=args.port,
            openclaw_version=args.openclaw_version,
            basic_auth_enabled=args.basic_auth_enabled,
            deleted_at=None,
            conn=conn,
        )
        metadata_store.upsert_credentials(
            user_id=args.user_id,
            basic_auth_username=args.user_id if args.basic_auth_password_ref else None,
            basic_auth_password_ref=args.basic_auth_password_ref,
            openclaw_token=args.openclaw_token,
            conn=conn,
        )
        if port is not None:
            metadata_store.record_port(port, user_id=args.user_id, status="allocated", conn=conn)
        metadata_store.record_operation(
            action="create_instance",
            status="success",
            actor=args.actor,
            user_id=args.user_id,
            message=args.message,
            finished_at=metadata_store.utc_now(),
            conn=conn,
        )


def set_instance_status(args):
    initialize_metadata()
    deleted_at = metadata_store.utc_now() if args.status == "deleted" else None
    with metadata_store.connect() as conn:
        port = upsert_instance(
            user_id=args.user_id,
            status=args.status,
            port=args.port,
            openclaw_version=args.openclaw_version,
            deleted_at=deleted_at,
            conn=conn,
        )
        if port is not None:
            metadata_store.record_port(
                port,
                user_id=None if args.status == "deleted" else args.user_id,
                status="released" if args.status == "deleted" else "allocated",
                conn=conn,
            )
        metadata_store.record_operation(
            action=args.action,
            status="success",
            actor=args.actor,
            user_id=args.user_id,
            message=args.message,
            finished_at=metadata_store.utc_now(),
            conn=conn,
        )


def set_basic_auth(args):
    initialize_metadata()
    with metadata_store.connect() as conn:
        existing_credentials = metadata_store.get_credentials(args.user_id, conn=conn) or {}
        upsert_instance(
            user_id=args.user_id,
            basic_auth_enabled=args.enabled,
            conn=conn,
        )
        metadata_store.upsert_credentials(
            user_id=args.user_id,
            basic_auth_username=existing_credentials.get("basic_auth_username") or args.user_id,
            basic_auth_password_ref=args.basic_auth_password_ref or existing_credentials.get("basic_auth_password_ref"),
            openclaw_token=existing_credentials.get("openclaw_token"),
            conn=conn,
        )
        metadata_store.record_operation(
            action="set_basic_auth",
            status="success",
            actor=args.actor,
            user_id=args.user_id,
            message=f"enabled={str(args.enabled).lower()}",
            finished_at=metadata_store.utc_now(),
            conn=conn,
        )


def update_version(args):
    initialize_metadata()
    with metadata_store.connect() as conn:
        upsert_instance(
            user_id=args.user_id,
            openclaw_version=args.openclaw_version,
            conn=conn,
        )
        metadata_store.record_operation(
            action="update_version",
            status="success",
            actor=args.actor,
            user_id=args.user_id,
            message=f"version={args.openclaw_version}",
            finished_at=metadata_store.utc_now(),
            conn=conn,
        )


def build_parser():
    parser = argparse.ArgumentParser(description="Write OpenClaw Manager metadata records.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record-operation")
    record.add_argument("--action", required=True)
    record.add_argument("--status", default="success", choices=["success", "failed", "skipped", "running"])
    record.add_argument("--user-id")
    record.add_argument("--actor")
    record.add_argument("--message")
    record.set_defaults(func=record_operation)

    create = subparsers.add_parser("create-instance")
    create.add_argument("--user-id", required=True)
    create.add_argument("--port", type=int)
    create.add_argument("--openclaw-version")
    create.add_argument("--basic-auth-enabled", type=bool_arg, default=True)
    create.add_argument("--basic-auth-password-ref")
    create.add_argument("--openclaw-token")
    create.add_argument("--actor")
    create.add_argument("--message")
    create.set_defaults(func=create_instance)

    status = subparsers.add_parser("set-instance-status")
    status.add_argument("--user-id", required=True)
    status.add_argument("--status", required=True, choices=["active", "stopped", "deleted", "failed"])
    status.add_argument("--action", required=True)
    status.add_argument("--port", type=int)
    status.add_argument("--openclaw-version")
    status.add_argument("--actor")
    status.add_argument("--message")
    status.set_defaults(func=set_instance_status)

    auth = subparsers.add_parser("set-basic-auth")
    auth.add_argument("--user-id", required=True)
    auth.add_argument("--enabled", required=True, type=bool_arg)
    auth.add_argument("--basic-auth-password-ref")
    auth.add_argument("--actor")
    auth.set_defaults(func=set_basic_auth)

    version = subparsers.add_parser("update-version")
    version.add_argument("--user-id", required=True)
    version.add_argument("--openclaw-version", required=True)
    version.add_argument("--actor")
    version.set_defaults(func=update_version)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[WARN] Metadata update failed: {exc}", file=sys.stderr)
        sys.exit(1)
