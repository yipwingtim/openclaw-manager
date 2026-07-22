import csv
import re
from pathlib import Path


def _restore_port(public_dir, user_id, nginx_conf=None):
    if nginx_conf and nginx_conf.is_file():
        match = re.search(
            r"^[ \t]*listen[ \t]+([0-9]+)(?:[ \t]+ssl)?;",
            nginx_conf.read_text(encoding="utf-8", errors="ignore"),
            re.MULTILINE,
        )
        if match:
            return int(match.group(1))

    users_csv = Path(public_dir) / "users.csv"
    if users_csv.is_file():
        port = None
        with users_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) > 1 and row[0] == user_id and row[1].isdigit():
                    port = int(row[1])
        if port is not None:
            return port
    return None


def deleted_payload(public_dir, user_id):
    candidates = [
        path
        for path in (Path(public_dir) / "deleted").glob(f"{user_id}_*")
        if path.is_dir()
    ]
    if not candidates:
        return "incomplete", None

    recycle_dir = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    current = recycle_dir / "user"
    if current.is_dir():
        compose = current / "docker-compose.yml"
        nginx = recycle_dir / "nginx" / f"{user_id}.conf"
        if compose.is_file() and nginx.is_file() and _restore_port(public_dir, user_id, nginx):
            return "restorable", current
        return "incomplete", None

    if (recycle_dir / "docker-compose.yml").is_file() and _restore_port(public_dir, user_id):
        return "restorable", recycle_dir
    return "incomplete", None
