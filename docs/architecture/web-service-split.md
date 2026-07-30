# Web Service Split

`manager-user-web` and `manager-admin-web` use separate processes and routes.
Neither service mounts the metadata database, Docker socket, repository, or
Nginx configuration. Both resolve authentication and authorization through
`manager-control`.

`manager-user-web` serves `/`, `/me`, `/instances/*`, login callbacks, and the
per-instance `/instance-admin/` redirect. Runtime status, logs, devices, and
files go through `manager-executor-api`, which resolves the instance from the
actor and instance UUID before using an Adapter or data path.

`manager-admin-web` now serves the global `/admin/*` portal. Instance HTTPS
ports route `/admin/` to `manager-user-web`, which resolves the legacy instance
identifier to an authorized instance UUID. The Compose-managed legacy
`manager-web` service is no longer an active Nginx upstream; it remains only as
a rollback target with its existing privileged mounts.

`manager-admin-web` cannot call Docker or write metadata directly. Its portal
uses structured Control and Executor operations for instance creation,
lifecycle, version, Basic Auth, Skill, device, retention, and batch actions.

## Current status

The service split and global admin migration are complete:

1. Adapter lifecycle methods consume instance records and Control and Executor
   enforce product capabilities.
2. `manager-admin-web` provides instance creation, batch creation, lifecycle,
   retention, version, Basic Auth, Skill, device, model-provider, and metadata
   workflows.
3. Privileged writes use structured, allowlisted jobs addressed by instance
   UUID and executed by `manager-executor` through an Adapter.
4. Production global `/admin/*` traffic routes to `manager-admin-web`.

The remaining split work is limited to verifying historical instance Nginx
configs and removing the legacy container after its rollback window. Until
then, only legacy `manager-web` retains privileged Web-service mounts.

The legacy compatibility service is transitional, not the target architecture.
The Adapter contract and admin/executor boundaries are now stable enough for a
registered-instance Hermes MVP; unified HTTPS ingress is not a prerequisite.

## Production switch

The production switch has been completed. Keep the previous
`openclaw-manager-web` container available only during the rollback window.
Back up the active manager and instance Nginx configuration before removing it
or making any further routing change.

Before retiring the compatibility service, verify `/me`, `/instances/{uuid}`,
global `/admin/*`, local login, instance `/admin/` on both historical and newly
created instances, and one idempotent lifecycle task. Remove the service only
after the split services have passed production acceptance and rollback
verification.

## Rollback

Run `scripts/update_manager_auth.sh --restore <backup-directory>`, restore the
previous Compose revision, and start `openclaw-manager-web`. The metadata
schema is unchanged, so no database rollback is required.
