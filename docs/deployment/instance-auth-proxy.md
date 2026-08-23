# UIS Instance Authentication Proxy

OpenClaw, Hermes and EvoScientist currently keep dedicated HTTPS ports. Their
UIs do not support a shared path prefix. Their ingress authenticates access
through the Manager UIS session before proxying the request.

## Access flow

1. The instance ingress sends an internal `auth_request` to
   `openclaw-instance-auth-proxy` on `instance-auth-net`.
2. The proxy hashes the `openclaw_manager_session` cookie and calls the
   read-only Manager Control authorization endpoint.
3. Manager Control allows active administrators, owners, and instance members.
4. A missing UIS session redirects to
   `https://<PUBLIC_HOST>:30015/login?instance=<instance UUID>`.
   The Manager stores only the instance UUID and resolves the final access URL
   from metadata after login.

EvoScientist no longer uses Nginx Basic Auth after migration. Existing
`.htpasswd` files are retained for rollback but are not referenced by ingress.
Hermes uses its official Dashboard authentication extension point through the
`campus-uis-bridge` Provider. After Manager UIS authorization, the bridge creates
an official Hermes session without a second password. See
[Hermes UIS authentication deployment](hermes-uis-auth.md).

New OpenClaw instances created through Manager use OpenClaw's official
`trusted-proxy` mode. Shared Nginx receives the authenticated UIS user UUID,
overwrites the trusted identity headers, and forwards them from a fixed address
on that instance's tenant network. Existing token instances are unchanged and
remain valid until a separate migration is performed. The same trusted-proxy
configuration can later sit behind a shared domain-and-path route; this change
does not implement that routing.

Inventory all non-deleted OpenClaw instances before planning that migration.
This command is read-only and does not change files, metadata, containers, or
Nginx:

```bash
python3 scripts/inventory_openclaw_auth.py
```

Export the same inventory for review:

```bash
python3 scripts/inventory_openclaw_auth.py --format csv \
  > /tmp/openclaw-auth-inventory.csv
```

The inventory reports `ready` for complete trusted-proxy instances,
`needs-migration` for internally consistent token instances, and `inconsistent`
for unreadable, unsupported, or conflicting configurations. Token instances are
not treated as errors; any inconsistent instance makes the command exit nonzero.
Review the `requires_openclaw_token`, `nginx_basic_auth`, and `issues` columns
before changing an instance.

OpenClaw's local device-management CLI also requires a Gateway control
credential. In `trusted-proxy` mode Manager writes the official local
`gateway.auth.password` field; it never writes `OPENCLAW_GATEWAY_TOKEN`, because
token and trusted-proxy authentication are mutually exclusive. The Adapter
continues to invoke the same OpenClaw CLI, which reads this password from the
mounted instance config.

## Deployment and migration

Generate a dedicated high-entropy value and add it to
`config/openclaw-manager.env`:

```bash
MANAGER_CONTROL_INSTANCE_AUTH_TOKEN=<random dedicated token>
```

Deploy the Control service and authentication proxy before changing any
instance ingress:

```bash
bash scripts/deploy_services.sh manager-control instance-auth-proxy
```

Preview historical instances. This command does not modify files or containers:

```bash
cd /data/docker/openclaw-manager/services

docker compose exec manager-executor \
  python3 /opt/openclaw-manager/scripts/migrate_instance_auth.py
```

Apply only after reviewing the plan:

```bash
docker compose exec manager-executor \
  python3 /opt/openclaw-manager/scripts/migrate_instance_auth.py --apply
```

The apply step first checks every target and dependency before writing anything,
then backs up changed ingress files, updates the shared Nginx Compose
network for Hermes, attaches only EvoScientist ingress containers to
`instance-auth-net`, validates Nginx, and rolls back a failed instance. For
EvoScientist, the ingress container is restarted after its single-file bind
mount configuration is replaced so Docker remounts the new file inode; Docker
restart preserves the container's existing tenant and `instance-auth-net`
attachments. Hermes continues to use `nginx -t` followed by a shared Nginx
reload and is not restarted by this migration. It does not attach tenant
application containers to `manager-net`, modify Hermes authentication files,
or change the database schema.

EvoScientist ingress configuration uses the following lifecycle paths:

- Active and stopped instances: `NGINX_USERS_CONF_DIR/evoscientist-<public_id>.conf`.
  Stopped status is represented by the instance/container state; the file is not
  moved to the recycle tree because the ingress container bind-mounts this file.
- Deleted instances: `OPENCLAW_PUBLIC_DIR/deleted/evoscientist/<public_id>/`,
  containing recycle data only.
- Historical compatibility: `deleted/evoscientist/<public_id>.nginx.conf` is
  read only by migration and consistency checks. New instances never write or
  resolve their ingress from this path.

When a running historical instance is migrated, the script writes the canonical
file, recreates that instance's ingress container with the canonical bind mount,
validates its network and Nginx configuration, then removes the legacy file and
updates `instances.nginx_conf_path`. Any failure restores the legacy file and
recreates the ingress using the old mount. Stopped instances are migrated without
starting them. If both paths exist, migration stops with an explicit conflict
instead of guessing which file is authoritative.

After application, run:

```bash
python3 scripts/check_metadata_consistency.py
bash scripts/check_runtime_security.sh
```

Keep the backup directory printed by the migration until browser verification
is complete.

For existing trusted-proxy OpenClaw instances, preview and apply the local
control-password migration before device approval:

```bash
docker compose exec manager-executor \
  python3 /opt/openclaw-manager/scripts/migrate_openclaw_trusted_proxy_password.py

docker compose exec manager-executor \
  python3 /opt/openclaw-manager/scripts/migrate_openclaw_trusted_proxy_password.py --apply
```

The apply command only updates instance `openclaw.json` files and creates a
backup; it does not restart user instances. Restart only the migrated
OpenClaw instances during an approved maintenance window so the Gateway loads
the password. Do not recreate unrelated instances or restore a Gateway token.

Then run both checks and verify device refresh/approval from the Manager UI:

```bash
python3 scripts/check_metadata_consistency.py
bash scripts/check_runtime_security.sh
```
