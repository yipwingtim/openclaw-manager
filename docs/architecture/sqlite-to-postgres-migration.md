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

## Executor port allocation / Executor 端口分配

PostgreSQL deployments require `psycopg[binary]` in the manager-executor image,
with `METADATA_DB_BACKEND=postgres` and `METADATA_DATABASE_URL` passed to the
container. The shared allocator excludes ports assigned to `provisioning`,
`active`, `stopped`, and `failed` instances, matching `idx_instances_live_port`.
A failed database lookup stops allocation without advancing the existing port
counter; it does not fall back to socket checks or the historical SQLite file.

PostgreSQL 部署的 manager-executor 镜像必须安装 `psycopg[binary]`，并配置
`METADATA_DB_BACKEND=postgres` 和 `METADATA_DATABASE_URL`。共享分配器排除
`provisioning`、`active`、`stopped`、`failed` 实例已占用的端口，与数据库唯一索引
保持一致。数据库查询失败时停止分配，不推进已有端口计数器，也不回退读取历史 SQLite。

After updating the code, rebuild with
`bash scripts/deploy_services.sh manager-executor`. This restarts the executor;
avoid deploying while it is processing a lifecycle job. The runtime security
check verifies that the executor can import the driver, connect to PostgreSQL,
and read the instance port column. No data migration is required for this fix.

更新代码后使用 `bash scripts/deploy_services.sh manager-executor` 重建。该操作会重启
executor，应避开实例任务执行期间。运行时校验会检查 executor 的驱动、数据库连接及
实例端口读取能力。本修复不需要数据迁移。
