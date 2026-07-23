# User, Identity, and Instance Migration / 用户、身份与实例迁移

## Purpose / 目标

Schema v2 separates platform users, authentication identities, and managed
instances. Schema v3 adds platform roles, Local authentication credentials,
server-side sessions, and the active authentication-provider setting. Schema
v4 adds instance sharing, separate session kinds, forced password changes,
idempotent execution jobs, and request-linked audit records.

Schema v2 将平台用户、认证身份和托管实例拆分为独立实体。Schema v3 在此基础上增加平台角色、Local 认证凭据、服务端 Session 和当前认证 Provider 设置。Schema v4 增加实例成员、Session 类型、强制改密、幂等执行任务和请求关联审计。

The migration preserves existing containers, ports, active data directories,
Nginx routes, OpenClaw tokens, and legacy manager routes.

迁移不会重命名现有容器、调整端口、移动运行中实例目录、修改 OpenClaw Token 或重建实例。

## Current model / 当前模型

```text
authentication identity
        │ provider + subject
        ▼
users.id ───────────────┐
        │ owner_user_id │
        ▼               │
instances.id            │
        │                │
        ├── instance_credentials
        ├── instance_endpoints
        ├── ports
        └── operation_records
```

- Internal relationships use integer primary keys; API-facing users and
  instances use UUID `public_id` values.
- 数据库内部关系使用 integer 主键，对外用户和实例标识使用 UUID `public_id`。

- Usernames are unique after Unicode NFKC normalization and case folding.
- 用户名经过 Unicode NFKC 归一化和大小写折叠后必须唯一，例如 `Alice` 与 `alice` 不能同时创建。

- One user can own multiple instances. Runtime identifiers and non-null data
  paths remain globally unique.
- 一个用户可以拥有多个实例；运行时标识和非空数据路径仍必须全局唯一。

- One user can bind multiple identities, but only the configured
  `MANAGER_AUTH_PROVIDER` is active for manager login.
- 一个用户可以绑定多个认证身份，但管理端同时只启用 `MANAGER_AUTH_PROVIDER` 指定的一种登录方式。

- `legacy_user_id` preserves compatibility with existing scripts, directories,
  containers, and manager routes.
- `legacy_user_id` 用于兼容现有脚本、目录、容器和管理路由，不再承担平台用户主键职责。

- Deleted instances are retained. Only records with
  `restore_state=restorable` may be restored.
- 已删除实例继续保留；只有 `restore_state=restorable` 的记录可以执行恢复。

The canonical schema is [`db/schema.sql`](../../db/schema.sql). The current
field-level reference is
[`metadata-data-dictionary.md`](metadata-data-dictionary.md).

当前权威 Schema 以 [`db/schema.sql`](../../db/schema.sql) 为准，字段说明见
[`metadata-data-dictionary.md`](metadata-data-dictionary.md)。

## Migration order / 迁移顺序

Existing schema v1 and v2 deployments must run the migrations in this order:

现有 schema v1 和 v2 部署必须按以下顺序迁移：

```text
schema v1 → migrate_identity_instance_model.py → migrate_local_auth_model.py → migrate_control_plane_model.py
schema v2 → migrate_local_auth_model.py → migrate_control_plane_model.py
schema v3 → migrate_control_plane_model.py
```

The identity/instance migration establishes the v2 data model but reads the
current `db/schema.sql`. On a current checkout it may therefore report schema
version 4 immediately. Always run `migrate_local_auth_model.py` afterwards:
that migration also assigns configured administrator roles and creates
`nginx-basic` identities, even when the structural version is already 4.

身份与实例迁移负责建立 v2 数据模型，但会读取当前的 `db/schema.sql`。因此在当前主分支上执行后，数据库可能直接显示 schema version 4。之后仍必须执行 `migrate_local_auth_model.py`：即使结构版本已经是 4，该迁移仍负责设置管理员角色并补充 `nginx-basic` 身份。最后运行 `migrate_control_plane_model.py`；如果结构已经是 v4，该步骤会安全返回。

Do not run both apply operations while `manager-web` can still write to the
database.

执行两个 apply 操作期间必须停止 `manager-web`，避免迁移过程中仍有数据库写入。

## Step 1: preflight / 第一步：迁移前检查

Resolve the configured runtime paths:

确认生产环境实际使用的路径：

```bash
source config/openclaw-manager.env
public_dir="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
db_file="${METADATA_DB_FILE:-$public_dir/manager.db}"
admin_users="${MANAGER_ADMIN_USERS:-openclaw}"
```

Run the consistency checker before changing the schema:

修改 Schema 前先执行一致性检查：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/check_metadata_consistency.py
```

Do not apply a migration while the checker reports errors. Known warnings for
incomplete deleted payloads may remain; those records will be retained with
`restore_state=incomplete` and will not expose a restore action.

存在 error 时不得执行迁移。已知的已删除实例回收数据不完整 warning 可以保留；这类记录会以 `restore_state=incomplete` 保存，管理页面不会提供恢复操作。

Stop manager-web before apply:

执行 apply 前停止 manager-web：

```bash
cd services
docker compose stop manager-web
cd ..
```

## Step 2: establish the identity/instance model / 第二步：建立身份与实例模型

Run the read-only plan first:

先执行只读规划：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_identity_instance_model.py \
  --db "$db_file" \
  --public-dir "$public_dir" \
  --dry-run
```

The preflight rejects normalized-username, runtime-identifier, data-path, and
relation conflicts. Resolve every error before applying.

预检查会拒绝用户名归一化冲突、运行时标识冲突、数据路径冲突和关系完整性错误；所有错误必须先处理。

Apply the identity/instance migration:

应用身份与实例迁移：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_identity_instance_model.py \
  --db "$db_file" \
  --public-dir "$public_dir" \
  --apply
```

The script creates `manager.db.pre-v2-<timestamp>.bak`, performs the schema
change in one transaction, and checks foreign keys before commit.

脚本会生成 `manager.db.pre-v2-<timestamp>.bak`，在单个事务中完成迁移，并在提交前检查外键。

For deleted instances, only the newest recycle payload is inspected. Current
layout payloads require both `user/docker-compose.yml` and a saved Nginx user
configuration. Legacy payloads also require a known port.

已删除实例只检查最新回收数据。当前目录格式必须同时具备 `user/docker-compose.yml` 和保存的 Nginx 用户配置；旧格式还必须能够确定端口。

## Step 3: finalize schema v3 authentication data / 第三步：完成 schema v3 认证数据

Run the Local Auth migration plan:

先执行 Local Auth 迁移规划：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_local_auth_model.py \
  --db "$db_file" \
  --admins "$admin_users"
```

Review the `admins=<count>` value. The migration does not create a missing
platform administrator. If the count is `0`, stop and provision the configured
Basic Auth administrator as a platform user before applying.

必须检查输出中的 `admins=<数量>`。迁移不会自动创建缺失的平台管理员；如果数量为 `0`，应先停止迁移并将配置的 Basic Auth 管理员预置为平台用户。

Apply schema v3:

应用 schema v3：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_local_auth_model.py \
  --db "$db_file" \
  --admins "$admin_users" \
  --apply
```

The script creates `manager.db.pre-v3-<timestamp>.bak`, adds Local Auth and
session tables, assigns configured administrators, and creates
`nginx-basic` identities for migrated legacy users.

脚本会生成 `manager.db.pre-v3-<timestamp>.bak`，增加 Local Auth 与 Session 表、设置配置中的管理员角色，并为迁移后的历史用户补充 `nginx-basic` 身份。

## Step 4: establish the control-plane model / 第四步：建立控制平面模型

先只读检查：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_control_plane_model.py \
  --db "$db_file"
```

确认输出为 `schema v3 -> v4` 后执行：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_control_plane_model.py \
  --db "$db_file" \
  --apply
```

脚本会使用 SQLite backup API 创建 `manager.db.pre-v4-<timestamp>.bak`。既有
Local 凭据会被标记为下次登录必须修改密码；实例、端口、运行目录和容器不会变更。

## Validation / 验证

Verify schema version, ownership, multi-instance support, deleted restore
states, and foreign keys:

验证 Schema 版本、实例归属、多实例约束、已删除实例恢复状态和外键：

```bash
sudo python3 - "$db_file" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
print("schema_version:", conn.execute(
    "SELECT MAX(version) FROM schema_migrations"
).fetchone()[0])
print("users:", conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
print("identities:", conn.execute(
    "SELECT COUNT(*) FROM user_identities"
).fetchone()[0])
print("instances:", conn.execute(
    "SELECT COUNT(*) FROM instances"
).fetchone()[0])
print("instances_without_owner:", conn.execute(
    "SELECT COUNT(*) FROM instances WHERE owner_user_id IS NULL"
).fetchone()[0])
print("owners_with_multiple_instances:", conn.execute("""
    SELECT owner_user_id, COUNT(*)
    FROM instances
    GROUP BY owner_user_id
    HAVING COUNT(*) > 1
""").fetchall())
print("deleted_restore_states:", conn.execute("""
    SELECT restore_state, COUNT(*)
    FROM instances
    WHERE status = 'deleted'
    GROUP BY restore_state
""").fetchall())
print("foreign_key_violations:", conn.execute(
    "PRAGMA foreign_key_check"
).fetchall())
PY
```

An empty `owners_with_multiple_instances` result only means no owner currently
has multiple instances; it does not mean the schema still enforces one
instance per user.

`owners_with_multiple_instances` 为空只表示当前数据中还没有一名用户拥有多个实例，不代表 Schema 仍限制一用户一实例。

Restart the services only after database validation succeeds:

数据库验证通过后再重新部署服务：

```bash
bash scripts/deploy_services.sh
```

## Recovery / 恢复

If post-migration validation fails:

如果迁移后验证失败：

1. Keep `manager-web` stopped.
2. Preserve the failed database for diagnosis.
3. Restore the corresponding `pre-v2`, `pre-v3`, or `pre-v4` backup as the configured
   `METADATA_DB_FILE`.
4. Run `PRAGMA foreign_key_check` and the consistency checker before restarting
   services.

1. 保持 `manager-web` 停止；
2. 保留失败数据库用于排障；
3. 将对应的 `pre-v2` 或 `pre-v3` 备份恢复为配置中的 `METADATA_DB_FILE`；
4. 重启服务前重新执行外键检查和一致性检查。

Database rollback does not require reverting containers, ports, active data
directories, or Nginx because the schema migrations do not modify those
resources.

数据库回滚不需要同步回滚容器、端口、运行中数据目录或 Nginx，因为 Schema 迁移不会修改这些运行资源。
