# Web Service Split

`manager-user-web` and `manager-admin-web` use separate processes and routes.
Neither service mounts the metadata database, Docker socket, repository, or
Nginx configuration. Both resolve authentication and authorization through
`manager-control`.

`manager-user-web` serves `/`, `/me`, `/instances/*`, login callbacks, and the
legacy `/instance-admin/` redirect. Runtime status, logs, devices, and files go
through `manager-executor-api`, which resolves the instance from the actor and
instance UUID before using an Adapter or data path.

`manager-admin-web` remains the target for the completed admin migration. Until
all existing admin operations have structured executor actions, `/admin/*` is
routed to the Compose-managed legacy `manager-web` compatibility service. This
service retains its existing privileged mounts, is not published on a host
port, and must not serve the user portal.

`manager-admin-web` cannot call Docker or write metadata directly. Its current
portal supports instance listing and start, stop, and restart. Legacy batch
creation, version, Basic Auth, Skill, and batch-device screens remain on the
compatibility service until each operation is migrated.

## Production switch

This changes Nginx upstreams and container names. Keep the previous
`openclaw-manager-web` container available until both new Web services have
been verified. Back up the active manager and instance Nginx configuration
before deployment.

1. Build and start `manager-control`, `manager-executor`,
   `manager-executor-api`, `manager-user-web`, and `manager-admin-web`.
2. Verify the user, admin, and legacy compatibility Web containers are reachable
   from Nginx on `manager-net`. Only the legacy compatibility service retains
   Docker, database, and Nginx mounts.
3. Run `scripts/update_manager_auth.sh`. It backs up the manager and instance
   configuration, validates with `nginx -t`, reloads Nginx, and restores the
   backup if validation or reload fails.
4. Verify `/me`, `/instances/{uuid}`, `/admin/users`, local login, instance
   `/admin/`, and one idempotent lifecycle task.
5. Remove the compatibility service only after all admin operations have moved
   to structured control and executor APIs and `/admin/*` is routed back to
   `manager-admin-web`.

## Rollback

Run `scripts/update_manager_auth.sh --restore <backup-directory>`, restore the
previous Compose revision, and start `openclaw-manager-web`. The metadata
schema is unchanged, so no database rollback is required.
