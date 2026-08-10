#!/usr/bin/env python3

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
NGINX_CONF_DIR = Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
NGINX_COMPOSE_FILE = Path(os.environ.get(
    "NGINX_COMPOSE_FILE", "/data/docker/nginx/compose/docker-compose.yml"
))
DB_FILE = Path(os.environ.get("METADATA_DB_FILE", PUBLIC_DIR / "manager.db"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()
AUTH_NETWORK = "instance-auth-net"
AUTH_CONTAINER = "openclaw-instance-auth-proxy"


def run(command):
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    return result.returncode, (result.stdout + "\n" + result.stderr).strip()


def auth_blocks(public_id):
    upstream = "instance_auth_" + re.sub(r"[^A-Za-z0-9_]", "_", public_id)
    return (
        f"upstream {upstream} {{\n"
        f"    zone {upstream} 64k;\n"
        "    resolver 127.0.0.11 valid=10s ipv6=off;\n"
        f"    server {AUTH_CONTAINER}:8084 resolve;\n"
        "}\n\n",
        "    location = /_instance_auth {\n"
        "        internal;\n"
        f"        proxy_pass http://{upstream}/authorize/{public_id};\n"
        "        proxy_pass_request_body off;\n"
        "        proxy_set_header Content-Length \"\";\n"
        "        proxy_set_header Cookie $http_cookie;\n"
        "    }\n"
        "    error_page 401 = @instance_login;\n"
        "    location @instance_login {\n"
        f"        return 302 https://{PUBLIC_HOST}:30015/login?instance={public_id};\n"
        "    }\n",
    )


def ensure_compose_network(text, network=AUTH_NETWORK):
    service_line = f"      - {network}\n"
    definition = f"  {network}:\n    external: true\n"
    lines = text.splitlines(keepends=True)
    service_start = next(
        (index for index, line in enumerate(lines) if line == "  nginx:\n"), None
    )
    if service_start is None:
        raise ValueError("Nginx compose service not found")
    service_end = next(
        (index for index in range(service_start + 1, len(lines))
         if re.match(r"^  [^ ]+:[ ]*\n$", lines[index])),
        len(lines),
    )
    if not any(line.strip().lstrip("-").strip() == network for line in lines[service_start:service_end]):
        networks_index = next(
            (index for index in range(service_start + 1, service_end)
             if lines[index] == "    networks:\n"),
            None,
        )
        if networks_index is None:
            raise ValueError("Nginx compose service networks section not found")
        lines.insert(networks_index + 1, service_line)
    if definition not in "".join(lines):
        top_level = next(
            (index for index, line in enumerate(lines) if line == "networks:\n"), None
        )
        if top_level is None:
            raise ValueError("Nginx compose top-level networks section not found")
        lines.insert(top_level + 1, definition)
    return "".join(lines)


def migrate_config(text, public_id, product):
    if "auth_request /_instance_auth;" in text:
        return text
    upstream, locations = auth_blocks(public_id)
    if "server {\n" not in text:
        raise ValueError("server block marker is missing")
    text = upstream + text
    text = text.replace("server {\n", "server {\n" + locations, 1)
    if product == "evoscientist":
        text = re.sub(
            r'^\s*auth_basic "OpenClaw Login";\n\s*auth_basic_user_file [^;]+;\n',
            "",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    locations_found = 0
    lines = []
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*location (?:= )?/(?!_instance_auth)", line):
            locations_found += 1
            if "{" in line and "}" in line:
                line = line.replace("{", "{ auth_request /_instance_auth;", 1)
            else:
                lines.append(line)
                lines.append("        auth_request /_instance_auth;\n")
                continue
        lines.append(line)
    if locations_found == 0:
        raise ValueError("proxy location marker is missing")
    return "".join(lines)


def instances():
    with sqlite3.connect(DB_FILE) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT public_id, legacy_user_id, product, runtime_identifier, status, port, nginx_conf_path
            FROM instances
            WHERE product IN ('hermes', 'evoscientist')
              AND status IN ('active', 'stopped')
            ORDER BY product, public_id
            """
        ).fetchall()


def config_path(instance):
    if instance["product"] == "evoscientist":
        canonical = NGINX_CONF_DIR / f"evoscientist-{instance['public_id']}.conf"
        legacy = PUBLIC_DIR / "deleted" / "evoscientist" / f"{instance['public_id']}.nginx.conf"
        if canonical.is_file() and legacy.is_file():
            raise RuntimeError(f"multiple EvoScientist configs found: {canonical}, {legacy}")
        return canonical if canonical.is_file() else legacy
    active = NGINX_CONF_DIR / f"hermes-{instance['public_id']}.conf"
    disabled = NGINX_CONF_DIR / "_disabled" / active.name
    return active if active.is_file() else disabled


def needs_path_migration(instance, path):
    return (
        instance["product"] == "evoscientist"
        and path == PUBLIC_DIR / "deleted" / "evoscientist" / f"{instance['public_id']}.nginx.conf"
    )


def reload_nginx(container):
    code, output = run(["docker", "exec", container, "nginx", "-t"])
    if code != 0:
        raise RuntimeError(output or f"{container}: nginx -t failed")
    code, output = run(["docker", "exec", container, "nginx", "-s", "reload"])
    if code != 0:
        raise RuntimeError(output or f"{container}: nginx reload failed")


def restart_nginx(container):
    code, output = run(["docker", "restart", container])
    if code != 0:
        raise RuntimeError(output or f"{container}: restart failed")
    code, output = run(["docker", "exec", container, "nginx", "-t"])
    if code != 0:
        raise RuntimeError(output or f"{container}: nginx -t failed")
    code, output = run([
        "docker", "inspect", "--format", "{{.State.Running}}", container,
    ])
    if code != 0 or output.strip() != "true":
        raise RuntimeError(output or f"{container}: container is not running")


def refresh_nginx(instance, container):
    if instance["product"] == "evoscientist":
        restart_nginx(container)
    else:
        reload_nginx(container)


def recreate_evoscientist_ingress(instance, config_file):
    container = instance_container(instance)
    port = instance["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError(f"invalid EvoScientist ingress port: {port}")
    network = "openclaw-user-" + hashlib.sha256(
        instance["legacy_user_id"].encode()
    ).hexdigest()
    auth_dir = os.environ.get("NGINX_AUTH_DIR", "/data/docker/nginx/auth")
    cert_dir = os.environ.get("NGINX_CERTS_DIR", "/data/docker/nginx/certs")
    run(["docker", "rm", "-f", container])
    code, output = run([
        "docker", "run", "-d", "--name", container, "--restart", "unless-stopped",
        "--network", network, "-p", f"{port}:443",
        "-v", f"{config_file}:/etc/nginx/conf.d/default.conf:ro",
        "-v", f"{cert_dir}:/etc/nginx/certs:ro",
        "-v", f"{auth_dir}:/etc/nginx/auth:ro", "nginx:alpine",
    ])
    if code != 0:
        raise RuntimeError(output or f"could not recreate {container}")
    code, output = run(["docker", "network", "connect", AUTH_NETWORK, container])
    if code != 0 and "already exists" not in output.lower():
        raise RuntimeError(output or f"could not connect {container}")
    code, output = run(["docker", "exec", container, "nginx", "-t"])
    if code != 0:
        raise RuntimeError(output or f"{container}: nginx -t failed")


def update_metadata_config_path(instance, path):
    if instance["product"] != "evoscientist":
        return
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute(
            "UPDATE instances SET nginx_conf_path = ?, updated_at = datetime('now') WHERE public_id = ?",
            (str(path), instance["public_id"]),
        )
        connection.commit()


def instance_container(instance):
    if instance["product"] == "evoscientist":
        return f"{instance['runtime_identifier']}-ingress"
    return os.environ.get("NGINX_CONTAINER_NAME", "openclaw-nginx")


def preflight_apply(pending):
    errors = []
    backup_parent = PUBLIC_DIR / ".manager-auth-backups"
    existing_backup_parent = backup_parent
    while not existing_backup_parent.exists():
        existing_backup_parent = existing_backup_parent.parent
    if not os.access(existing_backup_parent, os.W_OK | os.X_OK):
        errors.append(f"backup directory is not writable: {existing_backup_parent}")

    for instance in pending:
        path = config_path(instance)
        if not os.access(path, os.R_OK | os.W_OK):
            errors.append(f"config is not readable and writable: {path}")
        if not os.access(path.parent, os.W_OK | os.X_OK):
            errors.append(f"config directory is not writable: {path.parent}")
        if instance["product"] == "evoscientist":
            canonical_dir = NGINX_CONF_DIR
            if not os.access(canonical_dir, os.W_OK | os.X_OK):
                errors.append(f"canonical config directory is not writable: {canonical_dir}")

        container = instance_container(instance)
        code, output = run(["docker", "inspect", container])
        if code != 0 and not (
            instance["product"] == "evoscientist"
            and instance["status"] == "stopped"
        ):
            errors.append(output or f"container is not available: {container}")

    if any(instance["product"] == "hermes" for instance in pending):
        if not NGINX_COMPOSE_FILE.is_file():
            errors.append(f"Nginx compose file is missing: {NGINX_COMPOSE_FILE}")
        else:
            if not os.access(NGINX_COMPOSE_FILE, os.R_OK | os.W_OK):
                errors.append(
                    f"Nginx compose file is not readable and writable: {NGINX_COMPOSE_FILE}"
                )
            if not os.access(NGINX_COMPOSE_FILE.parent, os.W_OK | os.X_OK):
                errors.append(
                    f"Nginx compose directory is not writable: {NGINX_COMPOSE_FILE.parent}"
                )
            try:
                ensure_compose_network(NGINX_COMPOSE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                errors.append(f"Nginx compose cannot be migrated: {exc}")

    if errors:
        raise RuntimeError(
            "Migration preflight failed before any changes:\n- "
            + "\n- ".join(errors)
            + "\nRun this command through manager-executor."
        )


def apply_one(instance, backup_dir):
    source = config_path(instance)
    if not source.is_file():
        raise RuntimeError(f"config missing: {source}")
    target = (
        NGINX_CONF_DIR / f"evoscientist-{instance['public_id']}.conf"
        if instance["product"] == "evoscientist" else source
    )
    old = source.read_text(encoding="utf-8")
    new = migrate_config(old, instance["public_id"], instance["product"])
    if new == old and source == target:
        return "unchanged"
    backup = backup_dir / source.name
    shutil.copy2(source, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.instance-auth.tmp")
    temporary.write_text(new, encoding="utf-8")
    temporary.replace(target)
    container = instance_container(instance)
    connected = False
    try:
        if instance["product"] == "evoscientist":
            code, inspection = run([
                "docker", "inspect", "--format",
                "{{.State.Status}}|{{range $name, $_ := .NetworkSettings.Networks}}{{printf \"%s \" $name}}{{end}}",
                container,
            ])
            if code != 0:
                if instance["status"] == "stopped":
                    state = "stopped"
                    networks = ""
                else:
                    raise RuntimeError(inspection or f"container missing: {container}")
            else:
                state, _, networks = inspection.partition("|")
            if state == "running" and AUTH_NETWORK not in networks.split():
                code, output = run(["docker", "network", "connect", AUTH_NETWORK, container])
                if code != 0:
                    raise RuntimeError(output or f"could not connect {container}")
                connected = True
            if state != "running":
                if source != target:
                    source.unlink()
                    update_metadata_config_path(instance, target)
                return "updated-stopped"
        else:
            code, networks = run([
                "docker", "inspect", "--format",
                "{{range $name, $_ := .NetworkSettings.Networks}}{{printf \"%s\\n\" $name}}{{end}}",
                container,
            ])
            if code != 0:
                raise RuntimeError(networks or f"container missing: {container}")
            if AUTH_NETWORK not in networks.splitlines():
                code, output = run(["docker", "network", "connect", AUTH_NETWORK, container])
                if code != 0:
                    raise RuntimeError(output or f"could not connect {container}")
                connected = True
        if instance["product"] == "evoscientist" and source != target and state == "running":
            recreate_evoscientist_ingress(instance, target)
        else:
            refresh_nginx(instance, container)
        if source != target:
            source.unlink()
            update_metadata_config_path(instance, target)
        elif instance["product"] == "evoscientist" and instance.get("nginx_conf_path") != str(target):
            update_metadata_config_path(instance, target)
        return "updated"
    except Exception:
        if source != target:
            target.unlink(missing_ok=True)
        source.write_text(old, encoding="utf-8")
        if connected:
            run(["docker", "network", "disconnect", AUTH_NETWORK, container])
        try:
            if instance["product"] == "evoscientist" and source != target and state == "running":
                recreate_evoscientist_ingress(instance, source)
            else:
                refresh_nginx(instance, container)
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description="Migrate Hermes/EvoScientist ingress to UIS authorization")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not PUBLIC_HOST:
        raise SystemExit("PUBLIC_HOST is required")
    rows = instances()
    pending = []
    for instance in rows:
        try:
            path = config_path(instance)
        except RuntimeError as exc:
            print(f"[ERROR] {instance['product']} {instance['public_id']}: {exc}")
            continue
        if not path.is_file():
            print(f"[ERROR] missing {instance['product']} config: {path}")
            continue
        try:
            changed = migrate_config(path.read_text(encoding="utf-8"), instance["public_id"], instance["product"])
        except ValueError as exc:
            print(f"[ERROR] {path}: {exc}")
            continue
        if changed != path.read_text(encoding="utf-8") or needs_path_migration(instance, path):
            pending.append(instance)
            print(f"[PLAN] {instance['product']} {instance['public_id']}: {path}")
    if not args.apply:
        print(f"[INFO] {len(pending)} config(s) require migration; rerun with --apply")
        return
    code, output = run(["docker", "inspect", AUTH_CONTAINER])
    if code != 0:
        raise SystemExit(output or f"{AUTH_CONTAINER} is not available")
    code, output = run(["docker", "network", "inspect", AUTH_NETWORK])
    if code != 0:
        raise SystemExit(output or f"{AUTH_NETWORK} is not available")
    try:
        preflight_apply(pending)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = PUBLIC_DIR / ".manager-auth-backups" / f"instance-auth-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    if any(instance["product"] == "hermes" for instance in pending):
        if not NGINX_COMPOSE_FILE.is_file():
            raise SystemExit(f"Nginx compose file is missing: {NGINX_COMPOSE_FILE}")
        old_compose = NGINX_COMPOSE_FILE.read_text(encoding="utf-8")
        new_compose = ensure_compose_network(old_compose)
        if new_compose != old_compose:
            shutil.copy2(NGINX_COMPOSE_FILE, backup_dir / "nginx-docker-compose.yml")
            NGINX_COMPOSE_FILE.write_text(new_compose, encoding="utf-8")
            code, output = run([
                "docker", "compose", "-f", str(NGINX_COMPOSE_FILE), "up", "-d"
            ])
            if code != 0:
                NGINX_COMPOSE_FILE.write_text(old_compose, encoding="utf-8")
                run(["docker", "compose", "-f", str(NGINX_COMPOSE_FILE), "up", "-d"])
                raise SystemExit(output or "could not attach shared Nginx to instance-auth-net")
    for instance in pending:
        result = apply_one(instance, backup_dir)
        print(f"[OK] {instance['product']} {instance['public_id']}: {result}")
    print(f"[INFO] Backup: {backup_dir}")


if __name__ == "__main__":
    main()
