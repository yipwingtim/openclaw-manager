# Manager Control

`manager-control` is the internal metadata authority for the
`manager-user-web`, `manager-admin-web`, and `manager-executor` services.
It is attached only to `manager-net` and has no published host port.

The split user and admin Web services use this API for authentication,
sessions, instance authorization, and execution job creation. They do not
mount SQLite directly.

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
GET    /internal/v1/admin/instances
GET    /internal/v1/instances/{instance_public_id}
GET    /internal/v1/executor/instances/{instance_public_id}
GET    /internal/v1/instances/{instance_public_id}/members
POST   /internal/v1/instances/{instance_public_id}/members
PUT    /internal/v1/instances/{instance_public_id}/members/{user_public_id}
DELETE /internal/v1/instances/{instance_public_id}/members/{user_public_id}
GET    /internal/v1/operations
POST   /internal/v1/execution-jobs
GET    /internal/v1/execution-jobs
POST   /internal/v1/execution-jobs/claim
GET    /internal/v1/execution-jobs/{request_id}
PATCH  /internal/v1/execution-jobs/{request_id}
GET    /internal/v1/auth/session
DELETE /internal/v1/auth/session
GET    /internal/v1/auth/identity
POST   /internal/v1/auth/local-login
POST   /internal/v1/auth/external-login
POST   /internal/v1/auth/emergency-login
```

User-facing member operations require
`X-Actor-User-Public-Id`. Control resolves that UUID to an active platform
user and enforces instance ownership or membership from SQLite. Platform
administrators do not receive implicit access to instance content.

Owners may manage all member roles. Managers may manage only operators and
viewers. Member mutations and their audit records commit in one transaction.

Only the admin service may create allowlisted execution jobs. This PR permits
`instance.start`, `instance.stop`, and `instance.restart`; each action accepts
only its documented fields. The executor atomically claims the oldest queued
job, resolves its instance runtime identifier on the control service, and may
advance job state, while full task history remains admin-only. Reusing a
`request_id` with identical semantics returns the existing job; conflicting
reuse is rejected.

`manager-executor` processes one claimed job at a time. It prechecks start and
stop state, retries failed Adapter calls up to
`MANAGER_EXECUTOR_MAX_ATTEMPTS`, and records progress, heartbeat, output, and
the terminal result through `manager-control`. It accepts no container name or
shell command from callers and has no direct metadata database mount. Control
allows only one running job and requeues it after
`MANAGER_EXECUTOR_STALE_SECONDS` without a heartbeat, so executor restarts do
not leave the queue permanently blocked. Set this timeout above the longest
single Adapter operation.

Resource usage is collected by `manager-executor-api` as part of the explicit
activity-snapshot workflow. `GET /internal/v1/admin/instances` reads the latest
stored snapshot only, so a slow instance filesystem cannot block Control.

## Deployment

Set three independent tokens in `config/openclaw-manager.env`, then build and
start the internal service:

```bash
cd services
docker compose up -d --build manager-control
docker compose ps manager-control
docker compose logs --tail=100 manager-control
```

Start the executor after control is healthy:

```bash
docker compose up -d --build manager-executor
docker compose ps manager-executor
docker compose logs --tail=100 manager-executor
```

The service requires metadata schema v8. It does not run migrations.
