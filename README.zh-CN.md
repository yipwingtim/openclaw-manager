<h1 align="center">OpenClaw Manager</h1>
<p align="center">面向多用户隔离 AI 智能体实例的自托管管理平台。</p>
<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>

> [!IMPORTANT]
> OpenClaw Manager 是独立的社区项目，与 OpenClaw 上游项目不存在隶属关系，也不代表其官方立场。

## 项目简介

OpenClaw Manager 在不修改上游应用代码的前提下，为每个用户开通并管理独立的 AI 智能体实例。项目整合了容器生命周期自动化、Nginx 路由、访问认证、元数据跟踪和 Web 管理界面，适用于私有化平台、高校、实验室和组织内部 AI 服务。

目前主要管理 OpenClaw 实例；适配器层也已支持对登记后的现有 EvoScientist 实例执行生命周期管理。当前能力范围请参阅 [EvoScientist 适配器](docs/evoscientist-adapter.md)。

## 核心特性

- **按用户隔离：** 每个用户拥有独立的容器、工作区、配置和运行状态。
- **生命周期管理：** 支持创建、启动、停止、重启、升级、回收和恢复实例。
- **Web 管理：** 统一管理用户、状态、认证、Skills 和常用运维操作。
- **稳定反向代理：** 使用独立 HTTPS 端口和基于 Docker DNS 的 Nginx 上游。
- **多层访问控制：** 结合 Nginx Basic Auth、应用 Token 和设备审批。
- **可恢复删除：** 删除时将实例数据移入回收目录。
- **元数据可见性：** 使用 SQLite 记录状态，并检查运行数据一致性。
- **多产品扩展：** 通过能力适配器接入其他 AI 智能体运行时。

## 架构概览

```text
                         +--------------------+
                         |   Manager Web UI   |
                         |  生命周期与认证管理  |
                         +----------+---------+
                                    |
                                    v
用户 -> HTTPS 独立端口 -> Nginx -> agent-net -> 用户实例容器
                                    |
                                    +----------> OpenClaw
                                    +----------> EvoScientist

元数据：manager.db + 迁移期 users.csv / ports.txt
运行数据：/data/docker/openclaw-public + /data/docker/nginx
```

每个公网端口由 Nginx 统一监听。Nginx 通过容器名解析上游，因此容器重启后不会依赖失效的旧 IP。长期设计请参阅[智能体托管平台架构](docs/architecture/agent-hosting-platform.md)。

## 环境要求

- Ubuntu 22.04 LTS 或 Ubuntu 24.04 LTS
- Docker Engine 与 Docker Compose plugin
- Bash 与 Python 3
- `apache2-utils`、`acl`、`setfacl`、`flock` 和标准 GNU 工具

其他 Linux 发行版可能可以运行，但尚未正式验证。

## 快速开始

```bash
git clone https://github.com/yipwingtim/openclaw-manager.git /data/docker/openclaw-manager
cd /data/docker/openclaw-manager
cp config/openclaw-manager.env.example config/openclaw-manager.env
vim config/openclaw-manager.env

./scripts/check_bootstrap_readiness.sh
./scripts/bootstrap_runtime.sh
```

Bootstrap 不会安装 Docker、签发 TLS 证书、启动服务或创建生产认证信息。投入生产前，请完整阅读[全新环境初始化](docs/deployment/bootstrap.md)。

```bash
./scripts/create_user.sh <user_id>
sudo -E python3 scripts/check_metadata_consistency.py
```

## 常用操作

```bash
./scripts/list_users.sh
./scripts/update_instance_version.sh <user_id> <version>
./scripts/delete_user.sh <user_id>
./scripts/restore_user.sh <user_id>
```

多数日常操作也可以在 Manager Web 界面完成。只有对应产品适配器声明支持时，页面才会显示该产品专属操作。

## 运行目录

```text
/data/docker/openclaw-public/
├── users/          # 活跃实例数据
├── deleted/        # 可恢复的回收实例数据
├── manager.db      # 结构化元数据
├── users.csv       # 迁移期用户记录
├── ports.txt       # 迁移期端口分配状态
└── logs/           # 脚本日志

/data/docker/nginx/
├── compose/        # Nginx Compose 项目
├── conf/           # 启用和停用的用户配置
├── certs/          # TLS 证书材料
├── auth/           # Basic Auth 数据
└── logs/           # Nginx 日志
```

运行路径可通过 `config/openclaw-manager.env` 配置，并有意放在 Git 仓库之外。

## 文档

- [全新环境初始化](docs/deployment/bootstrap.md)
- [运行时安全检查](docs/deployment/runtime-security-checks.md)
- [用户自助面板](docs/user-self-service-panel.md)
- [EvoScientist 适配器](docs/evoscientist-adapter.md)
- [模型代理部署](docs/deployment/model-proxy.md)
- [元数据存储规划](docs/architecture/metadata-storage-plan.md)
- [元数据数据字典](docs/architecture/metadata-data-dictionary.md)
- [项目路线图](docs/architecture/roadmap.md)

## 安全说明

OpenClaw Manager 会协调高权限容器操作和反向代理配置。生产部署前，应审查认证、Docker Socket 权限、文件系统权限和网络边界。不要提交生产凭据、API Key、证书、`.htpasswd` 文件或运行数据。

请将[运行时安全检查](docs/deployment/runtime-security-checks.md)作为基本运维要求。

## 项目状态

本项目正在持续开发，目前更适合受控的自托管环境。欢迎提交 Issue 和范围明确的 Pull Request；报告问题时，请附上相关操作、容器状态以及脱敏后的日志。

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 开源许可证。Copyright 2026 yipwingtim and OpenClaw Manager contributors.
