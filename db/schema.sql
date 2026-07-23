PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    normalized_username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'locked', 'deleted')),
    provisioning_source TEXT NOT NULL DEFAULT 'legacy',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    external_username TEXT,
    profile_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (provider, subject)
);

CREATE TABLE IF NOT EXISTS local_credentials (
    user_id INTEGER PRIMARY KEY,
    password_hash TEXT NOT NULL,
    password_changed_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1
        CHECK (must_change_password IN (0, 1)),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    session_kind TEXT NOT NULL DEFAULT 'user'
        CHECK (session_kind IN ('user', 'admin', 'emergency')),
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions(expires_at);

CREATE TABLE IF NOT EXISTS auth_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    legacy_user_id TEXT UNIQUE,
    owner_user_id INTEGER NOT NULL,
    product TEXT NOT NULL DEFAULT 'openclaw',
    instance_name TEXT NOT NULL,
    runtime_identifier TEXT NOT NULL UNIQUE,
    port INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'stopped', 'deleted', 'failed')),
    restore_state TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (restore_state IN ('not_applicable', 'restorable', 'incomplete')),
    openclaw_version TEXT,
    basic_auth_enabled INTEGER NOT NULL DEFAULT 1
        CHECK (basic_auth_enabled IN (0, 1)),
    container_name TEXT,
    access_url TEXT,
    admin_url TEXT,
    data_path TEXT,
    nginx_conf_path TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE RESTRICT,
    UNIQUE (owner_user_id, product, instance_name)
);

CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status);
CREATE INDEX IF NOT EXISTS idx_instances_product ON instances(product);
CREATE INDEX IF NOT EXISTS idx_instances_owner ON instances(owner_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_data_path
ON instances(data_path)
WHERE data_path IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_live_port
ON instances(port)
WHERE port IS NOT NULL AND status IN ('active', 'stopped', 'failed');

CREATE TABLE IF NOT EXISTS instance_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL
        CHECK (role IN ('manager', 'operator', 'viewer')),
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (instance_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_instance_members_user_id
ON instance_members(user_id);

CREATE TRIGGER IF NOT EXISTS prevent_instance_owner_member_insert
BEFORE INSERT ON instance_members
WHEN EXISTS (
    SELECT 1 FROM instances
    WHERE id = NEW.instance_id AND owner_user_id = NEW.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'instance owner cannot be a member');
END;

CREATE TRIGGER IF NOT EXISTS prevent_instance_owner_member_update
BEFORE UPDATE OF instance_id, user_id ON instance_members
WHEN EXISTS (
    SELECT 1 FROM instances
    WHERE id = NEW.instance_id AND owner_user_id = NEW.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'instance owner cannot be a member');
END;

CREATE TRIGGER IF NOT EXISTS prevent_instance_member_becoming_owner
BEFORE UPDATE OF owner_user_id ON instances
WHEN EXISTS (
    SELECT 1 FROM instance_members
    WHERE instance_id = NEW.id AND user_id = NEW.owner_user_id
)
BEGIN
    SELECT RAISE(ABORT, 'instance member must be removed before ownership transfer');
END;

CREATE TABLE IF NOT EXISTS instance_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL UNIQUE,
    basic_auth_username TEXT,
    basic_auth_password_ref TEXT,
    openclaw_token TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS instance_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    endpoint_type TEXT NOT NULL,
    internal_host TEXT,
    internal_port INTEGER,
    external_host TEXT,
    external_port INTEGER,
    external_path TEXT,
    access_url TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive', 'failed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE,
    UNIQUE (instance_id, endpoint_type)
);

CREATE TABLE IF NOT EXISTS ports (
    port INTEGER PRIMARY KEY,
    instance_id INTEGER,
    status TEXT NOT NULL DEFAULT 'allocated'
        CHECK (status IN ('allocated', 'released', 'reserved')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    released_at TEXT,
    FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ports_status ON ports(status);
CREATE INDEX IF NOT EXISTS idx_ports_instance_id ON ports(instance_id);

CREATE TABLE IF NOT EXISTS operation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    actor TEXT,
    actor_user_id INTEGER,
    source_service TEXT,
    action TEXT NOT NULL,
    user_id TEXT,
    instance_id INTEGER,
    status TEXT NOT NULL
        CHECK (status IN ('success', 'failed', 'skipped', 'running')),
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_operation_records_created_at ON operation_records(created_at);
CREATE INDEX IF NOT EXISTS idx_operation_records_user_id ON operation_records(user_id);
CREATE INDEX IF NOT EXISTS idx_operation_records_instance_id ON operation_records(instance_id);
CREATE INDEX IF NOT EXISTS idx_operation_records_action ON operation_records(action);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_records_request_id
ON operation_records(request_id)
WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS execution_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    parent_request_id TEXT,
    actor_user_id INTEGER,
    instance_id INTEGER,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'running', 'succeeded', 'failed',
            'partial_failure', 'interrupted', 'cancelled'
        )),
    current_step TEXT,
    heartbeat_at TEXT,
    error_summary TEXT,
    output TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (parent_request_id) REFERENCES execution_jobs(request_id) ON DELETE SET NULL,
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_jobs_status_created
ON execution_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS idx_execution_jobs_instance_id
ON execution_jobs(instance_id);

INSERT OR IGNORE INTO schema_migrations (version, name)
VALUES (3, 'local_auth_session');

INSERT OR IGNORE INTO schema_migrations (version, name)
VALUES (4, 'control_plane_model');
