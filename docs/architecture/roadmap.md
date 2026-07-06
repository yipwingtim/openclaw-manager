# OpenClaw Manager Roadmap

This document records the current planning direction after the SQLite metadata, Basic Auth isolation, manager network isolation, and consistency-check work.

本文档记录 SQLite 元数据、Basic Auth 隔离、管理网络隔离和一致性检查工作后的后续规划方向。

## Current Baseline | 当前基线

- User instances are managed through Docker Compose and Nginx reverse proxy.
- 用户实例当前通过 Docker Compose 和 Nginx 反向代理管理。
- Each instance uses a per-user compose service name and `openclaw_<user_id>` container name.
- 每个实例使用每用户独立的 compose service 名，并保留 `openclaw_<user_id>` 容器名。
- Instance-local `/admin/` pages use per-instance Basic Auth files.
- 实例端口内的 `/admin/` 页面使用每实例独立 Basic Auth 文件。
- `manager-web` runs on `manager-net`; user containers remain on `agent-net`; Nginx joins both networks.
- `manager-web` 运行在 `manager-net`；用户容器保留在 `agent-net`；Nginx 同时加入两个网络。
- SQLite `manager.db` stores instance metadata, ports, credentials references, and operation records.
- SQLite `manager.db` 保存实例元数据、端口、凭据引用和操作记录。
- `check_metadata_consistency.py` provides read-only consistency checks across SQLite, runtime files, and Nginx state.
- `check_metadata_consistency.py` 提供 SQLite、运行时文件和 Nginx 状态之间的只读一致性检查。

## Priority 1: Fresh Environment Bootstrap | 优先级 1：全新环境初始化

Goal: make the project deployable on a clean Ubuntu host without relying on manually created Nginx runtime files.

目标：让项目可以在全新的 Ubuntu 主机上部署，不依赖手工创建好的 Nginx 运行目录和配置。

Scope:

- Create required runtime directories under `/data/docker/openclaw-public` and `/data/docker/nginx`.
- 创建 `/data/docker/openclaw-public` 和 `/data/docker/nginx` 下的运行目录。
- Generate or validate `config/openclaw-manager.env` from an example file.
- 从示例文件生成或校验 `config/openclaw-manager.env`。
- Create external Docker networks: `agent-net` and `manager-net`.
- 创建外部 Docker 网络：`agent-net` 和 `manager-net`。
- Provide a public Nginx compose template, including ports, volumes, and both networks.
- 提供对外 Nginx compose 模板，包含端口、挂载目录和两个网络。
- Initialize SQLite metadata database.
- 初始化 SQLite 元数据数据库。
- Validate required host dependencies: Docker, Docker Compose plugin, Python 3, Nginx image availability, and `apache2-utils` where needed.
- 校验宿主机依赖：Docker、Docker Compose plugin、Python 3、Nginx 镜像可用性，以及需要时的 `apache2-utils`。
- Document safe deployment order and rollback notes.
- 记录安全部署顺序和回滚说明。

Initial deliverables:

- `scripts/bootstrap_runtime.sh`
- `templates/nginx/docker-compose.tpl.yml`
- README quick-start section for a clean Ubuntu deployment
- `docs/deployment/bootstrap.md`
- Fresh-environment validation checklist

## Priority 2: Service-to-Service Authentication | 优先级 2：服务间认证

Goal: add a shared internal authentication header between Nginx and `manager-web`.

目标：在 Nginx 和 `manager-web` 之间增加内部共享认证 header。

Scope:

- Configure Nginx to inject an internal header for `/instance-admin/` proxy requests.
- 配置 Nginx 在代理 `/instance-admin/` 请求时注入内部 header。
- Configure `manager-web` to reject trusted internal routes when the header is missing or invalid.
- 配置 `manager-web` 在内部可信路由缺失或 header 无效时拒绝请求。
- Store the secret outside the repository in environment/config files.
- 将密钥存放在仓库外的环境或配置文件中。
- Update deployment docs and issue response.
- 更新部署文档和 issue 回复。

## Priority 3: Metadata Persistence and Repair | 优先级 3：元数据持久化与修复

Goal: move from double-write tracking toward reliable metadata management.

目标：从双写跟踪逐步演进到可靠的元数据管理。

Scope:

- Keep `check_metadata_consistency.py` aligned with business rules whenever lifecycle behavior changes.
- 生命周期逻辑变化时，同步更新 `check_metadata_consistency.py`。
- Add controlled repair commands for known safe cases, such as version sync, Basic Auth sync, deleted-state sync, and released port sync.
- 为已知安全场景增加受控修复命令，例如版本同步、Basic Auth 同步、删除状态同步和端口释放同步。
- Improve metadata import from existing runtime files.
- 改进从现有运行时文件导入元数据的能力。
- Decide when `manager.db` becomes the primary source for admin pages.
- 决定何时让 `manager.db` 成为管理页面的主要数据源。
- Add backup and restore guidance for `manager.db`.
- 增加 `manager.db` 的备份和恢复说明。

## Priority 4: Move Core Lifecycle Logic to Python | 优先级 4：核心生命周期逻辑迁移到 Python

Goal: reduce fragile shell text manipulation and centralize state transitions.

目标：减少易出错的 shell 文本处理，并集中管理状态流转。

Scope:

- Move Nginx config rendering to Python templates.
- 将 Nginx 配置渲染迁移到 Python 模板。
- Move `users.csv`, `ports.txt`, and metadata updates behind a Python state layer.
- 将 `users.csv`、`ports.txt` 和元数据写入收敛到 Python 状态层。
- Keep shell wrappers for operator convenience during migration.
- 迁移期间保留 shell wrapper 方便运维使用。
- Add tests around create, delete, restore, Basic Auth, and version update flows.
- 为创建、删除、恢复、Basic Auth 和版本升级流程增加测试。

## Priority 5: AI Application Adapter Model | 优先级 5：AI 应用适配器模型

Goal: support OpenClaw and other AI agent applications through a shared instance management layer.

目标：通过统一实例管理层支持 OpenClaw 以及其他 AI agent 应用。

Implementation order:

- Extract a thin `InstanceAdapter` around the current lifecycle actions first.
- Keep current behavior in an `OpenClawDockerAdapter`.
- Route lifecycle actions by the instance `product` metadata field, defaulting existing instances to `openclaw`.
- Route Web batch operations through the adapter, starting with batch set model.
- Add a future `K8sAdapter` after the Docker adapter path is stable.

实施顺序：

- 先围绕当前生命周期动作抽出一层很薄的 `InstanceAdapter`。
- 将当前行为保留在 `OpenClawDockerAdapter` 中。
- 生命周期操作按实例元数据里的 `product` 字段选择 adapter，既有实例默认视为 `openclaw`。
- Web 批量操作先接入 adapter，优先从批量设置模型开始。
- Docker adapter 路径稳定后，再增加未来的 `K8sAdapter`。

Scope:

- Introduce application adapter definitions for image, ports, volumes, health checks, auth, and UI routes.
- 引入应用适配器定义，描述镜像、端口、挂载、健康检查、认证和 UI 路由。
- Generalize metadata from `openclaw`-specific fields to product/application fields.
- 将元数据从 OpenClaw 专用字段泛化为产品/应用字段。
- Add adapter templates for future applications such as Hermes.
- 为 Hermes 等未来应用增加适配器模板。
- Keep OpenClaw as the first and default adapter.
- 保持 OpenClaw 作为第一个默认适配器。

## Long-Term: Kubernetes Support | 长期方向：Kubernetes 支持

Goal: support multi-node scheduling, stronger resource isolation, and elastic scaling when the deployment grows beyond a single Docker host.

目标：当部署规模超过单台 Docker 主机时，支持多节点调度、更强资源隔离和弹性伸缩。

This should wait until the Docker Compose deployment is complete, reproducible, and well-tested.

该方向应在 Docker Compose 部署完整、可复现且测试充分后再推进。

## Ongoing Rules | 持续规则

- Any change to lifecycle behavior must update the consistency checker.
- 任何生命周期行为变化都必须同步更新一致性检查脚本。
- Production operations should prefer check-first workflows before mutating files, containers, networks, or databases.
- 生产操作应优先采用先检查、再修改的流程。
- Runtime secrets must stay outside the repository.
- 运行时密钥必须保留在仓库外。
- Existing runtime data should be migrated through explicit, reversible steps.
- 现有运行数据迁移应通过明确、可回滚的步骤完成。
