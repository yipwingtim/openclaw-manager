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
    actor TEXT,
    actor_user_id INTEGER,
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

INSERT OR IGNORE INTO schema_migrations (version, name)
VALUES (2, 'user_identity_instance_model');
