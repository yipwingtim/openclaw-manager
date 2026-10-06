# SQLite to PostgreSQL metadata migration

Use `scripts/migrate_metadata_sqlite_to_postgres.py` only when the PostgreSQL
business tables are empty. The script preserves primary keys, imports all
schema-v8 metadata in one PostgreSQL transaction, checks every table count,
and synchronizes identity sequences. It never modifies the SQLite source.

Before applying, stop Manager services that can write metadata and make a
verified copy of `manager.db`. Back up PostgreSQL with the database operator's
normal procedure.

Run the read-only preflight through `manager-control`, where `psycopg`, the
SQLite mount, and the configured database URL are already available:

```bash
docker exec -i openclaw-manager-control python - \
  --sqlite /data/docker/openclaw-public/manager.db \
  < scripts/migrate_metadata_sqlite_to_postgres.py
```

After reviewing all table counts, rerun with `--apply`. Any error rolls back
the complete PostgreSQL transaction. Restart Manager services only after the
reported counts match the SQLite source, then run
`scripts/check_runtime_security.sh` and verify local and UIS login.

Rollback is to keep Manager stopped, restore the PostgreSQL backup (or clear
the failed empty target if the transaction already rolled back), and switch
the configured backend back to the verified SQLite copy.

## Overview compatibility verification

After migration, open Platform Overview (`/admin/metadata`) and verify user,
identity and instance counts. A healthy `/health` response alone does not
exercise database queries. The admin page can return HTTP 200 while showing
an internal metadata API failure. Check manager-control logs as well.

The PostgreSQL cursor adapter must support iteration and preserve named and
positional row access. Identity aggregation uses `identity_user` as its table
alias because PostgreSQL reserves `USER`. The cursor-contract regression test
uses SQLite for fixtures; it does not replace a real PostgreSQL smoke test.
