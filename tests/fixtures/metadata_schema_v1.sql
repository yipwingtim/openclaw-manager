PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    product TEXT NOT NULL DEFAULT 'openclaw',
    port INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    openclaw_version TEXT,
    basic_auth_enabled INTEGER NOT NULL DEFAULT 1,
    container_name TEXT,
    access_url TEXT,
    admin_url TEXT,
    data_path TEXT,
    nginx_conf_path TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE TABLE instance_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    basic_auth_username TEXT,
    basic_auth_password_ref TEXT,
    openclaw_token TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES instances(user_id) ON DELETE CASCADE
);

CREATE TABLE ports (
    port INTEGER PRIMARY KEY,
    user_id TEXT,
    status TEXT NOT NULL DEFAULT 'allocated',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    released_at TEXT,
    FOREIGN KEY (user_id) REFERENCES instances(user_id) ON DELETE SET NULL
);

CREATE TABLE operation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,
    action TEXT NOT NULL,
    user_id TEXT,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

INSERT INTO schema_migrations (version, name)
VALUES (1, 'initial_metadata_schema');
