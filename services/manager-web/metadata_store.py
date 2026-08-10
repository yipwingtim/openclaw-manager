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
            if version < 5:
                raise RuntimeError(
                    "metadata schema requires scripts/migrate_instance_provisioning_model.py"
                )
            if version < 6:
                raise RuntimeError(
                    "metadata schema requires scripts/migrate_external_session_tokens.py"
                )
            if version < 7:
                raise RuntimeError(
                    "metadata schema requires scripts/migrate_activity_snapshots.py"
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


def get_user_by_public_id(public_id, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        return row_to_dict(
            active_conn.execute(
                "SELECT * FROM users WHERE public_id = ?",
                (public_id,),
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


def record_identity_login(provider, subject, profile, db_file=None, conn=None):
    now = utc_now()
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            UPDATE user_identities
            SET profile_json = ?, last_login_at = ?, updated_at = ?
            WHERE provider = ? AND subject = ?
            """,
            (json.dumps(profile, ensure_ascii=True), now, now, provider, subject),
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
    external_token_hash=None,
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
        if external_token_hash:
            active_conn.execute(
                """
                INSERT INTO external_session_tokens (
                    external_token_hash, session_token_hash
                ) VALUES (?, ?)
                """,
                (external_token_hash, token_hash),
            )


def get_session(token_hash, db_file=None, conn=None):
    now = utc_now()
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            """
            SELECT s.token_hash, s.provider, s.session_kind, s.csrf_token, s.expires_at,
                   i.profile_json AS identity_profile_json,
                   u.id, u.public_id, u.username, u.normalized_username,
                   u.display_name, u.email, u.role, u.status
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN user_identities i
              ON i.user_id = s.user_id AND i.provider = s.provider
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


def delete_session_by_external_token(external_token_hash, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            """
            DELETE FROM user_sessions
            WHERE token_hash IN (
                SELECT session_token_hash
                FROM external_session_tokens
                WHERE external_token_hash = ?
            )
            """,
            (external_token_hash,),
        )


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


def get_setting(key, default=None, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            "SELECT value FROM auth_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default


def set_setting(key, value, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        active_conn.execute(
            "INSERT INTO auth_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utc_now()),
        )


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
    legacy_user_id=None,
    data_path=None,
    status="active",
    basic_auth_enabled=True,
    port=None,
    access_url=None,
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
        if port is not None:
            allocated = active_conn.execute(
                "SELECT status FROM ports WHERE port = ?", (port,)
            ).fetchone()
            if allocated is not None and allocated["status"] != "released":
                raise ValueError("port is already allocated")
        now = utc_now()
        public_id = str(uuid.uuid4())
        if data_path is None:
            data_path = str(PUBLIC_DIR / "instances" / product / public_id)
        try:
            active_conn.execute(
                """
                INSERT INTO instances (
                    public_id, legacy_user_id, owner_user_id, product, instance_name,
                    runtime_identifier, status, container_name, data_path,
                    basic_auth_enabled, port, access_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    legacy_user_id,
                    owner["id"],
                    product,
                    instance_name,
                    runtime_identifier,
                    status,
                    runtime_identifier,
                    data_path,
                    1 if basic_auth_enabled else 0,
                    port,
                    access_url,
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
        instance = instance_dict(
            active_conn.execute(
                "SELECT * FROM instances WHERE public_id = ?", (public_id,)
            ).fetchone()
        )
        if port is not None:
            active_conn.execute(
                """
                INSERT INTO instance_endpoints (
                    instance_id, endpoint_type, external_port, access_url,
                    status, created_at, updated_at
                ) VALUES (?, 'legacy_port', ?, ?, 'active', ?, ?)
                """,
                (instance["id"], port, access_url, now, now),
            )
            active_conn.execute(
                """
                INSERT INTO ports (port, instance_id, status, created_at, released_at)
                VALUES (?, ?, 'allocated', ?, NULL)
                ON CONFLICT(port) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    status = 'allocated',
                    created_at = excluded.created_at,
                    released_at = NULL
                """,
                (port, instance["id"], now),
            )
        return instance


def finish_instance_provisioning(
    instance_public_id,
    status,
    *,
    port=None,
    openclaw_version=None,
    access_url=None,
    admin_url=None,
    basic_auth_password_ref=None,
    openclaw_token=None,
    db_file=None,
    conn=None,
):
    if status not in {"active", "failed"}:
        raise ValueError("invalid provisioning result")
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        instance = active_conn.execute(
            "SELECT status FROM instances WHERE public_id = ?", (instance_public_id,)
        ).fetchone()
        if instance is None:
            raise ValueError("instance not found")
        if instance["status"] == status:
            return get_instance_by_public_id(instance_public_id, conn=active_conn)
        if instance["status"] != "provisioning":
            raise ValueError("invalid instance provisioning transition")
        now = utc_now()
        active_conn.execute(
            """
            UPDATE instances
            SET status = ?, port = COALESCE(?, port),
                openclaw_version = COALESCE(?, openclaw_version),
                access_url = COALESCE(?, access_url),
                admin_url = COALESCE(?, admin_url),
                nginx_conf_path = CASE WHEN product = 'evoscientist'
                    THEN ? ELSE nginx_conf_path END,
                updated_at = ?
            WHERE public_id = ?
            """,
            (
                status, port, openclaw_version, access_url, admin_url,
                str(Path(os.environ.get("NGINX_USERS_CONF_DIR", "/data/docker/nginx/conf"))
                    / f"evoscientist-{instance_public_id}.conf"),
                now, instance_public_id,
            ),
        )
        if status == "active":
            instance = get_instance_by_public_id(instance_public_id, conn=active_conn)
            if port is not None:
                record_port(
                    port,
                    user_id=instance["legacy_user_id"],
                    instance_id=instance["id"],
                    status="allocated",
                    conn=active_conn,
                )
                active_conn.execute(
                    """
                    INSERT INTO instance_endpoints (
                        instance_id, endpoint_type, external_port, access_url,
                        status, created_at, updated_at
                    ) VALUES (?, 'legacy_port', ?, ?, 'active', ?, ?)
                    ON CONFLICT(instance_id, endpoint_type) DO UPDATE SET
                        external_port = excluded.external_port,
                        access_url = excluded.access_url,
                        status = 'active', updated_at = excluded.updated_at
                    """,
                    (instance["id"], port, access_url, now, now),
                )
            if instance["legacy_user_id"] is not None:
                upsert_credentials(
                    user_id=instance["legacy_user_id"],
                    basic_auth_username=instance["legacy_user_id"],
                    basic_auth_password_ref=basic_auth_password_ref,
                    openclaw_token=openclaw_token,
                    conn=active_conn,
                )
        return get_instance_by_public_id(instance_public_id, conn=active_conn)


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
            WHERE (i.owner_user_id = current_user.id OR m.user_id IS NOT NULL)
              AND i.status IN ('active', 'stopped')
            ORDER BY i.id
            """,
            (user_public_id,),
        ).fetchall()
        return [instance_dict(row) for row in rows]


def get_instance_for_user(instance_public_id, user_public_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            """
            SELECT i.*,
                   current_user.id AS current_user_id,
                   CASE
                       WHEN i.owner_user_id = current_user.id THEN 'owner'
                       ELSE m.role
                   END AS access_role
            FROM instances i
            JOIN users current_user ON current_user.public_id = ?
            LEFT JOIN instance_members m
                ON m.instance_id = i.id
               AND m.user_id = current_user.id
            WHERE i.public_id = ?
              AND current_user.status = 'active'
              AND (i.owner_user_id = current_user.id OR m.user_id IS NOT NULL)
            """,
            (user_public_id, instance_public_id),
        ).fetchone()
        return instance_dict(row)


def get_instance_by_public_id(instance_public_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        return instance_dict(
            active_conn.execute(
                "SELECT * FROM instances WHERE public_id = ?", (instance_public_id,)
            ).fetchone()
        )


def list_instance_members(instance_public_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        rows = active_conn.execute(
            """
            SELECT u.public_id AS user_public_id,
                   u.username,
                   u.display_name,
                   m.role
            FROM instance_members m
            JOIN instances i ON i.id = m.instance_id
            JOIN users u ON u.id = m.user_id
            WHERE i.public_id = ?
            ORDER BY m.id
            """,
            (instance_public_id,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def remove_instance_member(
    instance_public_id,
    user_public_id,
    *,
    db_file=None,
    conn=None,
):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        cursor = active_conn.execute(
            """
            DELETE FROM instance_members
            WHERE instance_id = (
                SELECT id FROM instances WHERE public_id = ?
            )
              AND user_id = (
                SELECT id FROM users WHERE public_id = ?
            )
            """,
            (instance_public_id, user_public_id),
        )
        return cursor.rowcount > 0


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


def claim_next_execution_job(*, stale_seconds=900, db_file=None):
    with connect(db_file) as conn:
        conn.execute("BEGIN IMMEDIATE")
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        ).replace(microsecond=0).isoformat()
        now = utc_now()
        conn.execute(
            """
            UPDATE execution_jobs
            SET status = 'failed', error_summary = 'execution interrupted; manual confirmation required',
                updated_at = ?, finished_at = ?
            WHERE status = 'running' AND heartbeat_at < ?
              AND action IN ('instance.create', 'instance.delete', 'instance.restore')
            """,
            (now, now, stale_before),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO operation_records (
                request_id, actor_user_id, source_service, action, instance_id,
                status, message, created_at, finished_at
            )
            SELECT request_id, actor_user_id, 'manager-executor', action, instance_id,
                   'failed', 'execution interrupted; manual confirmation required', ?, ?
            FROM execution_jobs
            WHERE status = 'failed'
              AND error_summary = 'execution interrupted; manual confirmation required'
              AND action IN ('instance.create', 'instance.delete', 'instance.restore')
            """,
            (now, now),
        )
        conn.execute(
            """
            UPDATE instances SET status = 'failed', updated_at = ?
            WHERE status = 'provisioning' AND id IN (
                SELECT instance_id FROM execution_jobs
                WHERE action = 'instance.create' AND status = 'failed'
                  AND error_summary = 'execution interrupted; manual confirmation required'
            )
            """,
            (now,),
        )
        conn.execute(
            """
            UPDATE execution_jobs
            SET status = 'queued', current_step = 'recovered after stale heartbeat',
                updated_at = ?
            WHERE status = 'running' AND heartbeat_at < ?
            """,
            (utc_now(), stale_before),
        )
        now = utc_now()
        conn.execute(
            """
            UPDATE execution_jobs
            SET status = 'failed', error_summary = 'instance not found',
                updated_at = ?, finished_at = ?
            WHERE status = 'queued' AND instance_id IS NULL
            """,
            (now, now),
        )
        if conn.execute(
            "SELECT 1 FROM execution_jobs WHERE status = 'running' LIMIT 1"
        ).fetchone():
            return None, None
        job = conn.execute(
            """
            SELECT job.* FROM execution_jobs job
            JOIN instances instance ON instance.id = job.instance_id
            WHERE job.status = 'queued'
            ORDER BY job.created_at, job.id
            LIMIT 1
            """
        ).fetchone()
        if job is None:
            return None, None
        now = utc_now()
        conn.execute(
            """
            UPDATE execution_jobs
            SET status = 'running', started_at = ?, heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, now, job["id"]),
        )
        claimed = row_to_dict(
            conn.execute(
                """
                SELECT job.*,
                       actor.public_id AS actor_user_public_id,
                       instance.public_id AS instance_public_id
                FROM execution_jobs job
                LEFT JOIN users actor ON actor.id = job.actor_user_id
                LEFT JOIN instances instance ON instance.id = job.instance_id
                WHERE job.id = ?
                """,
                (job["id"],),
            ).fetchone()
        )
        instance = instance_dict(
            conn.execute(
                "SELECT * FROM instances WHERE id = ?", (job["instance_id"],)
            ).fetchone()
        )
        return claimed, instance


def get_execution_job(request_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        return row_to_dict(
            active_conn.execute(
                """
                SELECT job.*,
                       actor.public_id AS actor_user_public_id,
                       instance.public_id AS instance_public_id
                FROM execution_jobs job
                LEFT JOIN users actor ON actor.id = job.actor_user_id
                LEFT JOIN instances instance ON instance.id = job.instance_id
                WHERE job.request_id = ?
                """,
                (request_id,),
            ).fetchone()
        )


def list_execution_jobs(
    status=None,
    statuses=None,
    limit=100,
    *,
    actor_user_public_id=None,
    instance_public_id=None,
    action=None,
    parent_request_id=None,
    newest_first=False,
    db_file=None,
    conn=None,
):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        conditions = []
        params = []
        if status:
            conditions.append("job.status = ?")
            params.append(status)
        if statuses:
            conditions.append(
                f"job.status IN ({','.join('?' for _ in statuses)})"
            )
            params.extend(statuses)
        if actor_user_public_id:
            conditions.append("actor.public_id = ?")
            params.append(actor_user_public_id)
        if instance_public_id:
            conditions.append("instance.public_id = ?")
            params.append(instance_public_id)
        if action:
            conditions.append("job.action = ?")
            params.append(action)
        if parent_request_id:
            conditions.append("job.parent_request_id = ?")
            params.append(parent_request_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = active_conn.execute(
            f"""
            SELECT job.*,
                   actor.public_id AS actor_user_public_id,
                   instance.public_id AS instance_public_id
            FROM execution_jobs job
            LEFT JOIN users actor ON actor.id = job.actor_user_id
            LEFT JOIN instances instance ON instance.id = job.instance_id
            {where}
            ORDER BY job.created_at {"DESC" if newest_first else "ASC"},
                     job.id {"DESC" if newest_first else "ASC"}
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


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


def list_instances(status=None, db_file=None, conn=None, *, limit=None, offset=0):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        query = "SELECT * FROM instances"
        params = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, legacy_user_id ASC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        rows = active_conn.execute(query, params).fetchall()
        return [instance_dict(row) for row in rows]


def set_instance_basic_auth(instance_public_id, enabled, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        result = active_conn.execute(
            "UPDATE instances SET basic_auth_enabled = ?, updated_at = ? WHERE public_id = ?",
            (1 if enabled else 0, utc_now(), instance_public_id),
        )
        if result.rowcount != 1:
            raise ValueError("instance not found")


def set_instance_version(instance_public_id, version, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        result = active_conn.execute(
            "UPDATE instances SET openclaw_version = ?, updated_at = ? WHERE public_id = ?",
            (version, utc_now(), instance_public_id),
        )
        if result.rowcount != 1:
            raise ValueError("instance not found")


def set_instance_runtime_status(instance_public_id, status, *, db_file=None, conn=None):
    if status not in {"active", "stopped"}:
        raise ValueError("invalid instance runtime status")
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        result = active_conn.execute(
            "UPDATE instances SET status = ?, updated_at = ? "
            "WHERE public_id = ? AND status IN ('active', 'stopped')",
            (status, utc_now(), instance_public_id),
        )
        if result.rowcount != 1:
            raise ValueError("instance not found")


def set_instance_retention_state(instance_public_id, action, *, db_file=None, conn=None):
    if action not in {"delete", "restore"}:
        raise ValueError("invalid instance retention action")
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        now = utc_now()
        deleting = action == "delete"
        result = active_conn.execute(
            """
            UPDATE instances
            SET status = ?, restore_state = ?, deleted_at = ?, updated_at = ?
            WHERE public_id = ?
            """,
            (
                "deleted" if deleting else "active",
                "restorable" if deleting else "not_applicable",
                now if deleting else None,
                now,
                instance_public_id,
            ),
        )
        if result.rowcount != 1:
            raise ValueError("instance not found")
        instance = get_instance_by_public_id(instance_public_id, conn=active_conn)
        endpoint_status = "inactive" if deleting else "active"
        active_conn.execute(
            "UPDATE instance_endpoints SET status = ?, updated_at = ? WHERE instance_id = ?",
            (endpoint_status, now, instance["id"]),
        )
        if instance.get("port") is not None:
            record_port(
                instance["port"], instance_id=instance["id"],
                status="allocated", conn=active_conn,
            )


def purge_failed_instance(instance_public_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            "SELECT id, status FROM instances WHERE public_id = ?", (instance_public_id,)
        ).fetchone()
        if row is None:
            raise ValueError("instance not found")
        if row["status"] != "failed":
            raise ValueError("only failed instances can be cleaned up")
        now = utc_now()
        active_conn.execute(
            "UPDATE ports SET instance_id = NULL, status = 'released', released_at = ? "
            "WHERE instance_id = ? OR port = (SELECT port FROM instances WHERE id = ?)",
            (now, row["id"], row["id"]),
        )
        active_conn.execute("DELETE FROM instances WHERE id = ?", (row["id"],))


def purge_deleted_instance(instance_public_id, *, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        row = active_conn.execute(
            "SELECT id, status, restore_state FROM instances WHERE public_id = ?",
            (instance_public_id,),
        ).fetchone()
        if row is None:
            raise ValueError("instance not found")
        if row["status"] != "deleted" or row["restore_state"] != "restorable":
            raise ValueError("only restorable deleted instances can be permanently deleted")
        now = utc_now()
        active_conn.execute(
            "UPDATE ports SET instance_id = NULL, status = 'released', released_at = ? "
            "WHERE instance_id = ? OR port = (SELECT port FROM instances WHERE id = ?)",
            (now, row["id"], row["id"]),
        )
        active_conn.execute("DELETE FROM instances WHERE id = ?", (row["id"],))


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


def record_port(port, user_id=None, status="allocated", conn=None, *, instance_id=None):
    now = utc_now()
    released_at = now if status == "released" else None
    owns_conn = conn is None
    context = connect() if owns_conn else nullcontext(conn)
    with context as active_conn:
        if instance_id is None and user_id:
            instance_id = instance_id_for_legacy_user(user_id, active_conn)
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


def list_operation_events(limit=100, *, offset=0, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        rows = active_conn.execute(
            """
            SELECT o.request_id,
                   o.actor,
                   o.user_id,
                   actor.public_id AS actor_user_public_id,
                   instance.public_id AS instance_public_id,
                   o.source_service,
                   o.action,
                   o.status,
                   o.message,
                   o.created_at,
                   o.finished_at
            FROM operation_records o
            LEFT JOIN users actor ON actor.id = o.actor_user_id
            LEFT JOIN instances instance ON instance.id = o.instance_id
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def record_activity_snapshot(
    instance_public_id,
    *,
    status,
    source_version=None,
    source_schema=None,
    source_cursor=None,
    metrics=None,
    error_summary=None,
    db_file=None,
    conn=None,
):
    if status not in {"success", "failed"}:
        raise ValueError("invalid activity snapshot status")
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        instance = active_conn.execute(
            "SELECT id FROM instances WHERE public_id = ?", (instance_public_id,)
        ).fetchone()
        if instance is None:
            raise ValueError("instance not found")
        inserted = active_conn.execute(
            """
            INSERT OR IGNORE INTO activity_snapshots (
                instance_id, status, source_version, source_schema,
                source_cursor, metrics_json, error_summary, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance["id"], status, source_version, source_schema,
                source_cursor, json.dumps(metrics or {}, sort_keys=True),
                error_summary, utc_now(),
            ),
        )
        if inserted.rowcount == 0 and status == "success":
            selector = (
                "snapshot.instance_id = ? "
                "AND snapshot.status = 'success' "
                "AND snapshot.source_cursor = ?"
            )
            selector_params = (instance["id"], source_cursor)
        else:
            selector = "snapshot.id = ?"
            selector_params = (inserted.lastrowid,)
        row = active_conn.execute(
            """
            SELECT snapshot.*, instance.public_id AS instance_public_id,
                   instance.product, instance.instance_name,
                   owner.public_id AS owner_user_public_id,
                   owner.username AS owner_username,
                   owner.display_name AS owner_display_name
            FROM activity_snapshots snapshot
            JOIN instances instance ON instance.id = snapshot.instance_id
            JOIN users owner ON owner.id = instance.owner_user_id
            WHERE """ + selector + " LIMIT 1",
            selector_params,
        ).fetchone()
        value = row_to_dict(row)
        value["metrics"] = json.loads(value.pop("metrics_json"))
        return value


def list_latest_activity_snapshots(*, db_file=None, conn=None):
    owns_conn = conn is None
    context = connect(db_file) if owns_conn else nullcontext(conn)
    with context as active_conn:
        rows = active_conn.execute(
            """
            SELECT snapshot.*, instance.public_id AS instance_public_id,
                   instance.product, instance.instance_name,
                   owner.public_id AS owner_user_public_id,
                   owner.username AS owner_username,
                   owner.display_name AS owner_display_name,
                   (SELECT subject FROM user_identities
                    WHERE user_id = owner.id AND provider = 'campus-uis'
                    LIMIT 1) AS owner_uis_user_id
            FROM instances instance
            JOIN users owner ON owner.id = instance.owner_user_id
            LEFT JOIN activity_snapshots snapshot ON snapshot.id = (
                SELECT id FROM activity_snapshots
                WHERE instance_id = instance.id
                ORDER BY collected_at DESC, id DESC LIMIT 1
            )
            WHERE instance.status != 'deleted'
            ORDER BY owner.normalized_username, instance.instance_name
            """
        ).fetchall()
        values = []
        for row in rows:
            value = row_to_dict(row)
            value["metrics"] = json.loads(value.pop("metrics_json") or "{}")
            values.append(value)
        return values


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
        "activity_snapshots",
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
