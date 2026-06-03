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

第一版建议使用 SQLite。

推荐路径：

```text
/data/docker/openclaw-public/manager.db
```

选择 SQLite 的原因：

- 当前部署是单机管理平面，不需要分布式数据库。
- 不需要新增数据库容器，部署和恢复简单。
- Python 标准库内置 `sqlite3`，不增加依赖。
- 适合先把元数据模型建立起来，再决定是否迁移 PostgreSQL。

暂不建议第一版直接使用 PostgreSQL，除非后续出现以下需求：

- 多台 manager-web 同时写入
- 多节点运行时管理
- 复杂权限模型和查询
- 与统一身份认证、组织架构或其他系统深度集成

## 5. 第一版表结构

### 5.1 instances

保存实例主数据。

```text
id                  integer primary key
user_id             text unique not null
product             text not null default 'openclaw'
port                integer unique
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

第一版可以保存 OpenClaw token，方便 Web 创建结果和 accounts.csv 导出。

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

1. `create_user.sh` 仍按 `ports.txt` 分配端口。
2. 创建成功后写入 SQLite `ports` 表。
3. 后续新增数据库端口分配器，再逐步替换 `ports.txt`。
4. 替换完成前，不删除 `ports.txt`。

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

## 7. 迁移步骤

### Phase 1: 文档和 schema

- 新增本文档。
- 新增 SQLite schema 文件。
- 新增简单的数据库初始化脚本。

### Phase 2: 双写

- `create_user.sh` 成功后写 `users.csv`，同时写 SQLite。
- `delete_user.sh` 成功后更新 `users.csv`，同时更新 SQLite。
- Web 创建实例后可从 SQLite 读取结构化账号信息。

### Phase 3: Web 优先读数据库

- `/admin/users` 优先读 SQLite。
- SQLite 缺失时 fallback 到现有文件和 Docker 状态。
- 增加 `/admin/audit` 或 `/admin/operations` 页面查看最近操作。

### Phase 4: 端口分配迁移

- 新增数据库端口分配器。
- 分配端口时先检查 SQLite、Nginx compose、监听端口。
- `ports.txt` 只作为兼容指针。

### Phase 5: CSV 退化为导出

- `users.csv` 不再作为 Web 主数据源。
- 批量创建后的 `accounts.csv` 仍保留，作为培训分发文件。

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

建议下一步先实现：

1. `db/schema.sql`
2. `scripts/init_metadata_db.sh`
3. `services/manager-web/metadata_store.py`
4. Web 创建实例成功后写入 SQLite
5. `/admin/users` 继续保留现有读取逻辑，先不切换主数据源

这样可以先验证数据库双写，不影响当前生产使用方式。
