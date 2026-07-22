import os
import sqlite3
import unicodedata
import uuid
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
        migration_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if migration_table:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
            if version < 2:
                raise RuntimeError(
                    "metadata schema version 1 requires scripts/migrate_identity_instance_model.py"
                )
        conn.executescript(schema)


def row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def normalize_username(value):
    return unicodedata.normalize("NFKC", value).casefold()


def ensure_legacy_user(username, conn):
    normalized = normalize_username(username)
    identity = conn.execute(
        "SELECT user_id FROM user_identities WHERE provider = 'legacy' AND subject = ?",
        (username,),
    ).fetchone()
    row = conn.execute(
        "SELECT id FROM users WHERE normalized_username = ?", (normalized,)
    ).fetchone()
    if identity:
        if row and row["id"] != identity["user_id"]:
            raise ValueError(f"legacy identity owner conflict: {username!r}")
        return identity["user_id"]
    if row:
        existing = conn.execute(
            "SELECT username FROM users WHERE id = ?", (row["id"],)
        ).fetchone()["username"]
        if existing != username:
            raise ValueError(
                f"normalized username collision: {existing!r} and {username!r}"
            )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO user_identities (
                user_id, provider, subject, external_username, created_at, updated_at
            ) VALUES (?, 'legacy', ?, ?, ?, ?)
            """,
            (row["id"], username, username, now, now),
        )
        return row["id"]

    now = utc_now()
    conn.execute(
        """
        INSERT INTO users (
            public_id, username, normalized_username, status,
            provisioning_source, created_at, updated_at
        ) VALUES (?, ?, ?, 'active', 'legacy', ?, ?)
        """,
        (str(uuid.uuid4()), username, normalized, now, now),
    )
    user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO user_identities (
            user_id, provider, subject, external_username, created_at, updated_at
        ) VALUES (?, 'legacy', ?, ?, ?, ?)
        """,
        (user_id, username, username, now, now),
    )
    return user_id


def instance_id_for_legacy_user(user_id, conn):
    row = conn.execute(
        "SELECT id FROM instances WHERE legacy_user_id = ?", (user_id,)
    ).fetchone()
    return row["id"] if row else None


def instance_dict(row):
    value = row_to_dict(row)
    if value is not None:
        value["user_id"] = value.get("legacy_user_id")
    return value


def create_user(
    username,
    *,
    display_name=None,
    email=None,
    status="active",
    provisioning_source="local",
    db_file=None,
    conn=None,
):
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    normalized = normalize_username(username)
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        existing = active_conn.execute(
            "SELECT * FROM users WHERE normalized_username = ?", (normalized,)
        ).fetchone()
        if existing:
            raise ValueError(
                f"normalized username collision: {existing['username']!r} and {username!r}"
            )
        now = utc_now()
        public_id = str(uuid.uuid4())
        active_conn.execute(
            """
            INSERT INTO users (
                public_id, username, normalized_username, display_name,
                email, status, provisioning_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                username,
                normalized,
                display_name,
                email,
                status,
                provisioning_source,
                now,
                now,
            ),
        )
        return row_to_dict(
            active_conn.execute(
                "SELECT * FROM users WHERE public_id = ?", (public_id,)
            ).fetchone()
        )


def create_instance(
    *,
    owner_public_id,
    product,
    instance_name,
    runtime_identifier,
    data_path=None,
    status="active",
    db_file=None,
    conn=None,
):
    if not product or not instance_name or not runtime_identifier:
        raise ValueError("product, instance_name, and runtime_identifier are required")
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        owner = active_conn.execute(
            "SELECT id FROM users WHERE public_id = ?", (owner_public_id,)
        ).fetchone()
        if owner is None:
            raise ValueError("owner user not found")
        now = utc_now()
        public_id = str(uuid.uuid4())
        try:
            active_conn.execute(
                """
                INSERT INTO instances (
                    public_id, owner_user_id, product, instance_name,
                    runtime_identifier, status, container_name, data_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    owner["id"],
                    product,
                    instance_name,
                    runtime_identifier,
                    status,
                    runtime_identifier,
                    data_path,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "runtime_identifier" in str(exc):
                raise ValueError("runtime identifier already exists") from exc
            if "data_path" in str(exc):
                raise ValueError("data path already exists") from exc
            raise
        return instance_dict(
            active_conn.execute(
                "SELECT * FROM instances WHERE public_id = ?", (public_id,)
            ).fetchone()
        )


def list_instances_for_user(owner_public_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        rows = active_conn.execute(
            """
            SELECT i.*
            FROM instances i
            JOIN users u ON u.id = i.owner_user_id
            WHERE u.public_id = ?
            ORDER BY i.id
            """,
            (owner_public_id,),
        ).fetchall()
        return [instance_dict(row) for row in rows]


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
    restore_state=None,
    conn=None,
):
    now = utc_now()
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        owner_user_id = ensure_legacy_user(user_id, active_conn)
        runtime_identifier = container_name or f"{product}_{user_id}"
        existing = active_conn.execute(
            "SELECT restore_state FROM instances WHERE legacy_user_id = ?", (user_id,)
        ).fetchone()
        resolved_restore_state = restore_state
        if resolved_restore_state is None:
            resolved_restore_state = (
                existing["restore_state"]
                if existing
                else ("incomplete" if status == "deleted" else "not_applicable")
            )
        active_conn.execute(
            """
            INSERT INTO instances (
                public_id,
                legacy_user_id,
                owner_user_id,
                product,
                instance_name,
                runtime_identifier,
                port,
                status,
                restore_state,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(legacy_user_id) DO UPDATE SET
                owner_user_id = excluded.owner_user_id,
                product = excluded.product,
                runtime_identifier = excluded.runtime_identifier,
                port = excluded.port,
                status = excluded.status,
                restore_state = excluded.restore_state,
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
                str(uuid.uuid4()),
                user_id,
                owner_user_id,
                product,
                user_id,
                runtime_identifier,
                port,
                status,
                resolved_restore_state,
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
        instance_id = instance_id_for_legacy_user(user_id, active_conn)
        endpoint_status = "inactive" if status == "deleted" else "active"
        if port is not None:
            active_conn.execute(
                """
                INSERT INTO instance_endpoints (
                    instance_id, endpoint_type, external_port, access_url,
                    status, created_at, updated_at
                ) VALUES (?, 'legacy_port', ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, endpoint_type) DO UPDATE SET
                    external_port = excluded.external_port,
                    access_url = excluded.access_url,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (instance_id, port, access_url, endpoint_status, now, now),
            )
        elif status == "deleted":
            active_conn.execute(
                """
                UPDATE instance_endpoints
                SET status = 'inactive', updated_at = ?
                WHERE instance_id = ?
                """,
                (now, instance_id),
            )


def get_instance(user_id, conn=None):
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            "SELECT * FROM instances WHERE legacy_user_id = ?",
            (user_id,),
        ).fetchone()
        return instance_dict(row)


def list_instances(status=None, conn=None):
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        if status:
            rows = active_conn.execute(
                "SELECT * FROM instances WHERE status = ? ORDER BY created_at DESC, legacy_user_id ASC",
                (status,),
            ).fetchall()
        else:
            rows = active_conn.execute(
                "SELECT * FROM instances ORDER BY created_at DESC, legacy_user_id ASC"
            ).fetchall()
        return [instance_dict(row) for row in rows]


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
        instance_id = instance_id_for_legacy_user(user_id, active_conn)
        if instance_id is None:
            raise ValueError(f"instance not found for legacy user: {user_id}")
        active_conn.execute(
            """
            INSERT INTO instance_credentials (
                instance_id,
                basic_auth_username,
                basic_auth_password_ref,
                openclaw_token,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id) DO UPDATE SET
                basic_auth_username = excluded.basic_auth_username,
                basic_auth_password_ref = excluded.basic_auth_password_ref,
                openclaw_token = excluded.openclaw_token,
                updated_at = excluded.updated_at
            """,
            (
                instance_id,
                basic_auth_username,
                basic_auth_password_ref,
                openclaw_token,
                now,
                now,
            ),
        )


def get_credentials(user_id, conn=None):
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            """
            SELECT c.*, i.legacy_user_id
            FROM instance_credentials c
            JOIN instances i ON i.id = c.instance_id
            WHERE i.legacy_user_id = ?
            """,
            (user_id,),
        ).fetchone()
        value = row_to_dict(row)
        if value is not None:
            value["user_id"] = value.pop("legacy_user_id")
        return value


def record_port(port, user_id=None, status="allocated", conn=None):
    now = utc_now()
    released_at = now if status == "released" else None
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        instance_id = instance_id_for_legacy_user(user_id, active_conn) if user_id else None
        active_conn.execute(
            """
            INSERT INTO ports (port, instance_id, status, created_at, released_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(port) DO UPDATE SET
                instance_id = excluded.instance_id,
                status = excluded.status,
                created_at = excluded.created_at,
                released_at = excluded.released_at
            """,
            (port, instance_id, status, now, released_at),
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
        actor_user_id = None
        if actor:
            actor_row = active_conn.execute(
                "SELECT id FROM users WHERE normalized_username = ?",
                (normalize_username(actor),),
            ).fetchone()
            actor_user_id = actor_row["id"] if actor_row else None
        instance_id = instance_id_for_legacy_user(user_id, active_conn) if user_id else None
        active_conn.execute(
            """
            INSERT INTO operation_records (
                actor,
                actor_user_id,
                action,
                user_id,
                instance_id,
                status,
                message,
                created_at,
                finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (actor, actor_user_id, action, user_id, instance_id, status, message, now, finished_at),
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


def table_counts(conn=None):
    tables = [
        "users",
        "user_identities",
        "instances",
        "instance_endpoints",
        "ports",
        "operation_records",
        "instance_credentials",
    ]
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    counts = {}
    with context as active_conn:
        for table in tables:
            counts[table] = active_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


class nullcontext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, traceback):
        return False
