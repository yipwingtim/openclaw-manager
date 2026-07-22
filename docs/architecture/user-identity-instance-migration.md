# User, identity, and instance metadata migration

Schema version 2 separates platform users, authentication identities, and
managed instances. It does not rename containers, move active instance data,
change ports, or update Nginx during migration.

## Model

- Database relationships use integer primary keys.
- API-facing users and instances have UUID public IDs.
- Usernames are unique after Unicode NFKC normalization and case folding.
- Migrated identities use `provider=legacy` and the original user ID as their
  subject.
- `legacy_user_id` remains available for old scripts and manager-web routes.
- Credentials, ports, endpoints, and operation records reference instance IDs.
- Deleted instances receive an owner and retain their instance ID.

Only the newest recycle payload is considered, matching `restore_user.sh`.
Current-layout payloads require both `user/docker-compose.yml` and the saved
Nginx user configuration; legacy-layout payloads also require a known port.
Missing or incomplete payloads are marked `incomplete`; the Web UI and
lifecycle handler do not allow those records to be restored.

## Production procedure

Stop manager-web writes before migrating and identify the configured database:

```bash
grep '^METADATA_DB_FILE=' config/openclaw-manager.env
```

Reconcile the existing database, CSV, directories, Compose files, Nginx
configuration, ports, and deleted recycle payloads before changing the schema:

```bash
python3 scripts/check_metadata_consistency.py
```

Do not apply the migration while this command reports errors. Warnings for
known incomplete deleted payloads may remain; those instances will be migrated
with `restore_state=incomplete` and cannot be restored from the Web UI.

Run the read-only plan first:

```bash
python3 scripts/migrate_identity_instance_model.py \
  --db /data/docker/openclaw-public/manager.db \
  --public-dir /data/docker/openclaw-public \
  --dry-run
```

The plan aborts on case-insensitive username or runtime-identifier collisions.
Resolve every reported conflict before applying.

Apply the migration:

```bash
python3 scripts/migrate_identity_instance_model.py \
  --db /data/docker/openclaw-public/manager.db \
  --public-dir /data/docker/openclaw-public \
  --apply
```

Apply creates a consistent SQLite backup named
`manager.db.pre-v2-<timestamp>.bak` before changing the database. The schema
change itself runs in one transaction and performs a foreign-key check before
commit.

## Recovery

If validation after deployment fails, stop manager-web and restore the backup
as the configured `METADATA_DB_FILE`. Database rollback does not require
container, directory, port, or Nginx rollback because migration does not modify
those resources.
