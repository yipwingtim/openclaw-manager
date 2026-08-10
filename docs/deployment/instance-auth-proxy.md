# UIS Instance Authentication Proxy

Hermes and EvoScientist keep their dedicated HTTPS ports because their official
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
Hermes keeps its official Dashboard authentication, so users pass UIS first and
then the Hermes login until the upstream product has a verified way to disable
its built-in authentication.

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
python3 scripts/migrate_instance_auth.py
```

Apply only after reviewing the plan:

```bash
python3 scripts/migrate_instance_auth.py --apply
```

The apply step backs up changed ingress files, updates the shared Nginx Compose
network for Hermes, attaches only EvoScientist ingress containers to
`instance-auth-net`, validates Nginx, and rolls back a failed instance. It does
not attach tenant application containers to `manager-net`, modify Hermes
authentication files, or change the database schema.

After application, run:

```bash
python3 scripts/check_metadata_consistency.py
bash scripts/check_runtime_security.sh
```

Keep the backup directory printed by the migration until browser verification
is complete.
