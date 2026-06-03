import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


PUBLIC_DIR = Path(os.environ.get("OPENCLAW_PUBLIC_DIR", "/data/docker/openclaw-public"))
DB_FILE = Path(os.environ.get("METADATA_DB_FILE", str(PUBLIC_DIR / "manager.db")))
SCHEMA_FILE = Path(os.environ.get("METADATA_SCHEMA_FILE", "/opt/openclaw-manager/db/schema.sql"))


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect(db_file=None):
    path = Path(db_file or DB_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize(db_file=None, schema_file=None):
    schema_path = Path(schema_file or SCHEMA_FILE)
    schema = schema_path.read_text(encoding="utf-8")
    with connect(db_file) as conn:
        conn.executescript(schema)


def row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def upsert_instance(
    *,
    user_id,
    product="openclaw",
    port=None,
    status="active",
    openclaw_version=None,
    basic_auth_enabled=True,
    container_name=None,
    access_url=None,
    admin_url=None,
    data_path=None,
    nginx_conf_path=None,
    deleted_at=None,
    conn=None,
):
    now = utc_now()
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            INSERT INTO instances (
                user_id,
                product,
                port,
                status,
                openclaw_version,
                basic_auth_enabled,
                container_name,
                access_url,
                admin_url,
                data_path,
                nginx_conf_path,
                created_at,
                updated_at,
                deleted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                product = excluded.product,
                port = excluded.port,
                status = excluded.status,
                openclaw_version = excluded.openclaw_version,
                basic_auth_enabled = excluded.basic_auth_enabled,
                container_name = excluded.container_name,
                access_url = excluded.access_url,
                admin_url = excluded.admin_url,
                data_path = excluded.data_path,
                nginx_conf_path = excluded.nginx_conf_path,
                updated_at = excluded.updated_at,
                deleted_at = excluded.deleted_at
            """,
            (
                user_id,
                product,
                port,
                status,
                openclaw_version,
                1 if basic_auth_enabled else 0,
                container_name,
                access_url,
                admin_url,
                data_path,
                nginx_conf_path,
                now,
                now,
                deleted_at,
            ),
        )


def get_instance(user_id, conn=None):
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            "SELECT * FROM instances WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row_to_dict(row)


def list_instances(status=None, conn=None):
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        if status:
            rows = active_conn.execute(
                "SELECT * FROM instances WHERE status = ? ORDER BY created_at DESC, user_id ASC",
                (status,),
            ).fetchall()
        else:
            rows = active_conn.execute(
                "SELECT * FROM instances ORDER BY created_at DESC, user_id ASC"
            ).fetchall()
        return [row_to_dict(row) for row in rows]


def upsert_credentials(
    *,
    user_id,
    basic_auth_username=None,
    basic_auth_password_ref=None,
    openclaw_token=None,
    conn=None,
):
    now = utc_now()
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            INSERT INTO instance_credentials (
                user_id,
                basic_auth_username,
                basic_auth_password_ref,
                openclaw_token,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                basic_auth_username = excluded.basic_auth_username,
                basic_auth_password_ref = excluded.basic_auth_password_ref,
                openclaw_token = excluded.openclaw_token,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                basic_auth_username,
                basic_auth_password_ref,
                openclaw_token,
                now,
                now,
            ),
        )


def record_port(port, user_id=None, status="allocated", conn=None):
    now = utc_now()
    released_at = now if status == "released" else None
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            INSERT INTO ports (port, user_id, status, created_at, released_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(port) DO UPDATE SET
                user_id = excluded.user_id,
                status = excluded.status,
                created_at = excluded.created_at,
                released_at = excluded.released_at
            """,
            (port, user_id, status, now, released_at),
        )


def record_operation(
    *,
    action,
    status,
    actor=None,
    user_id=None,
    message=None,
    finished_at=None,
    conn=None,
):
    now = utc_now()
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            INSERT INTO operation_records (
                actor,
                action,
                user_id,
                status,
                message,
                created_at,
                finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (actor, action, user_id, status, message, now, finished_at),
        )


def list_operations(limit=100, conn=None):
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        rows = active_conn.execute(
            """
            SELECT * FROM operation_records
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


class nullcontext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, traceback):
        return False
