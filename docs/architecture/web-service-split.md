# Web Service Split

`manager-user-web` and `manager-admin-web` use separate processes and routes.
Neither service mounts the metadata database, Docker socket, repository, or
Nginx configuration. Both resolve authentication and authorization through
`manager-control`.

`manager-user-web` serves `/`, `/me`, `/instances/*`, login callbacks, and the
legacy `/instance-admin/` redirect. Runtime status, logs, devices, and files go
through `manager-executor-api`, which resolves the instance from the actor and
instance UUID before using an Adapter or data path.

`manager-admin-web` serves `/admin/*` after the migrated operations pass parity
verification. The Compose-managed legacy `manager-web` compatibility service
remains available during the rollback window. It retains its existing
privileged mounts, is not published on a host port, and must not serve the user
portal.

`manager-admin-web` cannot call Docker or write metadata directly. Its portal
uses structured Control and Executor operations for instance creation,
lifecycle, version, Basic Auth, Skill, device, retention, and batch actions.

## Remaining migration order

The remaining split is implemented in this order:

1. Stabilize the existing Adapter lifecycle contract and enforce product
   capabilities in the backend and executor. Keep authentication, routes,
   creation, and ingress unchanged in this step.
2. Move the legacy admin screens to `manager-admin-web`, including batch
   creation, version management, Basic Auth, Skill management, device actions,
   metadata views, and operation history. Each screen must retain its current
   behavior before production routing changes.
3. Add structured, allowlisted control and executor actions for every migrated
   privileged operation. Browser requests identify instances by UUID and never
   supply container names, host paths, or shell commands.
4. Move instance creation only after its record, runtime, endpoint, audit, and
   rollback sequence has an explicit contract.
5. Route `/admin/*` to `manager-admin-web`, keep the compatibility container
   during acceptance, then remove its privileged mounts and retire it after a
   tested rollback window.

The legacy compatibility service is therefore transitional, not the target
architecture. Hermes work starts after the Adapter contract and admin/executor
boundaries are stable; it does not require unified HTTPS ingress.

## Production switch

This changes Nginx upstreams and container names. Keep the previous
`openclaw-manager-web` container available until both new Web services have
been verified. Back up the active manager and instance Nginx configuration
before deployment.

1. Build and start `manager-control`, `manager-executor`,
   `manager-executor-api`, `manager-user-web`, and `manager-admin-web`.
2. Verify the user, admin, and legacy compatibility Web containers are reachable
   from Nginx on `manager-net`. Only the legacy compatibility service retains
   Docker, database, and Nginx mounts during the rollback window.
3. Run `scripts/update_manager_auth.sh`. It backs up the manager and instance
   configuration, validates with `nginx -t`, reloads Nginx, and restores the
   backup if validation or reload fails.
4. Verify `/me`, `/instances/{uuid}`, `/admin/users`, local login, instance
   `/admin/`, and one idempotent lifecycle task.
5. Remove the compatibility service only after `/admin/*` has passed production
   acceptance and rollback verification on `manager-admin-web`.

## Rollback

Run `scripts/update_manager_auth.sh --restore <backup-directory>`, restore the
previous Compose revision, and start `openclaw-manager-web`. The metadata
schema is unchanged, so no database rollback is required.
