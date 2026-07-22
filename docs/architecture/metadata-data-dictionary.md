# Metadata Data Dictionary

## 1. 文档目标

本文档定义 OpenClaw Manager 元数据数据库第一版的数据字典。

> 本文前半部分记录旧版 schema v1。当前用户、身份和实例关系及迁移流程见
> `user-identity-instance-migration.md`；schema v2 以 `db/schema.sql` 为准。

该数据字典服务于 SQLite 第一阶段实现，重点约束：

- 表的职责边界
- 字段类型和含义
- 是否必填
- 默认值
- 状态枚举
- 数据来源
- 更新时机
- 与现有文件的对应关系

本文档不是最终数据库设计。第一版目标是让 Web 管理功能先有稳定、可查询的结构化元数据，同时兼容当前脚本和文件体系。

## 2. 命名约定

- 表名使用小写复数形式，例如 `instances`。
- 字段名使用 snake_case。
- 时间字段统一使用 ISO 8601 字符串。
- 布尔值在 SQLite 中使用 integer：
  - `0` 表示 false
  - `1` 表示 true
- 主键第一版使用 integer 自增。
- 外部业务唯一键优先使用 `user_id`，后续多实例模型成熟后再引入独立 `instance_id` 业务键。

## 3. 表清单

| Table | Purpose |
| --- | --- |
| `instances` | 实例主数据，记录实例归属、端口、版本、生命周期状态 |
| `instance_credentials` | 实例认证信息摘要，记录 token 和 Basic Auth 相关引用 |
| `ports` | 端口分配状态，记录端口占用、释放和保留 |
| `operation_records` | 结构化操作记录，用于 Web 页面展示最近操作结果 |

审计日志不在第一版数据库中建表，仍使用 JSON Lines 文件：

```text
/data/docker/openclaw-public/logs/manager-web/audit.log
```

## 4. `instances`

### 4.1 表职责

`instances` 保存实例主数据，是 Web 管理页面的主要元数据来源。

该表不保存实时容器健康状态。实时状态仍从 Docker API 查询。

### 4.2 字段定义

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | integer | yes | auto | 主键 |
| `user_id` | text | yes | none | 当前实例对应的用户标识，第一版唯一 |
| `product` | text | yes | `openclaw` | 产品类型，例如 `openclaw`、`hermes` |
| `port` | integer | no | null | Nginx 对外 HTTPS 端口 |
| `status` | text | yes | `active` | 平台生命周期状态 |
| `openclaw_version` | text | no | null | OpenClaw 镜像版本，例如 `2026.5.26` |
| `basic_auth_enabled` | integer | yes | `1` | 是否启用 Nginx Basic Auth |
| `container_name` | text | no | null | Docker 容器名，例如 `openclaw_linjue` |
| `access_url` | text | no | null | 用户访问 URL |
| `admin_url` | text | no | null | 实例自助管理 URL |
| `data_path` | text | no | null | 用户实例数据目录 |
| `nginx_conf_path` | text | no | null | 用户 Nginx 配置路径 |
| `created_at` | text | yes | current time | 创建时间 |
| `updated_at` | text | yes | current time | 更新时间 |
| `deleted_at` | text | no | null | 删除或进入回收站时间 |

### 4.3 状态枚举

`status` 可选值：

| Value | Meaning | Updated By |
| --- | --- | --- |
| `active` | 实例处于平台可用状态 | `create_user.sh`、`restore_user.sh` |
| `stopped` | 实例被平台停止，但数据仍保留 | manager-web start/stop action |
| `deleted` | 实例已进入回收站 | `delete_user.sh` |
| `failed` | 创建或恢复过程中失败 | create/restore wrapper |

注意：

- Docker 容器可能是 `Restarting`、`Exited` 或 `Healthy`，这些不是 `instances.status`。
- Web 页面展示时应同时显示平台状态和 Docker 实时状态。

### 4.4 数据来源

| Field | Source |
| --- | --- |
| `user_id` | 创建表单或 CSV |
| `port` | `create_user.sh` 分配结果 |
| `openclaw_version` | `OPENCLAW_VERSION` 或实例 compose image tag |
| `basic_auth_enabled` | 创建参数或 `set_basic_auth.sh` |
| `container_name` | `openclaw_<user_id>` |
| `access_url` | `PUBLIC_HOST + port` |
| `admin_url` | `access_url + /admin/` |
| `data_path` | `/data/docker/openclaw-public/users/<user_id>` |
| `nginx_conf_path` | `/data/docker/nginx/conf/<user_id>.conf` |

### 4.5 与现有文件对应关系

| Existing File | Relationship |
| --- | --- |
| `users.csv` | 过渡期继续写；`instances` 是结构化版本 |
| `ports.txt` | 只保存端口指针；`instances.port` 保存实例占用端口 |
| `<user_id>.conf` | 运行配置；`instances.nginx_conf_path` 保存路径 |
| `docker-compose.yml` | 运行配置；`instances.openclaw_version` 可从 image tag 同步 |

## 5. `instance_credentials`

### 5.1 表职责

`instance_credentials` 保存实例认证信息摘要。

该表不应成为 Nginx Basic Auth 的认证源，`.htpasswd` 仍是实际认证文件。

### 5.2 字段定义

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | integer | yes | auto | 主键 |
| `user_id` | text | yes | none | 对应实例用户标识，唯一 |
| `basic_auth_username` | text | no | null | Basic Auth 用户名，通常等于 `user_id` |
| `basic_auth_password_ref` | text | no | null | Basic Auth 密码引用，不建议保存明文 |
| `openclaw_token` | text | no | null | OpenClaw gateway token |
| `created_at` | text | yes | current time | 创建时间 |
| `updated_at` | text | yes | current time | 更新时间 |

### 5.3 密码策略

第一版建议：

- 保存 `openclaw_token`。
- 不长期保存 Basic Auth 明文密码。
- Web 创建成功页仍可通过进程内 `LAST_CREATED_ACCOUNTS` 临时展示 Basic Auth 密码。
- 批量创建导出的 `accounts.csv` 由批处理结果文件承担，不直接依赖数据库长期保存。

如后续确需保存 Basic Auth 密码，必须先实现加密策略和访问权限控制。

### 5.4 更新时机

| Event | Update |
| --- | --- |
| 创建实例成功 | 插入或更新 `basic_auth_username`、`openclaw_token` |
| 重置 OpenClaw token | 更新 `openclaw_token` |
| 切换 Basic Auth | 只更新实例表中的 `basic_auth_enabled`；如用户名变化，再更新本表 |

## 6. `ports`

### 6.1 表职责

`ports` 保存平台端口分配状态。

第一版不立即替代 `ports.txt`，而是作为端口状态记录。

### 6.2 字段定义

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `port` | integer | yes | none | 主键，对外 HTTPS 端口 |
| `user_id` | text | no | null | 当前或历史占用该端口的用户 |
| `status` | text | yes | `allocated` | 端口状态 |
| `created_at` | text | yes | current time | 分配或记录时间 |
| `released_at` | text | no | null | 释放时间 |

### 6.3 状态枚举

| Value | Meaning |
| --- | --- |
| `allocated` | 当前已分配给实例 |
| `released` | 已释放，可重新分配 |
| `reserved` | 人工保留，不自动分配 |

### 6.4 更新时机

| Event | Update |
| --- | --- |
| 创建实例成功 | 插入或更新为 `allocated` |
| 删除实例成功 | 更新为 `released` |
| 人工保留端口 | 更新为 `reserved` |

### 6.5 与 `ports.txt` 的关系

过渡期：

- `ports.txt` 仍是创建脚本的分配指针。
- `ports` 表记录分配结果。
- 如果二者冲突，运维检查应以实际监听端口、Nginx compose 和实例记录共同判断。

后续：

- 端口分配器改为优先查询 `ports` 表。
- `ports.txt` 退化为兼容文件。

## 7. `operation_records`

### 7.1 表职责

`operation_records` 保存结构化操作结果，主要用于 Web 页面展示最近操作。

它不是完整审计日志。完整审计仍写入 JSON Lines 文件。

### 7.2 字段定义

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | integer | yes | auto | 主键 |
| `actor` | text | no | null | 操作人，例如 Basic Auth 用户 |
| `action` | text | yes | none | 操作类型 |
| `user_id` | text | no | null | 被操作实例用户 |
| `status` | text | yes | none | 操作结果 |
| `message` | text | no | null | 简短结果摘要 |
| `created_at` | text | yes | current time | 操作开始时间或记录时间 |
| `finished_at` | text | no | null | 操作结束时间 |

### 7.3 action 枚举

第一版建议支持：

| Value | Meaning |
| --- | --- |
| `create_instance` | 创建实例 |
| `delete_instance` | 删除实例 |
| `start_instance` | 启动实例 |
| `stop_instance` | 停止实例 |
| `restart_instance` | 重启实例 |
| `set_basic_auth` | 切换 Basic Auth |
| `update_version` | 升级实例版本 |
| `approve_device` | 审批设备 |
| `upload_file` | 上传文件 |
| `delete_file` | 删除文件 |

### 7.4 status 枚举

| Value | Meaning |
| --- | --- |
| `success` | 操作成功 |
| `failed` | 操作失败 |
| `skipped` | 操作跳过 |
| `running` | 操作进行中 |

### 7.5 与审计日志的区别

| Item | operation_records | audit.log |
| --- | --- | --- |
| 目标 | Web 展示最近操作 | 追责、排障、长期记录 |
| 存储 | SQLite | JSON Lines 文件 |
| 内容 | 摘要 | 尽量完整 |
| 可清理性 | 可归档/清理 | 应长期保留或归档 |

## 8. 第一版写入规则

### 8.1 创建实例

创建成功后写入：

- `instances`
- `instance_credentials`
- `ports`
- `operation_records`
- `audit.log`

创建失败后写入：

- `operation_records`
- `audit.log`

是否写入 `instances.status=failed` 需要谨慎。第一版建议失败时不创建实例主记录，避免污染实例列表；但应保留操作记录和审计日志。

### 8.2 删除实例

删除成功后：

- `instances.status = deleted`
- `instances.deleted_at = now`
- `ports.status = released`
- 写入 `operation_records`
- 写入 `audit.log`

### 8.3 停止实例

停止成功后：

- `instances.status = stopped`
- 写入 `operation_records`
- 写入 `audit.log`

### 8.4 启动实例

启动成功后：

- `instances.status = active`
- 写入 `operation_records`
- 写入 `audit.log`

### 8.5 Basic Auth 切换

切换成功后：

- `instances.basic_auth_enabled = 0/1`
- 写入 `operation_records`
- 写入 `audit.log`

## 9. 读取规则

### 9.1 Web 用户列表

第一阶段：

1. 继续使用现有读取逻辑。
2. SQLite 只作为补充信息来源。

第二阶段：

1. 优先读取 `instances`。
2. 实时容器状态从 Docker API 查询后合并。
3. SQLite 中没有的实例从 `users.csv` 和目录扫描 fallback。

### 9.2 创建成功页

优先使用当前请求的脚本输出和进程内账号记录。

后续可从数据库补充：

- port
- access_url
- admin_url
- openclaw_token
- basic_auth_enabled

### 9.3 批量导出

培训场景仍建议使用 `accounts.csv`。

数据库可用于补充或重新导出，但不应马上替代批量创建结果文件。

## 10. 待确认问题

以下问题在实现 SQLite 前需要确认：

1. 是否允许数据库保存 OpenClaw token。
2. Basic Auth 明文密码是否完全不入库。
3. 删除后的实例是否仍保留唯一 `user_id`，还是允许同名重建生成新记录。
4. `stopped` 是平台生命周期状态，还是只作为 Docker 实时状态展示。
5. `operation_records` 是否需要保留全部历史，还是只保留最近 N 天。

第一版建议：

- 允许保存 OpenClaw token。
- 不保存 Basic Auth 明文密码。
- 允许同名重建，但旧记录必须是 `deleted`，新记录需要更新同一个 `user_id` 的当前状态。
- `stopped` 作为平台生命周期状态。
- `operation_records` 暂不自动清理。
