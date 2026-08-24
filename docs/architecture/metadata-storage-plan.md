# Metadata Storage Plan

## 1. 文档目标

本文档用于定义 OpenClaw Manager 从分散文件记录逐步引入数据库的方案。

当前系统已经具备 Web 管理、实例创建、Basic Auth 开关、文件管理、版本升级检查等能力。随着管理动作增加，实例元数据继续分散在 `users.csv`、`ports.txt`、Nginx 配置、用户目录和容器状态中，会带来一致性和可维护性问题。

本文档的目标是明确：

- 哪些信息应进入数据库
- 哪些信息仍应保留在文件系统
- 审计日志是否需要数据库化
- 第一版数据库如何选型
- 如何兼容现有脚本和生产环境

## 2. 当前状态

当前管理状态主要分布在以下位置：

```text
/data/docker/openclaw-public/users.csv
/data/docker/openclaw-public/ports.txt
/data/docker/openclaw-public/users/<user_id>/
/data/docker/openclaw-public/deleted/
/data/docker/nginx/conf/<user_id>.conf
/data/docker/nginx/compose/docker-compose.yml
/data/docker/nginx/auth/.htpasswd
Docker container state
```

这种方式适合早期脚本化管理，但在 Web 管理能力增加后，问题开始明显：

- `users.csv` 只能表达很少字段，无法记录版本、Basic Auth 状态、创建来源等信息。
- `ports.txt` 只是端口指针，不能完整表达端口占用和历史。
- Nginx 配置是运行配置，不适合作为业务状态的唯一来源。
- 容器状态是实时状态，不适合作为实例元数据的唯一来源。
- Web 页面需要跨文件、脚本和 Docker API 拼装状态，逻辑会越来越复杂。

## 3. 设计原则

### 3.1 数据库保存管理元数据

数据库只保存平台管理所需的结构化状态，例如实例、端口、版本、认证开关、操作记录。

数据库不保存用户生成的大文件、workspace 文件、uploads 文件、skills 目录或 OpenClaw 内部运行数据。

### 3.2 文件系统继续保存用户资产

以下内容继续保存在文件系统：

- `workspace`
- `workspaces`
- `uploads`
- `skills`
- `extensions`
- `config`
- 回收站中的用户目录

原因是这些数据属于实例资产或产品运行数据，天然适合文件系统，也方便备份、迁移和人工排障。

### 3.3 审计日志优先使用 JSON Lines 文件

审计日志第一版建议继续使用文件，而不是直接写数据库。

推荐路径：

```text
/data/docker/openclaw-public/logs/manager-web/audit.log
```

每行一条 JSON：

```json
{"time":"2026-06-03T10:00:00+08:00","actor":"openclaw","action":"create_instance","user_id":"linjue","status":"success","message":"port=30019"}
```

原因：

- 审计日志是 append-only，文件天然适合。
- 出问题时可以直接 `tail`、`grep`、复制和归档。
- 即使数据库迁移或损坏，审计记录仍可独立保留。

后续如果需要复杂查询，可以异步导入数据库，而不是一开始就强依赖数据库。

## 4. 数据库选型

当前单机部署和本地开发继续使用 SQLite。新三节点生产拓扑的 Control Plane 使用
部署在节点 A 独立持久化卷上的 PostgreSQL；两种数据库通过同一控制面数据接口和迁移
工具保持兼容。

推荐路径：

```text
/data/docker/openclaw-public/manager.db
```

SQLite 仍适合作为测试和本地开发数据库，原因：

- 当前部署是单机管理平面，不需要分布式数据库。
- 不需要新增数据库容器，部署和恢复简单。
- Python 标准库内置 `sqlite3`，不增加依赖。
- 适合先把元数据模型建立起来，再决定是否迁移 PostgreSQL。

三节点生产拓扑已经满足切换到 PostgreSQL 的条件：

- 多节点运行时管理
- Control Plane 与多个 Runtime Node 并发交互
- 需要独立备份和后续数据库高可用

PostgreSQL 首期接受节点 A 单点故障，但必须将定时备份复制到节点 B、C 或独立存储。
具体节点注册、实例 `node_id` 和跨节点执行设计见[静态多节点运行设计](static-multi-node-runtime.md)。

## 5. 当前基线模型

数据库模型已经落地在 `db/schema.sql`，并由 `services/manager-web/metadata_store.py`
和迁移脚本维护。当前基线包括：

- `users`、`user_identities`、`local_credentials`、`user_sessions` 和认证设置；
- `instances`、`instance_members`、`instance_credentials`、`instance_endpoints`；
- `ports`、`operation_records`、`execution_jobs` 和 `activity_snapshots`；
- Hermes Session bridge 相关表及 `schema_migrations` 版本记录。

控制面通过实例 UUID 解析 `runtime_identifier`，浏览器不能直接提交容器名、宿主机路径
或 Shell 命令。`instances.port` 当前仍是单机兼容字段；静态多节点生产需要增加
`runtime_nodes`、`instances.node_id`，并将端口唯一性改为 `(node_id, port)`。

以下字段说明保留作为逻辑数据字典；旧的单机字段不是新生产 schema 的完整定义。

### 5.1 instances（逻辑字段）

保存实例主数据。

```text
id                  integer primary key
user_id             text unique not null
product             text not null default 'openclaw'
node_id             runtime node reference
port                integer
status              text not null
openclaw_version    text
basic_auth_enabled  integer not null default 1
container_name      text
access_url          text
admin_url           text
data_path           text
nginx_conf_path     text
created_at          text not null
updated_at          text not null
deleted_at          text
```

`status` 建议先使用：

```text
active
stopped
deleted
failed
```

注意：容器实时状态仍应从 Docker 查询，数据库中的 `status` 表示平台管理状态。

### 5.2 instance_credentials

保存实例认证相关元数据。

```text
id                      integer primary key
user_id                 text unique not null
basic_auth_username     text
basic_auth_password_ref text
openclaw_token          text
created_at              text not null
updated_at              text not null
```

现有实现按实例保存凭据引用；OpenClaw token 是否持久化仍受产品迁移策略约束，Basic Auth
密码不作为普通明文元数据保存。

Basic Auth 密码不建议长期明文保存。可选策略：

- 第一版只保存 `basic_auth_username` 和状态，不保存密码。
- 如果确实需要导出账号，使用短期内存记录或单独加密存储。
- `.htpasswd` 仍是 Nginx Basic Auth 的实际认证来源。

### 5.3 ports

保存端口分配状态。

```text
port        integer primary key
user_id     text
status      text not null
created_at  text not null
released_at text
```

`status` 建议先使用：

```text
allocated
released
reserved
```

`ports.txt` 可以在过渡期继续作为脚本兼容指针，但数据库应逐步成为端口状态的主记录。

### 5.4 operation_records

保存重要管理动作的结构化结果摘要。它不同于审计日志：

- 审计日志用于完整追踪，建议文件化。
- `operation_records` 用于 Web 页面展示最近操作结果和状态。

```text
id          integer primary key
actor       text
action      text not null
user_id     text
status      text not null
message     text
created_at  text not null
finished_at text
```

第一版可以只写关键动作：

- `create_instance`
- `delete_instance`
- `start_instance`
- `stop_instance`
- `restart_instance`
- `set_basic_auth`
- `update_version`

文件上传、文件删除、device approval 可以先只进入 `audit.log`，等需要页面查询时再进入数据库。

## 6. 与现有文件的关系

### 6.1 users.csv

过渡期继续保留。

建议阶段：

1. 创建实例时同时写 `users.csv` 和 SQLite。
2. Web 管理页面优先读 SQLite。
3. 如果 SQLite 中没有记录，则 fallback 到 `users.csv` 和目录扫描。
4. 稳定后，`users.csv` 退化为导出文件或兼容文件。

### 6.2 ports.txt

过渡期继续保留。

建议阶段：

1. 创建脚本优先从 SQLite `ports` 表复用 `released` 且宿主机可用的端口。
2. 没有可复用端口时，使用 `ports.txt` 游标继续分配。
3. 创建成功后写入 SQLite `ports` 表；永久删除和失败清理会标记端口为 `released`。
4. `ports.txt` 作为兼容回退指针保留，不作为端口状态的权威来源。

### 6.3 Nginx 配置

Nginx 配置仍由脚本生成，是运行时配置，不是业务状态主来源。

数据库中只记录：

- `port`
- `nginx_conf_path`
- `basic_auth_enabled`

实际配置仍以 `/data/docker/nginx/conf/<user_id>.conf` 为准。

### 6.4 Docker 状态

数据库不保存实时容器健康状态。

Web 页面应继续从 Docker API 查询：

- running
- exited
- restarting
- healthy

数据库中的实例状态只表达平台生命周期，例如 `active`、`deleted`。

## 7. 当前实现与 PostgreSQL 迁移计划

### 已完成：SQLite 控制面基线

- `db/schema.sql`、初始化脚本和版本迁移脚本已存在；
- Web 和 Control Plane 已使用结构化实例、成员、凭据、端点、操作及执行任务模型；
- 旧 `users.csv`、`ports.txt`、Nginx 配置和 Docker 状态仍保留为兼容/运行时来源，不再是
  新控制面功能的唯一业务来源；
- 历史实例元数据已通过兼容迁移进入 SQLite，运行目录仍按原节点保留。

### 计划：迁移到三节点生产 PostgreSQL

1. 在不改变 Control Plane 数据接口的前提下，为 SQLite 和 PostgreSQL 提供同一逻辑模型；
2. 为 PostgreSQL 增加等价 schema/migration，包含 `runtime_nodes`、`instances.node_id`、
   任务租约、`(node_id, port)` 和 `(ingress_id, host, normalized_path)` 约束；
3. 只读导入并校验所有平台用户、外部身份、认证配置、实例元数据、Activity、操作记录、
   审计索引和历史执行结果；
4. 少于 10 个仍需使用的实例再迁移完整数据目录和产品配置，并重新分配到 A/B/C；
5. 历史实例仅保留元数据，标记 `archived`/`historical`，`node_id = NULL`，禁止生产调度；
6. 双份备份并执行抽样恢复验证后，才切换新生产 Control Plane；旧服务器保留 SQLite、
   实例目录和旧运行记录作为只读查询与回滚副本。

### 已废弃的早期计划

本文档旧版本中“下一步新增 `db/schema.sql`、`scripts/init_metadata_db.sh`、
`metadata_store.py`，再从零开始双写”的表述已完成或不再准确。保留该段历史仅用于说明
演进过程，不应据此重复创建文件或改造现有调用方。

## 8. 风险和注意事项

### 8.1 数据库不能成为单点风险

SQLite 文件应放在 `/data/docker/openclaw-public`，并纳入备份。

如果数据库丢失，系统应能从以下来源重建大部分元数据：

- `users.csv`
- 用户目录
- Nginx conf
- Docker 容器列表

### 8.2 不要一次性迁移所有脚本

现有脚本已经在生产环境使用。数据库应先双写和只读验证，不应立即让所有脚本强依赖数据库。

### 8.3 认证信息要谨慎处理

OpenClaw token 可以作为实例访问信息保存，但 Basic Auth 明文密码不建议长期保存。

后续如果需要保存密码，应引入加密和权限控制，而不是直接写入普通数据库字段。

### 8.4 审计日志和操作记录要区分

审计日志用于追责和排障，应尽量完整、append-only。

操作记录用于 Web 展示，可以更简短，也可以被归档或清理。

## 9. 推荐下一步

建议下一步先完成 PostgreSQL 兼容 schema 与只读导入校验，再安排小规模实例数据迁移。
在此之前继续使用现有 SQLite 测试路径，不提前切换生产数据库。
