# Web Service Split

`manager-user-web` and `manager-admin-web` use separate processes and routes;
`manager-executor-api` is the runtime read API and `manager-executor` is the
privileged structured job worker.
Neither service mounts the metadata database, Docker socket, repository, or
Nginx configuration. Both resolve authentication and authorization through
`manager-control`.

`manager-user-web` serves `/`, `/me`, `/instances/*`, login callbacks, and the
per-instance `/instance-admin/` redirect. Runtime status, logs, devices, and
files go through `manager-executor-api`, which resolves the instance from the
actor and instance UUID before using an Adapter or data path.

`manager-admin-web` now serves the global `/admin/*` portal. Instance HTTPS
ports route `/admin/` to `manager-user-web`, which resolves the legacy instance
identifier to an authorized instance UUID. The legacy `manager-web` service is
absent from current Compose and no longer runs in production.

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

Production acceptance confirmed that active instance and manager Nginx routes
do not reference `openclaw-manager-web:8080`. Privileged runtime mounts now
belong only to the structured Executor services. Adapter lifecycle and ingress
methods consume server-resolved instance records, and product capability checks
are enforced before structured actions execute.

## Production switch

The production switch and rollback observation window are complete. There is no
legacy container or active Nginx upstream. Historical hidden Nginx backups may
still contain the old upstream name and are not loaded by Nginx.

## Rollback

Revert to a known-good split-service release and restore its matching Nginx
configuration. Do not restore a historical Nginx backup that references
`openclaw-manager-web:8080`; that container is no longer deployed.
