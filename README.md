# OpenClaw Manager

A lightweight multi-user OpenClaw instance management platform.

一个轻量级的 OpenClaw 多用户实例管理平台。

---

## Platform Support | 平台支持

OpenClaw Manager is currently developed and tested on Ubuntu-based Linux environments with Docker Engine and the Docker Compose plugin.

OpenClaw Manager 目前主要面向 Ubuntu 系 Linux 环境开发和测试，依赖 Docker Engine 与 Docker Compose plugin。

Other Linux distributions may work, but they are not officially validated yet. Some scripts assume Ubuntu/Debian-style packages and tools such as `apt`, `apache2-utils`, `acl`, `setfacl`, and standard GNU utilities.

其他 Linux 发行版可能可以运行，但目前尚未正式验证。部分脚本默认使用 Ubuntu/Debian 风格的软件包和工具，例如 `apt`、`apache2-utils`、`acl`、`setfacl` 以及标准 GNU 工具。

---

## Overview | 项目简介

OpenClaw Manager provides a "one OpenClaw instance per user" deployment model without requiring changes to the upstream OpenClaw codebase.

OpenClaw Manager 提供一种“每用户一个 OpenClaw 实例”的部署模式，不需要修改 OpenClaw 上游源码。

It is designed for user-level isolation, automated provisioning, lifecycle management, and controlled operations in private or internal platforms.

它面向多用户隔离、自动化开通、生命周期管理和私有化平台运维控制。

Typical environments | 适用场景：

- universities and data centers / 高校和数据中心
- internal AI platforms / 内部 AI 平台
- private deployments / 私有化部署环境
- training, labs, and multi-user trials / 培训、实验室和多用户试用场景

---

## Features | 功能特点

### Instance Isolation | 实例隔离

- Each user gets an independent OpenClaw container.
- 每个用户拥有独立的 OpenClaw 容器。
- Runtime state, workspace files, and configuration are isolated per user.
- 运行状态、工作区文件和配置按用户隔离，避免权限和状态污染。

---

### Lifecycle Management | 生命周期管理

- Create instances with `scripts/create_user.sh`.
- 使用 `scripts/create_user.sh` 创建实例。
- Delete instances into a recycle-bin style directory with `scripts/delete_user.sh`.
- 使用 `scripts/delete_user.sh` 回收删除实例数据。
- Restore deleted instances with `scripts/restore_user.sh`.
- 使用 `scripts/restore_user.sh` 恢复已删除实例。
- Upgrade a single instance with `scripts/update_instance_version.sh`.
- 使用 `scripts/update_instance_version.sh` 升级指定实例版本，并在升级前后执行检查。
- Manage instances from the web admin UI: create, start, stop, restart, delete, and toggle Basic Auth.
- 可通过 Web 管理页面创建、启停、重启、删除实例，并切换 Basic Auth。
- Generate WeChat plugin binding URLs from the web UI when the instance container can run the Tencent WeChat OpenClaw CLI through `npx`.
- 当实例容器可通过 `npx` 运行腾讯微信 OpenClaw CLI 时，可在 Web 页面生成微信插件绑定链接。

Typical flow | 典型流程：

```text
Create -> Use -> Delete to recycle bin -> Restore
创建 -> 使用 -> 删除到回收站 -> 恢复
```

---

### Port-Based Access Model | 端口访问模型

Each user is assigned a dedicated HTTPS port. The OpenClaw container does not publish that port directly; Nginx owns the public port and proxies traffic to the matching user container.

每个用户绑定一个独立 HTTPS 访问端口。OpenClaw 容器不直接发布宿主机端口，而是由 Nginx 统一对外监听并反向代理到对应用户容器。

Example | 示例：

```text
userA → https://IP:30000 → nginx → openclaw_userA:18789  
userB → https://IP:30001 → nginx → openclaw_userB:18789
```

Design properties | 设计特点：

- No subpath routing, which avoids WebSocket and basePath compatibility issues.
- 不使用子路径，避免 WebSocket 和 basePath 适配问题。
- No domain dependency; IP-plus-port access works well in intranet and data-center environments.
- 不依赖域名，适合内网和数据中心环境。
- User containers expose only `18789` inside the Docker network.
- 用户容器只在 Docker 网络内暴露 `18789`。
- Nginx centralizes HTTPS, Basic Auth, and reverse proxy configuration.
- 对外入口统一由 Nginx 提供 HTTPS、Basic Auth 和反向代理。

---

### Security Model | 安全机制

OpenClaw Manager uses three layers of access control.

OpenClaw Manager 当前采用三层访问控制。

1. Nginx Basic Auth  
   Users first access `https://IP:PORT` and authenticate through the browser Basic Auth prompt. Credentials are stored in `.htpasswd` and maintained by the management scripts.
   用户首先访问 `https://IP:PORT`，由浏览器弹出 Basic Auth 认证窗口。账号信息保存在 `.htpasswd` 中，并由管理脚本维护。

   Basic Auth for the main OpenClaw route `/` can be disabled per instance for trusted internal training environments.
   实例主页 `/` 的 Basic Auth 可按实例关闭，适用于可信内网培训场景。

   The instance-local `/admin/` route remains protected by Basic Auth.
   实例端口内的 `/admin/` 管理入口始终保留 Basic Auth 保护。

2. OpenClaw Token  
   After Basic Auth, users still need the OpenClaw login token for that instance.
   Basic Auth 通过后，用户仍需输入对应实例的 OpenClaw Login Token。

3. OpenClaw Device Approval  
   New browsers or devices may require approval before accessing the OpenClaw Control UI. Users can approve the latest request from their instance-local `/admin/` page, or an administrator can run:
   新浏览器或新设备首次访问 OpenClaw Control UI 时，可能需要审批。用户可进入自己实例端口的 `/admin/` 页面审批最新设备请求，管理员也可以执行：

   ```bash
   ./scripts/approve_device.sh <user_id> --latest
   ```

4. Docker Network Isolation
   User OpenClaw containers stay on `agent-net`, while `manager-web` runs on a separate `manager-net`. Nginx is the only service that should join both networks, so user containers cannot directly call `manager-web:8080`.
   用户 OpenClaw 容器保留在 `agent-net`，`manager-web` 运行在独立的 `manager-net`。只有 Nginx 应同时加入两个网络，因此用户容器不能直接访问 `manager-web:8080`。

Notes | 说明：

- Basic Auth protects the outer Nginx entry point.
- Basic Auth 用于保护 Nginx 外层入口。
- OpenClaw Token authenticates the OpenClaw application session.
- OpenClaw Token 用于 OpenClaw 应用登录认证。
- Device Approval confirms first-time device pairing.
- Device Approval 用于首次设备配对确认。
- `.htpasswd` is maintained on the host and granted read-only access to the Nginx container user through ACL.
- `.htpasswd` 文件由宿主机维护，并通过 ACL 授权给 Nginx 容器内用户只读访问。

---

### Safe Delete | 安全删除

Deleting an instance moves its data into a recycle-bin directory instead of permanently removing it.

删除实例不会直接删除数据，而是移动到回收站目录。

```text
users/testuser  
→ deleted/testuser_时间戳  
```

The instance can be restored later.

后续可恢复该实例。

---

## 📁 Project Structure | 项目结构

```text
openclaw-manager/
├── config/                 # manager configuration / 管理器配置
│   └── openclaw-manager.env
├── db/                     # SQLite schema / SQLite 表结构
│   └── schema.sql
├── templates/              # docker-compose templates / docker-compose 模板
│   └── docker-compose.tpl.yml
├── scripts/                # management scripts / 管理脚本
│   ├── create_user.sh      # create an instance / 创建实例
│   ├── delete_user.sh      # recycle an instance / 回收实例
│   ├── restore_user.sh     # restore an instance / 恢复实例
│   ├── metadata_cli.py     # SQLite metadata helper / SQLite 元数据写入入口
│   ├── list_users.sh       # list users / 查看用户列表
│   └── update_allowed_origins.sh
├── services/               # manager-web and services / Web 管理端和扩展服务
├── docs/
├── README.md
└── .gitignore
```

Nginx runtime configuration is generated outside this repository under `/data/docker/nginx/`, including user reverse-proxy configs, port mappings, certificates, logs, and the Basic Auth password file.

Nginx 运行时配置不放在项目目录内，而是由脚本生成到 `/data/docker/nginx/`，包括用户反代配置、端口映射、证书、日志和 Basic Auth 密码文件。

---

## 📚 Documentation | 文档索引

- [Agent Hosting Platform Architecture / 架构规划](docs/architecture/agent-hosting-platform.md)
- [Roadmap / 后续规划](docs/architecture/roadmap.md)
- [Fresh Environment Bootstrap / 全新环境初始化](docs/deployment/bootstrap.md)
- [Runtime Security Checks / 运行时安全检查](docs/deployment/runtime-security-checks.md)
- [Model Proxy Deployment / 模型代理部署](docs/deployment/model-proxy.md)
- [Metadata Storage Plan / 元数据存储规划](docs/architecture/metadata-storage-plan.md)
- [Metadata Data Dictionary / 元数据数据字典](docs/architecture/metadata-data-dictionary.md)
- [Internal Proxy Token Deployment / 内部代理令牌部署](docs/deployment/internal-proxy-token.md)
- [User Self-Service Panel / 用户自助面板](docs/user-self-service-panel.md)

---

## 📁 Runtime Structure | 运行时目录（重要）

Runtime data is split across OpenClaw public data, Nginx runtime config, and OpenClaw Manager configuration.

运行时数据主要分为 OpenClaw 公共数据、Nginx 运行配置和 OpenClaw Manager 配置。

### 1. OpenClaw Public Data | OpenClaw 用户运行数据

Path | 路径：

```text
/data/docker/openclaw-public/
```

Structure | 结构：

```text
/data/docker/openclaw-public/
├── users/                  # active instance directories / 运行中实例目录
│   ├── userA/
│   └── userB/
├── deleted/                # recycle bin / 已删除实例回收站
├── ports.txt               # port allocation pointer / 端口分配指针
├── users.csv               # legacy user-port records / 旧版用户端口记录
├── manager.db              # SQLite metadata database / SQLite 元数据数据库
└── logs/                   # script logs / 脚本运行日志
```

Notes | 说明：

- `users/` stores active user instance directories.
- `users/` 保存运行中的用户实例目录。
- `deleted/` stores deleted but restorable instance data.
- `deleted/` 保存已删除但可恢复的实例数据。
- `users.csv` and `ports.txt` remain runtime sources during the metadata migration period.
- 在元数据迁移阶段，`users.csv` 和 `ports.txt` 仍保留为运行来源。
- `manager.db` stores structured metadata for visibility, double-write tracking, and future migration.
- `manager.db` 保存结构化元数据，用于可视化、双写记录和后续迁移。

### 2. Nginx Runtime Config | Nginx 运行配置

Path | 路径：

```text
/data/docker/nginx/
```

Structure | 结构：

```text
/data/docker/nginx/
├── compose/
│   └── docker-compose.yml      # public port mappings / 对外端口映射
├── conf/
│   ├── openclaw.conf           # default entry config / 默认入口配置
│   ├── userA.conf              # user reverse proxy config / 用户反代配置
│   └── userB.conf
├── certs/                      # HTTPS certificates / HTTPS 证书
├── auth/
│   ├── .htpasswd               # manager/global Basic Auth file / 管理端或全局 Basic Auth 文件
│   └── users/
│       └── userA/.htpasswd     # per-instance Basic Auth file / 单实例 Basic Auth 文件
└── logs/                       # Nginx logs / Nginx 日志
```

Notes | 说明：

- Each user has one Nginx config file, such as `userA.conf`.
- 每个用户对应一个独立的 Nginx 配置文件，例如 `userA.conf`。
- Each user config listens on one dedicated HTTPS port.
- 每个用户配置文件监听一个独立 HTTPS 端口。
- Nginx proxies requests to `openclaw_<user_id>:18789`.
- Nginx 将请求反向代理到 `openclaw_<user_id>:18789`。
- Instance-local `/admin/` uses per-instance Basic Auth files under `auth/users/<user_id>/.htpasswd`.
- 实例端口 `/admin/` 使用 `auth/users/<user_id>/.htpasswd` 中的独立 Basic Auth 文件。
- The main workspace URL may disable Basic Auth, but the instance-local `/admin/` page remains protected.
- 主工作区 URL 可以关闭 Basic Auth，但实例端口 `/admin/` 页面仍保持认证保护。

### 3. OpenClaw Manager Config | OpenClaw Manager 配置

Path | 路径：

```text
/data/docker/openclaw-manager/config/openclaw-manager.env
```

This file centralizes deployment paths and Nginx-related paths.

该文件集中管理部署路径和 Nginx 相关路径。

Example | 示例：

```env
OPENCLAW_PUBLIC_DIR=/data/docker/openclaw-public
NGINX_COMPOSE_DIR=/data/docker/nginx/compose
NGINX_COMPOSE_FILE=/data/docker/nginx/compose/docker-compose.yml
NGINX_USERS_CONF_DIR=/data/docker/nginx/conf
NGINX_CERTS_DIR=/data/docker/nginx/certs
NGINX_LOGS_DIR=/data/docker/nginx/logs
NGINX_HTPASSWD_FILE=/data/docker/nginx/auth/.htpasswd
NGINX_HTPASSWD_FILE_IN_CONTAINER=/etc/nginx/auth/.htpasswd
NGINX_CONTAINER_NAME=openclaw-nginx
```

Notes | 说明：

- Update this config first when deployment paths change.
- 修改部署路径时，优先修改该配置文件。
- Scripts should read paths from this file whenever possible.
- 脚本应尽量从该配置文件读取路径，避免硬编码。
- Do not commit secrets, certificates, `.htpasswd`, or production config files to a public repository.
- 不应将敏感信息、证书、`.htpasswd` 或生产配置文件提交到公开仓库。

### Fresh Environment Bootstrap | 全新环境初始化

For a clean Ubuntu host, run the bootstrap script first:

全新 Ubuntu 主机可先执行初始化脚本：

Supported target systems are Ubuntu 22.04 LTS and Ubuntu 24.04 LTS. Install `python3`, Docker Engine, Docker Compose plugin, and `apache2-utils` before running bootstrap.

当前支持目标为 Ubuntu 22.04 LTS 和 Ubuntu 24.04 LTS。执行 bootstrap 前，应先安装 `python3`、Docker Engine、Docker Compose plugin 和 `apache2-utils`。

```bash
./scripts/check_bootstrap_readiness.sh
./scripts/bootstrap_runtime.sh
```

The bootstrap script creates missing runtime directories, external Docker networks, `users.csv`, `ports.txt`, SQLite metadata database, and initial Nginx compose/config files. It does not overwrite existing runtime files and does not start containers.

该脚本会创建缺失的运行目录、外部 Docker 网络、`users.csv`、`ports.txt`、SQLite 元数据数据库和初始 Nginx compose/config 文件。脚本不会覆盖已有运行文件，也不会启动容器。

After bootstrap, review `config/openclaw-manager.env`, place TLS certificate files, create the global manager Basic Auth user, and then start Nginx and manager services.

初始化后，应检查 `config/openclaw-manager.env`，放置 TLS 证书文件，创建全局管理端 Basic Auth 用户，然后再启动 Nginx 和管理端服务。

See [Fresh Environment Bootstrap / 全新环境初始化](docs/deployment/bootstrap.md) for detailed prerequisites and steps.

详细前置条件和步骤见 [Fresh Environment Bootstrap / 全新环境初始化](docs/deployment/bootstrap.md)。

---

## 🧠 Architecture | 架构说明

```text
User Browser
↓
https://IP:PORT
↓
Nginx HTTPS + Basic Auth
↓
openclaw_<user_id>:18789
↓
OpenClaw Gateway / Control UI
```

Design principles | 设计原则：

- Avoid subpath routing to reduce WebSocket and basePath compatibility issues.
- 不使用子路径，避免 WebSocket 和 basePath 适配问题。
- Avoid subdomain dependency; IP-plus-port access is supported.
- 不依赖子域名，适合内网 IP 或单域名多端口访问。
- Use port isolation, with one public HTTPS port per user.
- 使用端口隔离，每个用户对应一个独立访问端口。
- Keep user containers private to the Docker network.
- 用户容器不直接发布宿主机端口，只通过 Docker 内部网络暴露。
- Keep OpenClaw itself close to its native runtime model.
- 保持 OpenClaw 原生运行方式，尽量不修改 OpenClaw 源码。

---
## 🏗 Standard Provisioning Flow | 标准实例开通流程

### 1️⃣ 创建用户实例

进入 OpenClaw Manager 项目目录：

```bash
cd /data/docker/openclaw-manager
```

执行创建命令：

```bash
./scripts/create_user.sh <user_id>
```

也可以在管理页面创建单个实例：

```text
https://<服务器IP>:30015/admin/create-user
```

该页面会调用同一个 `scripts/create_user.sh`，适合临时补开单个实例。批量创建仍建议使用 CSV 批处理脚本。

如需关闭该实例的 Nginx Basic Auth：

```bash
./scripts/create_user.sh <user_id> --basic-auth-enabled false
```

批量创建 CSV 可增加第三列：

```csv
user_id,basic_auth_password,basic_auth_enabled
training01,example-password,false
training02,example-password,true
```

默认值为 `true`。

示例：

```bash
./scripts/create_user.sh xinxizhongxin
```

脚本会自动完成：

- 分配独立 HTTPS 访问端口
- 创建用户运行目录
- 生成 docker-compose.yml
- 创建 Nginx 配置
- 配置 Basic Auth
- 配置实例端口 `/admin/` 自助管理入口
- 启动 openclaw_<user_id> 容器
- 自动 reload Nginx

创建成功后，可通过：

```text
https://<服务器IP>:<PORT>
```

访问 OpenClaw WebUI。

实例自助管理入口：

```text
https://<服务器IP>:<PORT>/admin/
```

该入口用于设备审批、上传文件、查看并下载工作区中的常见导出文件。
中文使用说明页面：

```text
https://<服务器IP>:<PORT>/admin/help
```

下载列表只显示 `workspace`、`workspaces` 和 `uploads` 的顶层文件，避免展示 OpenClaw 运行过程中在子目录生成的大量内部文件。
页面支持删除顶层用户生成文件，但 `AGENTS.md`、`SOUL.md`、`TOOLS.md`、`IDENTITY.md`、`USER.md`、`HEARTBEAT.md`、`BOOTSTRAP.md`、`MEMORY.md` 等核心文件不会提供删除按钮。

如果文件名唯一，也可以使用实例端口内的直链下载：

```text
https://<服务器IP>:<PORT>/admin/files/<filename>
```

例如：

```text
https://<服务器IP>:<PORT>/admin/files/report.pdf
```

---

### 2️⃣ 首次登录与设备审批

用户首次访问 WebUI 时，如果出现 Device Pairing，可以直接访问自己的实例自助管理入口：

```text
https://<服务器IP>:<PORT>/admin/
```

在该页面中可刷新设备缓存并审批最新设备请求。

管理员也可在服务器上执行：


```bash
./scripts/approve_device.sh <user_id>
```

查看待审批设备。

审批最新请求：

```bash
./scripts/approve_device.sh <user_id> --latest
```

新版 OpenClaw CLI 中，`openclaw devices approve --latest` 只预览最新请求。管理脚本会提取该请求的 `requestId`，再显式执行审批。
旧版 OpenClaw CLI 中，如果 `approve --latest` 已经直接完成审批，管理脚本会识别成功输出并兼容处理。

系统同时会定时刷新设备缓存：

```text
/data/docker/openclaw-public/users/<user_id>/devices.txt
```

用于后续只读查询。

如果使用全局管理员入口，也可以访问：

```text
https://<服务器IP>:30015
```

普通用户优先使用自己的实例端口 `/admin/`，避免在实例端口和 `30015` 之间切换。

详细说明见：

```text
docs/user-self-service-panel.md
```

---

### 3️⃣ 配置模型 Provider

模型 Provider 配置采用：

```text
config/model-providers.env
```

该文件不会提交到 Git。

首次使用时：

```bash
cp config/model-providers.env.example config/model-providers.env
```

编辑：

```bash
vim config/model-providers.env
```

示例：

```bash
MODEL_PROVIDER_ID=gpustack
MODEL_ID=gpustack/minimax-m2.1
MODEL_BASE_URL=http://openclaw-model-proxy:8081/v1
MODEL_ALIAS="MiniMax M2.1"
```

真实上游模型服务地址和 API Key 应配置在 `config/openclaw-manager.env`：

```bash
MODEL_PROXY_UPSTREAM_BASE_URL=http://10.x.x.x:18080/v1
MODEL_PROXY_UPSTREAM_API_KEY=xxxxxxxx
```

启用后，实例模型配置会写入 `MODEL_PROXY_PUBLIC_BASE_URL` 和实例级 token；真实上游 URL/API Key 不再写入用户实例。未来如改用独立 API 网关，可将 `MODEL_PROXY_PUBLIC_BASE_URL` 指向外部网关地址，并由外部网关完成鉴权、转发、限流和审计。

`model-proxy` 还会按实例 token 读取 `<user_id>.models` 白名单，限制 `/v1/models` 可见模型和 chat/completions 等请求可调用模型，避免上游 key 的其他模型权限被用户实例直接使用。

详细部署说明见 [Model Proxy Deployment / 模型代理部署](docs/deployment/model-proxy.md)。

配置完成后：

```bash
./scripts/set_model_provider.sh <user_id>
```

示例：

```bash
./scripts/set_model_provider.sh xinxizhongxin
```

脚本会自动：

- 备份 openclaw.json
- 更新模型配置
- 校验 JSON
- 重启对应容器
- 检查容器健康状态

---

### 4️⃣ 验证模型是否生效

访问对应用户 WebUI：

```text
https://<服务器IP>:<PORT>
```

发送测试消息：

```text
你好，请回复“模型配置成功”
```

若模型正常回复，则说明：

- Provider 可连接
- API Key 正常
- Base URL 正常
- 模型配置成功

---

### 5️⃣ 删除与恢复用户

删除用户（进入回收站）：

```bash
./scripts/delete_user.sh <user_id>
```

恢复用户：

```bash
./scripts/restore_user.sh <user_id>
```

用户数据不会立即删除。

## ⚙️ Usage | 使用方式

### 用户声明周期管理
####  1️⃣ 创建用户

进入 OpenClaw Manager 项目目录：

```bash
cd /data/docker/openclaw-manager
```

执行创建命令：
```bash
./scripts/create_user.sh <user_id>
```
示例：
```bash
./scripts/create_user.sh testuser
```
脚本会自动完成以下操作：

为用户分配一个可用端口
创建用户运行目录
生成用户专属 docker-compose.yml
启动 openclaw_<user_id> 容器
生成 Nginx 用户配置文件
将访问端口加入 Nginx docker-compose.yml
创建或更新 Nginx Basic Auth 用户
更新 OpenClaw Control UI allowedOrigins
输出访问地址、Basic Auth 用户名和 OpenClaw Login Token

创建过程中会提示设置该用户的 Nginx Basic Auth 密码。
如果该实例创建时使用 `--basic-auth-enabled false`，则不会创建或要求输入 Basic Auth 密码。

输出示例：

SUCCESS
User: testuser
Port: 30000
Access URL:
https://<服务器IP或域名>:30000

Basic Auth:
username: testuser
password: 创建用户时输入的密码

Login Token:
<OpenClaw Login Token>

首次登录流程：

浏览器访问 https://<服务器IP或域名>:PORT
输入 Nginx Basic Auth 用户名和密码
输入 OpenClaw Login Token
如果提示设备审批，访问 https://<服务器IP或域名>:PORT/admin/ 审批最新设备请求

查看待审批设备:
docker exec -it openclaw_<user_id> openclaw devices list

批准设备：
docker exec -it openclaw_<user_id> openclaw devices approve <requestId>

说明：

新浏览器或新设备首次访问时通常需要 approve device
设备批准后会被 OpenClaw 记住，后续一般不需要重复批准
如果浏览器更换、认证信息变化或设备被 revoke，可能会再次要求审批

---

#### 2️⃣ 查看用户列表

./scripts/list_users.sh

---

#### 3️⃣ 删除用户（温和删除）

./scripts/delete_user.sh <user_id>

---

#### 4️⃣ 恢复用户

./scripts/restore_user.sh <user_id>

---

#### 5️⃣ 升级指定实例的 OpenClaw 版本

升级前应先选择测试实例验证，不要直接批量升级业务实例。

```bash
./scripts/update_instance_version.sh <user_id> <version>
```

如需升级后自动尝试恢复模型 Provider：

```bash
./scripts/update_instance_version.sh <user_id> <version> --restore-model-provider
```

示例：

```bash
./scripts/update_instance_version.sh batchtest004 2026.5.26
```

脚本会：

- 备份该实例的 `docker-compose.yml`
- 备份该实例的 `config`、`skills`、`extensions` 持久化目录
- 升级前执行 `scripts/check_instance_upgrade.sh <user_id>`，检查失败会中止升级
- 只替换该实例的 OpenClaw 镜像 tag
- 执行 `docker compose pull`
- 重新创建该实例容器
- 等待容器进入 `running` / `healthy`
- 升级后再次执行检查，并保存 post-check 报告
- 输出可直接执行的 compose 回滚命令和持久化数据备份路径

升级过程中该实例会短暂不可用。用户数据目录不会删除。升级后应检查设备审批和模型 Provider 配置；如果模型配置缺失，重新执行：

```bash
./scripts/set_model_provider.sh <user_id>
```

也可以单独执行升级检查：

```bash
./scripts/check_instance_upgrade.sh <user_id>
```

升级检查报告保存在：

```text
/data/docker/openclaw-public/users/<user_id>/backups/version-upgrades/<timestamp>/pre-check.txt
/data/docker/openclaw-public/users/<user_id>/backups/version-upgrades/<timestamp>/post-check.txt
```

### Metadata 一致性检查

检查 SQLite 元数据与运行时文件是否一致：

```bash
./scripts/check_metadata_consistency.py
./scripts/check_metadata_consistency.py --user-id <user_id>
./scripts/check_metadata_consistency.py --verbose
```

该检查脚本只读扫描 `manager.db`、`users.csv`、用户目录、Nginx 配置、实例 htpasswd 文件和 Nginx compose 端口映射，用于发现元数据迁移期间的状态差异，不会自动修改生产数据。

### Device Pairing 管理
- approve_device.sh
- refresh_device_cache.sh
- enable_instance_admin.sh

### Basic Auth 管理
- set_basic_auth.sh

已有实例可切换 Basic Auth：

```bash
./scripts/set_basic_auth.sh false <user_id>
docker exec openclaw-nginx nginx -t
docker exec openclaw-nginx nginx -s reload
```

管理员也可以在 `https://<服务器IP>:30015/admin/users` 中直接切换。页面会先备份当前 Nginx 用户配置，执行 `nginx -t`，测试通过后才 reload；失败时自动恢复原配置。

管理员也可以在 `https://<服务器IP>:30015/admin/create-user` 创建单个实例。表单支持选择是否启用 Basic Auth；启用时需要填写 Basic Auth 密码，关闭时不写密码即可。
创建成功后页面会显示访问地址、Basic Auth 状态、OpenClaw Login Token，并支持复制或下载本次创建的 `accounts.csv`。该下载记录保存在 manager-web 进程内存中，manager-web 重启后需要从 `users.csv` 或实例配置中重新查询。

管理员也可以在 `https://<服务器IP>:30015/admin/users` 对单个实例执行 Start、Stop、Restart 和 Delete。Delete 会调用 `scripts/delete_user.sh`，用户目录会进入回收站，并移除对应 Nginx 配置和端口映射。
用户列表默认隐藏 stopped 实例，可通过筛选条件查看全部或指定状态。

重新启用：

```bash
./scripts/set_basic_auth.sh true <user_id>
docker exec openclaw-nginx nginx -t
docker exec openclaw-nginx nginx -s reload
```

### Model Provider 管理
- set_model_provider.sh

---

### Manager Web 运行依赖

`manager-web` 通过 Web 页面调用管理脚本，并需要 Docker API 管理实例容器。容器内需要具备：

- Docker CLI
- Docker Compose plugin
- `/var/run/docker.sock` 挂载
- OpenClaw Manager 项目目录挂载到 `OPENCLAW_MANAGER_DIR`
- OpenClaw public 数据目录挂载
- Nginx conf、auth 和 compose 目录挂载
- Nginx auth root should stay read-only, while `auth/users` must be writable so Web-created instances can create per-instance htpasswd files.
- Nginx auth 根目录建议保持只读，`auth/users` 必须可写，以便 Web 创建实例时生成每实例 htpasswd 文件。
- `manager-web` should only join `manager-net`. The public Nginx container should join both `agent-net` and `manager-net`.
- `manager-web` 应只加入 `manager-net`。对外 Nginx 容器应同时加入 `agent-net` 和 `manager-net`。
- In production, set `OPENCLAW_INTERNAL_TOKEN` and configure Nginx to send `X-OpenClaw-Internal-Token` when proxying to `manager-web`.
- 生产环境建议设置 `OPENCLAW_INTERNAL_TOKEN`，并让 Nginx 代理到 `manager-web` 时发送 `X-OpenClaw-Internal-Token`。启用步骤见 [Internal Proxy Token Deployment / 内部代理令牌部署](docs/deployment/internal-proxy-token.md)。

Production deployment order matters when enabling network isolation:

1. Create the external `manager-net` Docker network.
2. Attach the public Nginx container to `manager-net` while keeping it on `agent-net`.
3. Persist both networks in the public Nginx compose file, otherwise a future Nginx recreate may lose `manager-net`.
4. Pull this repository update and rebuild/recreate `manager-web`.
5. Run `nginx -t` and reload Nginx after `manager-web` is recreated.
6. Verify Nginx can reach `openclaw-manager-web:8080`, and user OpenClaw containers cannot.

启用网络隔离时，生产部署顺序很重要：

1. 创建外部 Docker 网络 `manager-net`。
2. 让对外 Nginx 容器加入 `manager-net`，同时保留在 `agent-net`。
3. 在对外 Nginx 的 compose 文件中持久化两个网络，否则未来重建 Nginx 后可能丢失 `manager-net`。
4. 拉取本仓库更新并重建/重建容器 `manager-web`。
5. `manager-web` 重建后执行 `nginx -t` 并 reload Nginx。
6. 验证 Nginx 可以访问 `openclaw-manager-web:8080`，用户 OpenClaw 容器不能访问。

Web 创建实例时，脚本会在创建完成后把用户目录、用户 Nginx 配置和 `users.csv` 的 owner 归还给宿主机数据目录 owner，避免后续宿主机脚本因为 root-owned 文件失败。

---

## ⚠️ Important Notes | 注意事项

### 🚨 端口策略

端口由 `create_user.sh` 自动分配。

当前策略：

- 从配置的起始端口开始查找
- 自动跳过已经被占用的端口
- 新用户端口由 Nginx 对外发布
- 用户容器自身不直接发布宿主机端口，只 `expose 18789`
- 删除用户时，`delete_user.sh` 会移除对应 Nginx 配置和端口映射，从而释放端口

注意：

- 不要手工在用户容器里配置 `ports:`
- 不要手工长期保留无对应用户配置的 Nginx 端口映射
- 如果发现某个端口被占用但没有对应 `listen PORT ssl;` 的用户配置，说明可能存在孤儿端口映射，需要从 Nginx compose 中清理

---

### 🚨 数据安全

- 禁止手动删除 users 目录
- 必须通过脚本管理

---

### 🚨 子路径限制

不支持：

https://domain/testuser  

原因：

- OpenClaw 不支持 basePath
- WebSocket 会断开（1006）

---

## 🔐 Security Considerations | 安全说明

建议：

- 限制公网访问
- 使用 Token
- 控制插件来源
- 定期清理实例

---

## 🔐 Security Design | 安全设计

系统采用最小权限原则：

- 用户实例彼此隔离
- 用户容器不直接暴露宿主机 Docker 权限
- Nginx 统一负责 HTTPS 与 Basic Auth
- Device Pairing 使用只读缓存机制
- OpenClaw 不直接执行 docker.sock
- 敏感配置通过本地 `.env` 管理
- `.env` 文件不会提交到 Git

危险操作（如 approve）通过受限 wrapper 执行，而不是直接开放 Docker 控制权限。
---


## 🛠 Roadmap | 后续规划

- [x] 用户生命周期管理
- [x] 端口分配系统
- [x] 安全删除与恢复
- [ ] 端口加锁（并发安全）
- [ ] Web 管理界面
- [ ] 配额控制
- [ ] OAuth2 / SSO

---

## 📌 License

MIT

---

## 👤 Author

Maintained for internal multi-user OpenClaw deployment.

面向高校 / 数据中心 / 私有化部署场景设计。
