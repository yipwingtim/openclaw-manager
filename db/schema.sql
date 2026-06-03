PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    product TEXT NOT NULL DEFAULT 'openclaw',
    port INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'stopped', 'deleted', 'failed')),
    openclaw_version TEXT,
    basic_auth_enabled INTEGER NOT NULL DEFAULT 1
        CHECK (basic_auth_enabled IN (0, 1)),
    container_name TEXT,
    access_url TEXT,
    admin_url TEXT,
    data_path TEXT,
    nginx_conf_path TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status);
CREATE INDEX IF NOT EXISTS idx_instances_product ON instances(product);
CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_live_port
ON instances(port)
WHERE port IS NOT NULL AND status IN ('active', 'stopped', 'failed');

CREATE TABLE IF NOT EXISTS instance_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    basic_auth_username TEXT,
    basic_auth_password_ref TEXT,
    openclaw_token TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES instances(user_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ports (
    port INTEGER PRIMARY KEY,
    user_id TEXT,
    status TEXT NOT NULL DEFAULT 'allocated'
        CHECK (status IN ('allocated', 'released', 'reserved')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    released_at TEXT,
    FOREIGN KEY (user_id) REFERENCES instances(user_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ports_status ON ports(status);
CREATE INDEX IF NOT EXISTS idx_ports_user_id ON ports(user_id);

CREATE TABLE IF NOT EXISTS operation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,
    action TEXT NOT NULL,
    user_id TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('success', 'failed', 'skipped', 'running')),
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_operation_records_created_at ON operation_records(created_at);
CREATE INDEX IF NOT EXISTS idx_operation_records_user_id ON operation_records(user_id);
CREATE INDEX IF NOT EXISTS idx_operation_records_action ON operation_records(action);

INSERT OR IGNORE INTO schema_migrations (version, name)
VALUES (1, 'initial_metadata_schema');
