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
- 用户列表：list_users.sh

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

2. OpenClaw Token  
   Basic Auth 通过后，用户还需要输入对应实例的 OpenClaw Login Token。

3. OpenClaw Device Approval  
   新浏览器或新设备首次访问 Control UI 时，OpenClaw 可能要求管理员批准设备。管理员进入对应容器后执行：

   `openclaw devices approve --latest`

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
- 启动 openclaw_<user_id> 容器
- 自动 reload Nginx

创建成功后，可通过：

```text
https://<服务器IP>:<PORT>
```

访问 OpenClaw WebUI。

---

### 2️⃣ 首次登录与设备审批

用户首次访问 WebUI 时，需要完成 Device Pairing。

管理员可执行：

```bash
./scripts/approve_device.sh <user_id>
```

查看待审批设备。

审批最新请求：

```bash
./scripts/approve_device.sh <user_id> --latest
```

系统同时会定时刷新设备缓存：

```text
/data/docker/openclaw-public/users/<user_id>/devices.txt
```

用于后续只读查询。

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
如果提示设备审批，管理员进入对应容器批准设备

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


### Device Pairing 管理
- approve_device.sh
- refresh_device_cache.sh

### Model Provider 管理
- set_model_provider.sh

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
