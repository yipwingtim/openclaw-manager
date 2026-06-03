# OpenClaw Manager

A lightweight multi-user OpenClaw instance management platform.

一个轻量级的 OpenClaw 多用户实例管理平台。

---

## 🚀 Overview | 项目简介

OpenClaw Manager 提供一种“每用户一个实例”的部署模式，在不修改 OpenClaw 源码的前提下，实现：

- 多用户隔离
- 自动化部署
- 生命周期管理（创建 / 删除 / 恢复）
- 运维可控

适用于：

- 高校 / 数据中心
- 内部 AI 平台
- 私有化部署环境

---

## ✨ Features | 功能特点

### 🧩 实例隔离

- 每用户独立 OpenClaw 容器
- 无共享状态，避免权限污染

---

### ⚙️ 用户生命周期管理

- 创建用户：create_user.sh
- 删除用户（回收站）：delete_user.sh
- 恢复用户：restore_user.sh
- 升级指定实例版本：update_instance_version.sh，升级前后自动执行实例检查
- 用户列表：list_users.sh
- 管理页面创建、启停、删除、Basic Auth 开关

完整流程：

创建 → 使用 → 删除（回收）→ 恢复

---

### 🌐 端口访问模型（核心设计）

每个用户仍然绑定一个独立访问端口，但端口不再由 OpenClaw 容器直接发布，而是由 Nginx 统一对外发布并反向代理到对应用户容器。

示例：

userA → https://IP:30000 → nginx → openclaw_userA:18789  
userB → https://IP:30001 → nginx → openclaw_userB:18789

特点：

- 不使用子路径，避免 WebSocket 和 basePath 适配问题
- 不依赖域名，适合内网和数据中心环境
- 用户容器只 expose 18789，不直接暴露到宿主机
- 对外入口统一由 Nginx 提供 HTTPS、Basic Auth 和反向代理
- 每个用户实例仍保持独立容器、独立配置和独立工作区

---

### 🔐 安全机制

当前采用三层访问控制：

1. Nginx Basic Auth  
   用户首先访问 `https://IP:PORT`，由 Nginx 弹出用户名 / 密码认证窗口。账号信息保存在 `.htpasswd` 文件中，由 `create_user.sh` 自动创建或更新。
   Basic Auth 可按实例关闭，适用于可信内网培训场景。

2. OpenClaw Token  
   Basic Auth 通过后，用户还需要输入对应实例的 OpenClaw Login Token。

3. OpenClaw Device Approval  
   新浏览器或新设备首次访问 Control UI 时，OpenClaw 可能要求管理员批准设备。用户可进入自己实例端口的 `/admin/` 页面审批最新设备请求，也可由管理员在服务器执行：

   `./scripts/approve_device.sh <user_id> --latest`

说明：

- Basic Auth 用于外层入口保护
- OpenClaw Token 用于实例登录认证
- Device Approval 用于首次设备配对确认
- `.htpasswd` 文件由宿主机脚本维护，并通过 ACL 授权给 nginx 容器内用户只读访问

---

### ♻️ 安全删除机制

删除用户不会直接删除数据：

users/testuser  
→ deleted/testuser_时间戳  

支持恢复。

---

## 📁 Project Structure | 项目结构

openclaw-manager/
├── config/                 # 管理器配置文件
│   └── openclaw-manager.env
├── templates/              # docker-compose 模板
│   └── docker-compose.tpl.yml
├── scripts/                # 用户管理脚本
│   ├── create_user.sh      # 创建用户实例、生成 nginx 配置、创建 Basic Auth 用户
│   ├── delete_user.sh      # 删除用户实例、回收数据、释放 nginx 端口
│   ├── restore_user.sh     # 恢复用户实例
│   ├── list_users.sh       # 查看用户列表
│   └── update_allowed_origins.sh
├── services/               # 扩展服务（如 pdf-extract）
├── docs/
├── README.md
└── .gitignore
说明：Nginx 的运行时配置不放在项目目录内，而是由脚本生成到 `/data/docker/nginx/`，包括用户反代配置、端口映射、证书、日志和 Basic Auth 密码文件。


---

## 📚 Documentation | 文档索引

- [Agent Hosting Platform 架构规划](docs/architecture/agent-hosting-platform.md)
- [Metadata Storage Plan 元数据存储规划](docs/architecture/metadata-storage-plan.md)
- [Metadata Data Dictionary 元数据数据字典](docs/architecture/metadata-data-dictionary.md)
- [User Self-Service Panel 用户自助面板](docs/user-self-service-panel.md)

---

## 📁 Runtime Structure | 运行时目录（重要）

运行时数据主要分为三部分：

### 1. OpenClaw 用户运行数据

路径：

```text
/data/docker/openclaw-public/
结构如下：
/data/docker/openclaw-public/
├── users/                  # 用户实例目录
│   ├── userA/
│   └── userB/
├── deleted/                # 删除用户后的回收站
├── ports.txt               # 端口分配指针
├── users.csv               # 用户与端口记录
└── logs/                   # 脚本运行日志
说明：

users/：运行中的用户实例
deleted/：已删除但可恢复的用户数据
ports.txt：端口分配起点或当前指针
users.csv：用户、端口、创建时间、状态等记录
logs/：创建、删除等脚本日志
```
### 2. Nginx 运行配置

路径：
```
/data/docker/nginx/
结构如下：
/data/docker/nginx/
├── compose/
│   └── docker-compose.yml      # Nginx 容器 compose 文件，维护对外端口映射
├── conf/
│   ├── openclaw.conf           # 默认 / 主入口配置
│   ├── userA.conf              # 用户 A 的 Nginx 反代配置
│   └── userB.conf              # 用户 B 的 Nginx 反代配置
├── certs/                      # HTTPS 证书
├── auth/
│   └── .htpasswd               # Basic Auth 用户密码文件
└── logs/                       # Nginx 日志
说明：

每个用户对应一个独立的 Nginx 配置文件，例如 userA.conf
每个用户配置文件监听一个独立 HTTPS 端口
Nginx 将请求反向代理到 openclaw_<user_id>:18789
.htpasswd 由 create_user.sh 创建或更新
.htpasswd 通过 ACL 授权给 nginx 容器内用户只读访问
```

### 3. OpenClaw Manager 配置
```
路径：
/data/docker/openclaw-manager/config/openclaw-manager.env
该文件集中管理部署路径和 Nginx 相关路径，例如：
OPENCLAW_PUBLIC_DIR=/data/docker/openclaw-public
NGINX_COMPOSE_DIR=/data/docker/nginx/compose
NGINX_COMPOSE_FILE=/data/docker/nginx/compose/docker-compose.yml
NGINX_USERS_CONF_DIR=/data/docker/nginx/conf
NGINX_HTPASSWD_FILE=/data/docker/nginx/auth/.htpasswd
NGINX_CONTAINER_NAME=openclaw-nginx

说明：

修改部署路径时，优先修改该配置文件
脚本应尽量从该配置文件读取路径，避免硬编码
不应将包含敏感信息的配置文件、证书、.htpasswd 提交到公开仓库
```
---

## 🧠 Architecture | 架构说明

User Browser
↓
https://IP:PORT
↓
Nginx HTTPS + Basic Auth
↓
openclaw_<user_id>:18789
↓
OpenClaw Gateway / Control UI

设计原则：

- 不使用子路径，避免 WebSocket 和 basePath 适配问题
- 不依赖子域名，适合内网 IP 或单域名多端口访问
- 使用端口隔离，每个用户对应一个独立访问端口
- 用户容器不直接发布宿主机端口，只通过 Docker 内部网络暴露 18789
- 对外入口统一由 Nginx 管理，便于集中配置 HTTPS、Basic Auth 和反向代理
- 保持 OpenClaw 原生运行方式，尽量不修改 OpenClaw 源码

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
MODEL_BASE_URL=http://10.x.x.x:18080/v1
MODEL_API_KEY=xxxxxxxx
MODEL_ALIAS="MiniMax M2.1"
```

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
