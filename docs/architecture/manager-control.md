# Manager Control

`manager-control` is the internal metadata authority for the future
`manager-user-web`, `manager-admin-web`, and `manager-executor` services.
It is attached only to `manager-net` and has no published host port.

The current combined `manager-web` uses this API for user-facing instance and
member authorization. Administrative compatibility paths still access SQLite
directly until the physical service split removes their database mounts.

## Authentication

Each caller has a separate bearer token:

```text
MANAGER_CONTROL_USER_WEB_TOKEN
MANAGER_CONTROL_ADMIN_WEB_TOKEN
MANAGER_CONTROL_EXECUTOR_TOKEN
```

Protected endpoints fail closed when no tokens are configured. Tokens must be
different high-entropy secrets and must not be committed to the repository.

## API scope

```text
GET    /health
GET    /internal/v1/users/{user_public_id}/instances
GET    /internal/v1/instances/{instance_public_id}
GET    /internal/v1/instances/{instance_public_id}/members
POST   /internal/v1/instances/{instance_public_id}/members
PUT    /internal/v1/instances/{instance_public_id}/members/{user_public_id}
DELETE /internal/v1/instances/{instance_public_id}/members/{user_public_id}
GET    /internal/v1/operations
POST   /internal/v1/execution-jobs
GET    /internal/v1/execution-jobs
GET    /internal/v1/execution-jobs/{request_id}
PATCH  /internal/v1/execution-jobs/{request_id}
```

User-facing member operations require
`X-Actor-User-Public-Id`. Control resolves that UUID to an active platform
user and enforces instance ownership or membership from SQLite. Platform
administrators do not receive implicit access to instance content.

Owners may manage all member roles. Managers may manage only operators and
viewers. Member mutations and their audit records commit in one transaction.

Only the admin service may create allowlisted execution jobs. This PR permits
`instance.start`, `instance.stop`, and `instance.restart`; each action accepts
only its documented fields. The executor may list only queued jobs and may
advance job state, while full task history remains admin-only. Reusing a
`request_id` with identical semantics returns the existing job; conflicting
reuse is rejected.

## Deployment

Set three independent tokens in `config/openclaw-manager.env`, then build and
start the internal service:

```bash
cd services
docker compose up -d --build manager-control
docker compose ps manager-control
docker compose logs --tail=100 manager-control
```

The service requires metadata schema v4. It does not run migrations.
