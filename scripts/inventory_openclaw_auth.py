#!/usr/bin/env python3

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

import check_metadata_consistency as consistency


PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
DB_FILE = Path(os.environ.get("METADATA_DB_FILE", PUBLIC_DIR / "manager.db"))
NGINX_USERS_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))


def instances(db_file):
    with sqlite3.connect(db_file) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            """
            SELECT i.public_id, i.legacy_user_id, i.instance_name, i.status,
                   i.data_path, i.nginx_conf_path, u.username AS owner_username
            FROM instances i
            JOIN users u ON u.id = i.owner_user_id
            WHERE i.product = 'openclaw' AND i.status != 'deleted'
            ORDER BY u.normalized_username, i.instance_name
            """
        )]


def managed_config_path(instance, public_dir):
    data_path = Path(instance.get("data_path") or "").resolve()
    data_path.relative_to(public_dir.resolve())
    return data_path / "config" / "openclaw.json"


def nginx_path(instance, nginx_dir):
    configured = instance.get("nginx_conf_path")
    if configured:
        path = Path(configured).resolve()
        path.relative_to(nginx_dir.resolve())
        if path.is_file():
            return path
    user_id = instance.get("legacy_user_id")
    if not user_id:
        return nginx_dir / f"openclaw-{instance['public_id']}.conf"
    candidates = (
        nginx_dir / f"{user_id}.conf",
        nginx_dir / "_disabled" / f"{user_id}.conf",
        Path(f"{nginx_dir}.disabled") / f"{user_id}.conf",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def inspect_instance(instance, public_dir=PUBLIC_DIR, nginx_dir=NGINX_USERS_CONF_DIR):
    issues = []
    try:
        config_path = managed_config_path(instance, public_dir)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        gateway = config.get("gateway") or {}
        auth = gateway.get("auth") or {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return result(instance, "unknown", "inconsistent", False, None, ["config_unreadable"])

    mode = auth.get("mode", "token")
    if mode not in {"token", "trusted-proxy"}:
        return result(instance, str(mode), "inconsistent", False, None, ["unsupported_auth_mode"])

    try:
        nginx = consistency.detect_nginx_conf(nginx_path(instance, nginx_dir))
    except (OSError, ValueError):
        return result(instance, mode, "inconsistent", mode == "token", None, ["nginx_path_invalid"])
    compose = consistency.detect_compose(Path(instance["data_path"]) / "docker-compose.yml")
    if not nginx["exists"]:
        issues.append("nginx_missing")
    if mode == "token" and not auth.get("token") and not compose["has_gateway_token_env"]:
        issues.append("gateway_token_missing")
    if mode == "trusted-proxy":
        if not nginx["instance_auth"]:
            issues.append("instance_auth_missing")
        if not nginx["trusted_identity_headers"]:
            issues.append("trusted_identity_headers_missing")
        if auth.get("trustedProxy", {}).get("userHeader") != "x-forwarded-user":
            issues.append("user_header_mismatch")
        if len(gateway.get("trustedProxies") or []) != 1:
            issues.append("trusted_proxies_invalid")
        if compose["has_gateway_token_env"]:
            issues.append("gateway_token_conflict")
        if not auth.get("password"):
            issues.append("control_password_missing")
        if nginx["basic_auth_enabled"] is not False:
            issues.append("basic_auth_not_disabled")

    blocking_issues = [issue for issue in issues if issue != "basic_auth_not_disabled"]
    status = (
        "inconsistent" if blocking_issues
        else "ready" if mode == "trusted-proxy" and not issues
        else "needs-migration"
    )
    return result(instance, mode, status, mode == "token", nginx["basic_auth_enabled"], issues)


def result(instance, mode, migration_status, requires_token, nginx_basic_auth, issues):
    return {
        "instance_public_id": instance["public_id"],
        "instance_name": instance["instance_name"],
        "owner": instance["owner_username"],
        "runtime_status": instance["status"],
        "auth_mode": mode,
        "requires_openclaw_token": "yes" if requires_token else "no",
        "nginx_basic_auth": "unknown" if nginx_basic_auth is None else "enabled" if nginx_basic_auth else "disabled",
        "migration_status": migration_status,
        "issues": ",".join(issues) or "-",
    }


def print_table(rows, output):
    fields = ("owner", "instance_name", "runtime_status", "auth_mode", "requires_openclaw_token", "nginx_basic_auth", "migration_status", "issues")
    widths = {field: max(len(field), *(len(str(row[field])) for row in rows)) for field in fields}
    print("  ".join(field.ljust(widths[field]) for field in fields), file=output)
    for row in rows:
        print("  ".join(str(row[field]).ljust(widths[field]) for field in fields), file=output)


def main(argv=None, output=sys.stdout):
    parser = argparse.ArgumentParser(description="Inventory OpenClaw token and trusted-proxy authentication modes")
    parser.add_argument("--db", type=Path, default=DB_FILE)
    parser.add_argument("--public-dir", type=Path, default=PUBLIC_DIR)
    parser.add_argument("--nginx-dir", type=Path, default=NGINX_USERS_CONF_DIR)
    parser.add_argument("--format", choices=("table", "csv"), default="table")
    args = parser.parse_args(argv)
    try:
        rows = [inspect_instance(item, args.public_dir, args.nginx_dir) for item in instances(args.db)]
    except (OSError, sqlite3.Error) as exc:
        print(f"[ERROR] inventory failed: {exc}", file=output)
        return 1
    if args.format == "csv":
        writer = csv.DictWriter(output, fieldnames=rows[0].keys() if rows else (
            "instance_public_id", "instance_name", "owner", "runtime_status", "auth_mode",
            "requires_openclaw_token", "nginx_basic_auth", "migration_status", "issues",
        ))
        writer.writeheader()
        writer.writerows(rows)
    else:
        print_table(rows, output)
        counts = {status: sum(row["migration_status"] == status for row in rows) for status in ("ready", "needs-migration", "inconsistent")}
        print(f"[SUMMARY] total={len(rows)} ready={counts['ready']} needs-migration={counts['needs-migration']} inconsistent={counts['inconsistent']}", file=output)
    return 1 if any(row["migration_status"] == "inconsistent" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
