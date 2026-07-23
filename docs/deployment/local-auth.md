# Manager Web authentication

`manager-web` has one active authentication provider at a time:

- `nginx-basic` (default): Nginx Basic Auth; existing users are mapped to the `nginx-basic` identity provider.
- `local`: the application login page and a server-side session.

Both identity types can belong to the same platform user. Switching the active provider invalidates existing manager sessions. OpenClaw instance Token login is not changed.

## Migrate existing metadata

Run this before deploying the feature. The apply command creates a database backup.

```bash
source config/openclaw-manager.env
```

```bash
python3 scripts/migrate_local_auth_model.py --db "$METADATA_DB_FILE" --admins "${MANAGER_ADMIN_USERS:-openclaw}" --dry-run
python3 scripts/migrate_local_auth_model.py --db "$METADATA_DB_FILE" --admins "${MANAGER_ADMIN_USERS:-openclaw}" --apply
```

## Enable a local user

The password helper uses the manager-web container's installed Werkzeug version. It accepts the password interactively, or from standard input; never place a password in a command argument.

```bash
docker exec -it openclaw-manager-web \
  python /opt/openclaw-manager/scripts/set_local_password.py <username> --role admin
```

The user must already exist in the platform database. To switch manager-web to Local Auth, set the following in `config/openclaw-manager.env`, then deploy services:

```dotenv
MANAGER_AUTH_PROVIDER=local
MANAGER_SESSION_HOURS=8
MANAGER_COOKIE_SECURE=true
```

```bash
bash scripts/deploy_services.sh
```

Switching back to `MANAGER_AUTH_PROVIDER=nginx-basic` and deploying restores the previous manager login path. It does not remove Local credentials.

Future OIDC/UIS providers must pre-provision a matching `user_identities(provider, subject)` record; first login must not create a platform user.
