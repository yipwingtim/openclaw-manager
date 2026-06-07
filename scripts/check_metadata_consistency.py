#!/usr/bin/env python3

import argparse
import csv
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
MANAGER_DIR = SCRIPT_DIR.parent
CONFIG_FILE = MANAGER_DIR / "config" / "openclaw-manager.env"


def load_env_file(path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(CONFIG_FILE)

OPENCLAW_PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
USERS_CSV = Path(os.environ.get("USERS_CSV", OPENCLAW_PUBLIC_DIR / "users.csv"))
PORT_FILE = Path(os.environ.get("PORT_FILE", OPENCLAW_PUBLIC_DIR / "ports.txt"))
METADATA_DB_FILE = Path(os.environ.get("METADATA_DB_FILE", OPENCLAW_PUBLIC_DIR / "manager.db"))
NGINX_USERS_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
NGINX_COMPOSE_FILE = Path(os.environ.get("NGINX_COMPOSE_FILE", "/data/docker/nginx/compose/docker-compose.yml"))
NGINX_HTPASSWD_FILE_IN_CONTAINER = os.environ.get(
    "NGINX_HTPASSWD_FILE_IN_CONTAINER",
    "/etc/nginx/auth/.htpasswd",
)
NGINX_AUTH_DIR = Path(os.environ.get("NGINX_AUTH_DIR", "/data/docker/nginx/auth"))


@dataclass
class Issue:
    level: str
    code: str
    message: str


class Reporter:
    def __init__(self):
        self.issues = []

    def add(self, level, code, message):
        self.issues.append(Issue(level=level, code=code, message=message))

    def error(self, code, message):
        self.add("ERROR", code, message)

    def warn(self, code, message):
        self.add("WARN", code, message)

    def print(self):
        for issue in self.issues:
            print(f"[{issue.level}] {issue.code}: {issue.message}")
        errors = sum(1 for issue in self.issues if issue.level == "ERROR")
        warnings = sum(1 for issue in self.issues if issue.level == "WARN")
        if errors == 0 and warnings == 0:
            print("[OK] Metadata consistency check passed.")
        else:
            print(f"[SUMMARY] errors={errors} warnings={warnings}")
        return errors


def read_text(path):
    return path.read_text(encoding="utf-8", errors="ignore")


def service_id(user_id):
    value = re.sub(r"[^a-z0-9]+", "-", user_id.lower()).strip("-")
    return value


def parse_users_csv(path, reporter):
    rows = {}
    if not path.is_file():
        reporter.warn("users_csv_missing", f"users.csv not found: {path}")
        return rows

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        values = list(csv.reader(handle))

    if not values:
        return rows

    first = [item.strip() for item in values[0]]
    has_header = {"user_id", "port", "created_at"}.issubset(set(first))
    data_rows = values[1:] if has_header else values
    header = first if has_header else ["user_id", "port", "created_at", "status"]

    for index, values in enumerate(data_rows, 2 if has_header else 1):
        if not values or not any(value.strip() for value in values):
            continue
        row = {
            header[column]: values[column].strip() if column < len(values) else ""
            for column in range(len(header))
        }
        user_id = row.get("user_id", "")
        if not user_id:
            reporter.warn("users_csv_empty_user", f"empty user_id at row {index}")
            continue
        if user_id in rows:
            reporter.error("users_csv_duplicate_user", f"duplicate user_id in users.csv: {user_id}")
        rows[user_id] = {
            "port": parse_int(row.get("port")),
            "status": (row.get("status") or "active").strip() or "active",
        }
    return rows


def parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def scan_user_dirs(public_dir):
    users_root = public_dir / "users"
    if not users_root.is_dir():
        return {}
    return {
        path.name: path
        for path in users_root.iterdir()
        if path.is_dir() and (path / "docker-compose.yml").is_file()
    }


def detect_compose(path):
    result = {
        "exists": path.is_file(),
        "service": None,
        "container_name": None,
        "version": None,
        "has_agent_net": False,
        "has_old_gateway_service": False,
        "has_bad_empty_service": False,
    }
    if not path.is_file():
        return result
    text = read_text(path)
    service_match = re.search(r"(?m)^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$", text)
    container_match = re.search(r"(?m)^\s*container_name:\s*([^\s]+)\s*$", text)
    image_match = re.search(r"image:\s*ghcr\.io/openclaw/openclaw:([^\s\"']+)", text)
    result["service"] = service_match.group(1) if service_match else None
    result["container_name"] = container_match.group(1).strip('"').strip("'") if container_match else None
    result["version"] = image_match.group(1) if image_match else None
    result["has_agent_net"] = "agent-net" in text
    result["has_old_gateway_service"] = bool(re.search(r"(?m)^  openclaw-gateway:\s*$", text))
    result["has_bad_empty_service"] = bool(re.search(r"(?m)^  openclaw-:\s*$", text))
    return result


def detect_nginx_conf(path):
    result = {
        "exists": path.is_file(),
        "port": None,
        "proxy_user": None,
        "basic_auth_enabled": None,
        "admin_htpasswd": None,
        "root_proxy": None,
    }
    if not path.is_file():
        return result
    text = read_text(path)
    listen = re.search(r"(?m)^\s*listen\s+([0-9]+)\b", text)
    proxy_user = re.search(r"proxy_pass\s+http://openclaw_([^:;]+):18789;", text)
    admin_block = extract_block(text, "location /admin/ {")
    root_block = extract_block(text, "location / {")
    result["port"] = int(listen.group(1)) if listen else None
    result["proxy_user"] = proxy_user.group(1) if proxy_user else None
    result["root_proxy"] = f"openclaw_{result['proxy_user']}" if result["proxy_user"] else None
    if root_block is not None:
        if "auth_basic off;" in root_block:
            result["basic_auth_enabled"] = False
        elif 'auth_basic "OpenClaw Login";' in root_block:
            result["basic_auth_enabled"] = True
    if admin_block is not None:
        match = re.search(r"auth_basic_user_file\s+([^;]+);", admin_block)
        if match:
            result["admin_htpasswd"] = match.group(1).strip()
    return result


def extract_block(text, marker):
    start = text.find(marker)
    if start < 0:
        return None
    next_location = text.find("\n    location ", start + len(marker))
    next_server = text.find("\n}", start + len(marker))
    candidates = [value for value in [next_location, next_server] if value >= 0]
    end = min(candidates) if candidates else len(text)
    return text[start:end]


def load_db(path, reporter):
    instances = {}
    ports = {}
    if not path.is_file():
        reporter.warn("metadata_db_missing", f"metadata database not found: {path}")
        return instances, ports
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute("SELECT * FROM instances"):
                instances[row["user_id"]] = dict(row)
            for row in conn.execute("SELECT * FROM ports"):
                ports[int(row["port"])] = dict(row)
        except sqlite3.Error as exc:
            reporter.error("metadata_db_read_failed", f"could not read metadata database: {exc}")
    return instances, ports


def container_htpasswd_path(user_id):
    base = NGINX_HTPASSWD_FILE_IN_CONTAINER.rstrip("/")
    if base.endswith("/.htpasswd"):
        auth_root = base[: -len("/.htpasswd")]
    else:
        auth_root = "/etc/nginx/auth"
    return f"{auth_root}/users/{user_id}/.htpasswd"


def host_htpasswd_path(user_id):
    return NGINX_AUTH_DIR / "users" / user_id / ".htpasswd"


def verbose_check(enabled, user_id, label):
    if enabled:
        print(f"[CHECK] {user_id}: {label}")


def check_user(user_id, user_dir, users_csv, db_instances, db_ports, reporter, verbose=False):
    compose_file = user_dir / "docker-compose.yml"
    nginx_conf = NGINX_USERS_CONF_DIR / f"{user_id}.conf"
    verbose_check(verbose, user_id, "compose file")
    compose = detect_compose(compose_file)
    verbose_check(verbose, user_id, "nginx conf")
    nginx = detect_nginx_conf(nginx_conf)
    csv_row = users_csv.get(user_id)
    db_row = db_instances.get(user_id)
    csv_status = (csv_row or {}).get("status")
    db_status = (db_row or {}).get("status")
    is_deleted = csv_status == "deleted" or db_status == "deleted"
    expected_service = f"openclaw-{service_id(user_id)}"
    expected_container = f"openclaw_{user_id}"
    expected_htpasswd = container_htpasswd_path(user_id)

    if not service_id(user_id):
        reporter.error("invalid_service_id", f"{user_id}: could not derive compose service id")
    if compose["has_old_gateway_service"]:
        reporter.error("old_compose_service", f"{user_id}: compose still uses openclaw-gateway service")
    if compose["has_bad_empty_service"]:
        reporter.error("bad_compose_service", f"{user_id}: compose service is openclaw-")
    if compose["service"] and compose["service"] != expected_service:
        reporter.warn("unexpected_compose_service", f"{user_id}: service={compose['service']} expected={expected_service}")
    if compose["container_name"] != expected_container:
        reporter.error(
            "container_name_mismatch",
            f"{user_id}: container_name={compose['container_name']} expected={expected_container}",
        )
    if not compose["has_agent_net"]:
        reporter.error("compose_missing_agent_net", f"{user_id}: compose does not reference agent-net")

    if not nginx["exists"] and not is_deleted:
        reporter.error("nginx_conf_missing", f"{user_id}: nginx conf missing: {nginx_conf}")
    if nginx["proxy_user"] and nginx["proxy_user"] != user_id:
        reporter.error("nginx_proxy_mismatch", f"{user_id}: proxy target user={nginx['proxy_user']}")
    if nginx["admin_htpasswd"] and nginx["admin_htpasswd"] != expected_htpasswd:
        reporter.error(
            "admin_htpasswd_mismatch",
            f"{user_id}: admin htpasswd={nginx['admin_htpasswd']} expected={expected_htpasswd}",
        )
    if not host_htpasswd_path(user_id).is_file() and not is_deleted:
        reporter.error("htpasswd_missing", f"{user_id}: htpasswd missing: {host_htpasswd_path(user_id)}")

    verbose_check(verbose, user_id, "users.csv row")
    if csv_row is None:
        reporter.warn("users_csv_missing_user", f"{user_id}: user dir exists but users.csv has no row")
    elif nginx["port"] is not None and csv_row["port"] is not None and csv_row["port"] != nginx["port"]:
        reporter.error(
            "csv_port_mismatch",
            f"{user_id}: users.csv port={csv_row['port']} nginx port={nginx['port']}",
        )

    verbose_check(verbose, user_id, "metadata instance row")
    if db_row is None:
        reporter.warn("metadata_missing_user", f"{user_id}: user dir exists but metadata has no instance row")
        return

    if db_status == "deleted":
        reporter.warn("metadata_deleted_but_dir_exists", f"{user_id}: metadata status is deleted but user dir exists")
    if nginx["port"] is not None and db_row.get("port") is not None and int(db_row["port"]) != nginx["port"]:
        reporter.error(
            "metadata_port_mismatch",
            f"{user_id}: metadata port={db_row['port']} nginx port={nginx['port']}",
        )
    if db_row.get("container_name") and db_row["container_name"] != expected_container:
        reporter.error(
            "metadata_container_mismatch",
            f"{user_id}: metadata container={db_row['container_name']} expected={expected_container}",
        )
    if compose["version"] and db_row.get("openclaw_version") and db_row["openclaw_version"] != compose["version"]:
        reporter.warn(
            "metadata_version_mismatch",
            f"{user_id}: metadata version={db_row['openclaw_version']} compose version={compose['version']}",
        )
    if nginx["basic_auth_enabled"] is not None and db_row.get("basic_auth_enabled") is not None:
        db_auth = bool(db_row["basic_auth_enabled"])
        if db_auth != nginx["basic_auth_enabled"]:
            reporter.warn(
                "metadata_basic_auth_mismatch",
                f"{user_id}: metadata basic_auth={db_auth} nginx basic_auth={nginx['basic_auth_enabled']}",
            )

    port = nginx["port"] if nginx["port"] is not None else db_row.get("port")
    if port is not None:
        verbose_check(verbose, user_id, "metadata port row")
        port_row = db_ports.get(int(port))
        if port_row is None:
            reporter.warn("metadata_port_row_missing", f"{user_id}: ports table missing port={port}")
        elif is_deleted and port_row.get("status") == "released":
            pass
        elif port_row.get("user_id") != user_id or port_row.get("status") != "allocated":
            reporter.warn(
                "metadata_port_row_mismatch",
                f"{user_id}: ports row port={port} user_id={port_row.get('user_id')} status={port_row.get('status')}",
            )


def check_global(users_dirs, users_csv, db_instances, reporter):
    for user_id, row in users_csv.items():
        if row["status"] != "deleted" and user_id not in users_dirs:
            reporter.warn("users_csv_dir_missing", f"{user_id}: users.csv row exists but user dir is missing")

    for user_id, row in db_instances.items():
        if row.get("status") != "deleted" and user_id not in users_dirs:
            reporter.warn("metadata_dir_missing", f"{user_id}: active metadata row exists but user dir is missing")

    if NGINX_COMPOSE_FILE.is_file():
        text = read_text(NGINX_COMPOSE_FILE)
        mapped_ports = {
            int(match.group(1))
            for match in re.finditer(r'["\']?([0-9]+):\1["\']?', text)
        }
        for user_id, user_dir in users_dirs.items():
            port = detect_nginx_conf(NGINX_USERS_CONF_DIR / f"{user_id}.conf")["port"]
            if port is not None and port not in mapped_ports:
                reporter.warn("nginx_compose_port_missing", f"{user_id}: port {port} missing from nginx compose")
    else:
        reporter.warn("nginx_compose_missing", f"nginx compose not found: {NGINX_COMPOSE_FILE}")

    if PORT_FILE.is_file():
        current = parse_int(read_text(PORT_FILE).strip())
        used_ports = [
            detect_nginx_conf(NGINX_USERS_CONF_DIR / f"{user_id}.conf")["port"]
            for user_id in users_dirs
        ]
        used_ports = [port for port in used_ports if port is not None]
        if current is not None and used_ports and current <= max(used_ports):
            reporter.warn(
                "port_file_not_ahead",
                f"ports.txt={current} is not greater than max used port={max(used_ports)}",
            )
    else:
        reporter.warn("port_file_missing", f"ports.txt not found: {PORT_FILE}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Check consistency between OpenClaw runtime files and SQLite metadata."
    )
    parser.add_argument("--user-id", help="check a single user only")
    parser.add_argument("--quiet", action="store_true", help="only print issues and summary")
    parser.add_argument("--verbose", action="store_true", help="print each checked category")
    return parser


def main():
    args = build_parser().parse_args()
    reporter = Reporter()
    users_csv = parse_users_csv(USERS_CSV, reporter)
    users_dirs = scan_user_dirs(OPENCLAW_PUBLIC_DIR)
    db_instances, db_ports = load_db(METADATA_DB_FILE, reporter)

    if args.user_id:
        if args.user_id not in users_dirs:
            reporter.error("user_dir_missing", f"user dir not found or compose missing: {args.user_id}")
        else:
            check_user(
                args.user_id,
                users_dirs[args.user_id],
                users_csv,
                db_instances,
                db_ports,
                reporter,
                verbose=args.verbose,
            )
    else:
        for user_id, user_dir in sorted(users_dirs.items()):
            check_user(user_id, user_dir, users_csv, db_instances, db_ports, reporter, verbose=args.verbose)
        check_global(users_dirs, users_csv, db_instances, reporter)

    if not args.quiet:
        print(f"[INFO] OpenClaw public dir: {OPENCLAW_PUBLIC_DIR}")
        print(f"[INFO] users.csv: {USERS_CSV}")
        print(f"[INFO] metadata db: {METADATA_DB_FILE}")
        print(f"[INFO] nginx conf dir: {NGINX_USERS_CONF_DIR}")
        print(f"[INFO] checked user dirs: {len(users_dirs)}")

    errors = reporter.print()
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
