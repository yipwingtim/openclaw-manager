import json
import os
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
            if version < 4:
                raise RuntimeError(
                    "metadata schema requires scripts/migrate_control_plane_model.py"
                )
        conn.executescript(schema)


def row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def normalize_username(value):
    return unicodedata.normalize("NFKC", value).casefold()


def get_user_by_username(username, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        return row_to_dict(
            active_conn.execute(
                "SELECT * FROM users WHERE normalized_username = ?",
                (normalize_username(username),),
            ).fetchone()
        )


def get_user_by_identity(provider, subject, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        return row_to_dict(
            active_conn.execute(
                """
                SELECT u.*
                FROM users u
                JOIN user_identities i ON i.user_id = u.id
                WHERE i.provider = ? AND i.subject = ?
                """,
                (provider, subject),
            ).fetchone()
        )


def upsert_identity(user_id, provider, subject, external_username=None, db_file=None, conn=None):
    now = utc_now()
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            INSERT INTO user_identities (
                user_id, provider, subject, external_username, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, subject) DO UPDATE SET
                external_username = excluded.external_username,
                updated_at = excluded.updated_at
            """,
            (user_id, provider, subject, external_username, now, now),
        )
        owner = active_conn.execute(
            "SELECT user_id FROM user_identities WHERE provider = ? AND subject = ?",
            (provider, subject),
        ).fetchone()["user_id"]
        if owner != user_id:
            raise ValueError("identity is already linked to another user")


def set_user_role(user_id, role, db_file=None, conn=None):
    if role not in {"admin", "user"}:
        raise ValueError("invalid role")
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
            (role, utc_now(), user_id),
        )


def set_local_credential(
    user_id,
    password_hash,
    must_change_password=True,
    db_file=None,
    conn=None,
):
    now = utc_now()
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            INSERT INTO local_credentials (
                user_id, password_hash, password_changed_at,
                must_change_password, failed_login_count, locked_until,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, NULL, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                password_hash = excluded.password_hash,
                password_changed_at = excluded.password_changed_at,
                must_change_password = excluded.must_change_password,
                failed_login_count = 0,
                locked_until = NULL,
                updated_at = excluded.updated_at
            """,
            (user_id, password_hash, now, 1 if must_change_password else 0, now, now),
        )


def get_local_credential(user_id, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        return row_to_dict(
            active_conn.execute(
                "SELECT * FROM local_credentials WHERE user_id = ?", (user_id,)
            ).fetchone()
        )


def record_login_failure(user_id, max_failures=5, lock_minutes=15, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        credential = active_conn.execute(
            "SELECT failed_login_count FROM local_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if credential is None:
            return
        failures = credential["failed_login_count"] + 1
        locked_until = None
        if failures >= max_failures:
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=lock_minutes)).replace(microsecond=0).isoformat()
            failures = 0
        active_conn.execute(
            "UPDATE local_credentials SET failed_login_count = ?, locked_until = ?, updated_at = ? WHERE user_id = ?",
            (failures, locked_until, utc_now(), user_id),
        )


def reset_login_failures(user_id, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            "UPDATE local_credentials SET failed_login_count = 0, locked_until = NULL, updated_at = ? WHERE user_id = ?",
            (utc_now(), user_id),
        )


def create_session(
    token_hash,
    user_id,
    provider,
    csrf_token,
    expires_at,
    session_kind="user",
    db_file=None,
    conn=None,
):
    if session_kind not in {"user", "admin", "emergency"}:
        raise ValueError("invalid session kind")
    now = utc_now()
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
        active_conn.execute(
            """
            INSERT INTO user_sessions (
                token_hash, user_id, provider, session_kind, csrf_token,
                expires_at, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_hash,
                user_id,
                provider,
                session_kind,
                csrf_token,
                expires_at,
                now,
                now,
            ),
        )


def get_session(token_hash, db_file=None, conn=None):
    now = utc_now()
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            """
            SELECT s.token_hash, s.provider, s.session_kind, s.csrf_token, s.expires_at,
                   u.id, u.public_id, u.username, u.normalized_username,
                   u.display_name, u.email, u.role, u.status
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        return row_to_dict(row)


def delete_session(token_hash, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))


def activate_auth_provider(provider, db_file=None, conn=None):
    """Record the active provider and invalidate sessions when it changes."""
    now = utc_now()
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            INSERT OR IGNORE INTO auth_settings (key, value, updated_at)
            VALUES ('active_provider', ?, ?)
            """,
            (provider, now),
        )
        row = active_conn.execute(
            "SELECT value FROM auth_settings WHERE key = 'active_provider'"
        ).fetchone()
        previous = row["value"]
        if previous == provider:
            return False
        active_conn.execute("DELETE FROM user_sessions")
        active_conn.execute(
            "UPDATE auth_settings SET value = ?, updated_at = ? WHERE key = 'active_provider'",
            (provider, now),
        )
        return True


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


def list_instances_for_user(user_public_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        rows = active_conn.execute(
            """
            SELECT i.*,
                   CASE
                       WHEN i.owner_user_id = current_user.id THEN 'owner'
                       ELSE m.role
                   END AS access_role
            FROM instances i
            JOIN users current_user ON current_user.public_id = ?
            LEFT JOIN instance_members m
                ON m.instance_id = i.id
               AND m.user_id = current_user.id
            WHERE i.owner_user_id = current_user.id
               OR m.user_id IS NOT NULL
            ORDER BY i.id
            """,
            (user_public_id,),
        ).fetchall()
        return [instance_dict(row) for row in rows]


def add_instance_member(
    instance_public_id,
    user_public_id,
    role,
    *,
    created_by_user_id=None,
    db_file=None,
    conn=None,
):
    if role not in {"manager", "operator", "viewer"}:
        raise ValueError("invalid instance member role")
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        instance = active_conn.execute(
            "SELECT id, owner_user_id FROM instances WHERE public_id = ?",
            (instance_public_id,),
        ).fetchone()
        user = active_conn.execute(
            "SELECT id, status FROM users WHERE public_id = ?",
            (user_public_id,),
        ).fetchone()
        if instance is None:
            raise ValueError("instance not found")
        if user is None or user["status"] != "active":
            raise ValueError("active member user not found")
        if instance["owner_user_id"] == user["id"]:
            raise ValueError("owner cannot be an instance member")
        now = utc_now()
        active_conn.execute(
            """
            INSERT INTO instance_members (
                instance_id, user_id, role, created_by_user_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id, user_id) DO UPDATE SET
                role = excluded.role,
                updated_at = excluded.updated_at
            """,
            (
                instance["id"],
                user["id"],
                role,
                created_by_user_id,
                now,
                now,
            ),
        )
        return row_to_dict(
            active_conn.execute(
                "SELECT * FROM instance_members WHERE instance_id = ? AND user_id = ?",
                (instance["id"], user["id"]),
            ).fetchone()
        )


def create_execution_job(
    *,
    request_id,
    action,
    actor_user_id=None,
    instance_public_id=None,
    params=None,
    parent_request_id=None,
    db_file=None,
    conn=None,
):
    if not request_id or not action:
        raise ValueError("request_id and action are required")
    params_json = json.dumps(
        params or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        instance_id = None
        if instance_public_id is not None:
            instance = active_conn.execute(
                "SELECT id FROM instances WHERE public_id = ?",
                (instance_public_id,),
            ).fetchone()
            if instance is None:
                raise ValueError("instance not found")
            instance_id = instance["id"]
        now = utc_now()
        active_conn.execute(
            """
            INSERT INTO execution_jobs (
                request_id, parent_request_id, actor_user_id, instance_id,
                action, params_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (
                request_id,
                parent_request_id,
                actor_user_id,
                instance_id,
                action,
                params_json,
                now,
                now,
            ),
        )
        job = active_conn.execute(
            "SELECT * FROM execution_jobs WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if (
            job["action"],
            job["actor_user_id"],
            job["instance_id"],
            job["params_json"],
            job["parent_request_id"],
        ) != (
            action,
            actor_user_id,
            instance_id,
            params_json,
            parent_request_id,
        ):
            raise ValueError("request_id already used for another operation")
        return row_to_dict(job)


def update_execution_job(
    request_id,
    status,
    *,
    current_step=None,
    error_summary=None,
    output=None,
    db_file=None,
    conn=None,
):
    transitions = {
        "queued": {"running", "cancelled"},
        "running": {
            "running",
            "succeeded",
            "failed",
            "partial_failure",
            "interrupted",
            "cancelled",
        },
    }
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        job = active_conn.execute(
            "SELECT * FROM execution_jobs WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if job is None:
            raise ValueError("execution job not found")
        if status not in transitions.get(job["status"], set()):
            raise ValueError(
                f"invalid execution job transition: {job['status']} -> {status}"
            )
        now = utc_now()
        started_at = job["started_at"] or (now if status == "running" else None)
        heartbeat_at = now if status == "running" else job["heartbeat_at"]
        finished_at = (
            now
            if status
            in {
                "succeeded",
                "failed",
                "partial_failure",
                "interrupted",
                "cancelled",
            }
            else None
        )
        active_conn.execute(
            """
            UPDATE execution_jobs
            SET status = ?,
                current_step = COALESCE(?, current_step),
                heartbeat_at = ?,
                error_summary = COALESCE(?, error_summary),
                output = COALESCE(?, output),
                updated_at = ?,
                started_at = ?,
                finished_at = ?
            WHERE request_id = ?
            """,
            (
                status,
                current_step,
                heartbeat_at,
                error_summary,
                output,
                now,
                started_at,
                finished_at,
                request_id,
            ),
        )
        return row_to_dict(
            active_conn.execute(
                "SELECT * FROM execution_jobs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        )


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
    actor_user_id=None,
    instance_id=None,
    request_id=None,
    source_service=None,
    user_id=None,
    message=None,
    finished_at=None,
    conn=None,
):
    now = utc_now()
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        if actor_user_id is None and actor:
            actor_row = active_conn.execute(
                "SELECT id FROM users WHERE normalized_username = ?",
                (normalize_username(actor),),
            ).fetchone()
            actor_user_id = actor_row["id"] if actor_row else None
        if instance_id is None and user_id:
            instance_id = instance_id_for_legacy_user(user_id, active_conn)
        active_conn.execute(
            """
            INSERT INTO operation_records (
                request_id,
                actor,
                actor_user_id,
                source_service,
                action,
                user_id,
                instance_id,
                status,
                message,
                created_at,
                finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                actor,
                actor_user_id,
                source_service,
                action,
                user_id,
                instance_id,
                status,
                message,
                now,
                finished_at,
            ),
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
        "local_credentials",
        "user_sessions",
        "auth_settings",
        "instances",
        "instance_members",
        "instance_endpoints",
        "ports",
        "operation_records",
        "execution_jobs",
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
