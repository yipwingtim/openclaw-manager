# Metadata Data Dictionary / 元数据数据字典

## 1. 文档范围

本文档描述当前 SQLite metadata schema v4。权威定义以
[`db/schema.sql`](../../db/schema.sql) 为准，生产迁移步骤见
[`user-identity-instance-migration.md`](user-identity-instance-migration.md)。

Schema v4 的核心关系为：

```text
users
├── user_identities
├── local_credentials
├── user_sessions
├── instances
│   ├── instance_members
│   ├── instance_credentials
│   ├── instance_endpoints
│   ├── ports
│   └── operation_records
└── execution_jobs
```

完整审计日志仍保存在：

```text
/data/docker/openclaw-public/logs/manager-web/audit.log
```

`operation_records` 是用于页面展示和结构化查询的操作摘要，不替代审计日志。

## 2. 通用约定

- 表名使用小写复数形式，字段名使用 `snake_case`。
- 数据库内部关系使用 integer 主键。
- 用户和实例的对外标识使用 UUID 字符串 `public_id`。
- 时间字段使用 UTC ISO 8601 字符串或 SQLite `datetime('now')`。
- 布尔值使用 integer：`0=false`、`1=true`。
- 所有连接必须启用 `PRAGMA foreign_keys = ON`。
- 用户名使用 Unicode NFKC + `casefold()` 生成 `normalized_username`。
- `users.id`、`instances.id` 与运行时容器名、目录名、端口相互独立。

## 3. `schema_migrations`

记录已经应用的 Schema 版本。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `version` | integer | primary key | Schema 版本 |
| `name` | text | required | 迁移名称 |
| `applied_at` | text | required | 应用时间 |

当前版本：

```text
4 / control_plane_model
```

## 4. `users`

平台用户主体。用户不是容器、目录或某个具体产品实例。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 内部关系主键 |
| `public_id` | text | unique, required | 对外 UUID |
| `username` | text | required | 用户名原始展示值 |
| `normalized_username` | text | unique, required | NFKC + casefold 后的唯一用户名 |
| `display_name` | text | optional | 展示名称 |
| `email` | text | optional | 邮箱 |
| `role` | text | `admin` / `user` | 平台角色 |
| `status` | text | enum | 平台用户状态 |
| `provisioning_source` | text | required | 用户预置来源，例如 `legacy`、`local` |
| `created_at` | text | required | 创建时间 |
| `updated_at` | text | required | 更新时间 |

`status`：

| Value | Meaning |
| --- | --- |
| `active` | 平台标记为正常启用 |
| `disabled` | 平台标记为禁用 |
| `locked` | 平台标记为锁定 |
| `deleted` | 已删除的平台用户记录 |

关键约束：

- `Alice`、`alice` 等归一化后相同的名称不能创建为两个用户。
- 业务关系必须关联 `users.id`，不得使用外部身份的 subject 作为平台主键。
- 删除用户时，存在实例归属关系会受到 `ON DELETE RESTRICT` 保护。

## 5. `user_identities`

将一个或多个登录身份映射到同一个平台用户。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 身份记录主键 |
| `user_id` | integer | FK → `users.id` | 所属平台用户 |
| `provider` | text | required | Provider 名称 |
| `subject` | text | required | Provider 内稳定唯一标识 |
| `external_username` | text | optional | 外部系统显示用户名 |
| `profile_json` | text | optional | 外部身份原始资料摘要 |
| `created_at` | text | required | 创建时间 |
| `updated_at` | text | required | 更新时间 |
| `last_login_at` | text | optional | 最近登录时间 |

唯一约束：

```text
UNIQUE(provider, subject)
```

当前 Provider：

| Provider | Subject |
| --- | --- |
| `legacy` | 历史 `user_id`，用于兼容迁移 |
| `nginx-basic` | Nginx Basic Auth 用户名 |
| `local` | 归一化后的平台用户名 |

未来 OIDC/UIS Provider 也必须先创建此映射，首次登录不自动创建平台用户。

## 6. `local_credentials`

保存 Local Provider 的密码验证状态，与 `users` 一对一。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `user_id` | integer | PK, FK → `users.id` | 平台用户 |
| `password_hash` | text | required | scrypt 单向密码哈希 |
| `password_changed_at` | text | required | 最近修改密码时间 |
| `must_change_password` | integer | `0` / `1` | 下次登录是否必须修改密码 |
| `failed_login_count` | integer | default `0` | 连续失败次数 |
| `locked_until` | text | optional | 临时锁定截止时间 |
| `created_at` | text | required | 创建时间 |
| `updated_at` | text | required | 更新时间 |

数据库不保存 Local 明文密码或可逆密码。

## 7. `user_sessions`

保存 Local Provider 的服务端 Session。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `token_hash` | text | primary key | 浏览器随机 Token 的 SHA-256 哈希 |
| `user_id` | integer | FK → `users.id` | 登录用户 |
| `provider` | text | required | 创建 Session 的 Provider |
| `session_kind` | text | enum | `user`、`admin` 或 `emergency` |
| `csrf_token` | text | required | POST 请求 CSRF Token |
| `expires_at` | text | required | 过期时间 |
| `created_at` | text | required | 创建时间 |
| `last_seen_at` | text | required | 最近访问时间 |

Provider 发生变化时，现有 Session 会被清空。

## 8. `auth_settings`

记录认证模块的运行状态。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `key` | text | primary key | 设置名 |
| `value` | text | required | 设置值 |
| `updated_at` | text | required | 更新时间 |

当前使用：

```text
active_provider = nginx-basic | local
```

实际部署配置仍以 `MANAGER_AUTH_PROVIDER` 为入口；应用启动或处理认证请求时同步此值，并在发生变化时清除旧 Session。

## 9. `instances`

实例主体。一个用户可以拥有多个实例。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 内部关系主键 |
| `public_id` | text | unique, required | 对外实例 UUID |
| `legacy_user_id` | text | unique, optional | 旧脚本和路由兼容键 |
| `owner_user_id` | integer | FK → `users.id` | 实例所有者 |
| `product` | text | required | 产品类型，例如 `openclaw`、`evoscientist` |
| `instance_name` | text | required | 用户可见实例名称 |
| `runtime_identifier` | text | unique, required | 运行环境中的唯一标识 |
| `port` | integer | optional | 旧版独立 HTTPS 端口 |
| `status` | text | enum | 平台生命周期状态 |
| `restore_state` | text | enum | 已删除实例是否可恢复 |
| `openclaw_version` | text | optional | 兼容的 OpenClaw 版本字段 |
| `basic_auth_enabled` | integer | `0` / `1` | 实例入口 Basic Auth 设置 |
| `container_name` | text | optional | 当前 Docker 容器名 |
| `access_url` | text | optional | 实例访问地址 |
| `admin_url` | text | optional | 兼容管理入口地址 |
| `data_path` | text | unique when non-null | 实例数据路径 |
| `nginx_conf_path` | text | optional | Nginx 配置路径 |
| `metadata_json` | text | optional | 产品或运行时扩展元数据 |
| `created_at` | text | required | 创建时间 |
| `updated_at` | text | required | 更新时间 |
| `deleted_at` | text | optional | 删除时间 |

`status`：

| Value | Meaning |
| --- | --- |
| `active` | 平台标记为启用 |
| `stopped` | 平台标记为停止 |
| `deleted` | 已进入回收状态 |
| `failed` | 创建或迁移失败状态 |

`restore_state`：

| Value | Meaning |
| --- | --- |
| `not_applicable` | 非已删除实例 |
| `restorable` | 回收数据完整，可以恢复 |
| `incomplete` | 仅保留历史记录，不能恢复 |

关键约束：

```text
UNIQUE(runtime_identifier)
UNIQUE(owner_user_id, product, instance_name)
UNIQUE(data_path) WHERE data_path IS NOT NULL
UNIQUE(port) WHERE status IN ('active', 'stopped', 'failed')
```

这些约束允许一个用户拥有多个实例，同时防止运行时、目录和活动端口冲突。

## 10. `instance_members`

保存实例所有者之外的共享授权。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 成员记录主键 |
| `instance_id` | integer | FK → `instances.id` | 被共享实例 |
| `user_id` | integer | FK → `users.id` | 被授权用户 |
| `role` | text | enum | `manager`、`operator` 或 `viewer` |
| `created_by_user_id` | integer | optional FK → `users.id` | 授权发起人 |
| `created_at` | text | required | 创建时间 |
| `updated_at` | text | required | 最近角色更新时间 |

`UNIQUE(instance_id, user_id)` 防止重复授权。实例所有者只保存在
`instances.owner_user_id`；触发器禁止将 owner 重复写入成员表，也禁止在
所有权转移前保留新 owner 的成员记录。

## 11. `instance_credentials`

保存实例自身的认证信息摘要，与平台登录凭据分离。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 记录主键 |
| `instance_id` | integer | unique, FK → `instances.id` | 目标实例 |
| `basic_auth_username` | text | optional | 旧实例入口 Basic Auth 用户名 |
| `basic_auth_password_ref` | text | optional | 密码文件引用，不保存明文 |
| `openclaw_token` | text | optional | OpenClaw 应用 Token |
| `created_at` | text | required | 创建时间 |
| `updated_at` | text | required | 更新时间 |

`local_credentials` 用于平台 Local 登录；`instance_credentials` 用于具体实例，两者不能混用。

## 12. `instance_endpoints`

保存实例访问端点，使实例运行与外部入口解耦。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 端点主键 |
| `instance_id` | integer | FK → `instances.id` | 目标实例 |
| `endpoint_type` | text | unique per instance | 端点类型，例如 `legacy_port` |
| `internal_host` | text | optional | 内部主机 |
| `internal_port` | integer | optional | 内部端口 |
| `external_host` | text | optional | 外部主机或域名 |
| `external_port` | integer | optional | 外部端口 |
| `external_path` | text | optional | 外部路径 |
| `access_url` | text | optional | 完整访问地址 |
| `status` | text | `active` / `inactive` / `failed` | 端点状态 |
| `created_at` | text | required | 创建时间 |
| `updated_at` | text | required | 更新时间 |

独立端口目前仍用于兼容现有实例；未来子域名或统一 HTTPS 入口可增加新的 `endpoint_type`，不需要更换实例主键。

## 13. `ports`

记录独立端口的分配状态。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `port` | integer | primary key | 对外端口 |
| `instance_id` | integer | FK → `instances.id` | 当前或历史实例 |
| `status` | text | `allocated` / `released` / `reserved` | 端口状态 |
| `created_at` | text | required | 创建或分配时间 |
| `released_at` | text | optional | 释放时间 |

`ports.txt` 仍是迁移期兼容文件。数据库记录、Nginx 实际监听和运行配置应通过一致性检查共同验证。

## 14. `operation_records`

保存平台操作摘要。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 记录主键 |
| `request_id` | text | unique when non-null | 与幂等任务关联的请求 ID |
| `actor` | text | optional | 兼容的操作人文本 |
| `actor_user_id` | integer | FK → `users.id` | 平台操作人 |
| `source_service` | text | optional | 发起操作的内部服务 |
| `action` | text | required | 操作类型 |
| `user_id` | text | optional | 兼容的历史目标用户 |
| `instance_id` | integer | FK → `instances.id` | 目标实例 |
| `status` | text | enum | 执行结果 |
| `message` | text | optional | 结果摘要 |
| `created_at` | text | required | 创建时间 |
| `finished_at` | text | optional | 完成时间 |

`status`：

```text
success | failed | skipped | running
```

新增业务逻辑应优先写入 `actor_user_id` 和 `instance_id`；`actor` 与 `user_id` 仅用于历史兼容。

## 15. `execution_jobs`

保存需要 Executor 执行的持久化幂等任务。

| Field | Type | Constraint | Description |
| --- | --- | --- | --- |
| `id` | integer | primary key | 任务主键 |
| `request_id` | text | unique, required | 幂等请求 ID |
| `parent_request_id` | text | optional FK | 重试所关联的原任务 |
| `actor_user_id` | integer | optional FK → `users.id` | 发起人 |
| `instance_id` | integer | optional FK → `instances.id` | 目标实例 |
| `action` | text | required | 白名单动作名 |
| `params_json` | text | required | 经 Control 校验的结构化参数 |
| `status` | text | enum | 任务状态 |
| `current_step` | text | optional | 当前安全步骤 |
| `heartbeat_at` | text | optional | Executor 最近心跳 |
| `error_summary` | text | optional | 脱敏失败摘要 |
| `output` | text | optional | 有大小上限的脱敏输出 |
| `created_at` / `updated_at` | text | required | 创建和更新时间 |
| `started_at` / `finished_at` | text | optional | 执行时间 |

任务状态：

```text
queued | running | succeeded | failed | partial_failure | interrupted | cancelled
```

`queued` 只能进入 `running` 或 `cancelled`；`running` 可刷新心跳并进入任一终态；
终态不能重新打开，重试必须创建新的 `request_id` 并设置 `parent_request_id`。

重复 `request_id` 只能返回语义完全相同的原任务；不得用于另一动作或实例。

## 16. 删除与恢复规则

- 删除实例不会删除其 `users`、`instances` 或历史操作记录。
- 可恢复性由 `instances.restore_state` 决定，不根据页面或 Docker 状态临时猜测。
- `restore_state=incomplete` 的实例仍记录真实使用历史，但不能显示恢复按钮。
- 已删除实例的端点应标记为 `inactive`，端口可以进入 `released`。
- 删除平台用户前必须先处理其仍有关联的实例。

## 17. 一致性验证

数据库变更或生产迁移后至少执行：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/check_metadata_consistency.py
```

数据库级检查：

```sql
SELECT MAX(version) FROM schema_migrations;
PRAGMA foreign_key_check;
```

预期 Schema 版本为 `4`，`PRAGMA foreign_key_check` 返回空结果。
