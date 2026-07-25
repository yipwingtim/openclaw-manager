# Web Service Split

`manager-user-web` and `manager-admin-web` use separate processes and routes.
Neither service mounts the metadata database, Docker socket, repository, or
Nginx configuration. Both resolve authentication and authorization through
`manager-control`.

`manager-user-web` serves `/`, `/me`, `/instances/*`, login callbacks, and the
legacy `/instance-admin/` redirect. Runtime status, logs, devices, and files go
through `manager-executor-api`, which resolves the instance from the actor and
instance UUID before using an Adapter or data path.

`manager-admin-web` serves `/admin/*` and creates allowlisted execution jobs.
It cannot call Docker or write metadata directly. The current admin portal
supports instance listing and start, stop, and restart. Legacy batch creation,
version, Basic Auth, Skill, and batch-device screens are intentionally absent
until each operation has a structured executor action.

## Production switch

This changes Nginx upstreams and container names. Keep the previous
`openclaw-manager-web` container available until both new Web services have
been verified. Back up the active manager and instance Nginx configuration
before deployment.

1. Build and start `manager-control`, `manager-executor`,
   `manager-executor-api`, `manager-user-web`, and `manager-admin-web`.
2. Verify both Web containers are reachable from Nginx on `manager-net` and
   have no Docker, database, or Nginx mounts.
3. Run `scripts/update_manager_auth.sh`. It backs up the manager and instance
   configuration, validates with `nginx -t`, reloads Nginx, and restores the
   backup if validation or reload fails.
4. Verify `/me`, `/instances/{uuid}`, `/admin/instances`, local or external
   login, instance `/admin/`, and one idempotent lifecycle task.
5. Remove the old container only after the checks pass.

## Rollback

Run `scripts/update_manager_auth.sh --restore <backup-directory>`, restore the
previous Compose revision, and start `openclaw-manager-web`. The metadata
schema is unchanged, so no database rollback is required.
