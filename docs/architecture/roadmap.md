# OpenClaw Manager Roadmap

This document records the implemented platform baseline and the planned order
for completing the multi-product control plane.

本文档记录当前已经落地的平台基线，以及完成多产品控制平面的后续实施顺序。

## Current Baseline | 当前基线

- SQLite separates platform users, external identities, local credentials,
  sessions, instances, members, credentials, endpoints, and operation records.
- SQLite 已将平台用户、外部身份、本地凭据、Session、实例、成员、凭据、端点和操作记录拆分建模。
- A user can own or access multiple instances. Runtime operations resolve an
  instance UUID on the server and use its `runtime_identifier`; clients cannot
  submit an arbitrary container name.
- 一个用户可以拥有或访问多个实例。运行时操作由服务端解析实例 UUID 并使用其
  `runtime_identifier`，客户端不能提交任意容器名。
- Local authentication, server-side sessions, CSRF protection, external
  OAuth2/OIDC configuration, and identity mapping are available.
- 本地认证、服务端 Session、CSRF 防护、外部 OAuth2/OIDC 配置和身份映射已经具备。
- `manager-user-web`, `manager-admin-web`, `manager-control`, and
  `manager-executor` are deployed as separate processes. The user portal no
  longer needs privileged host mounts.
- `manager-user-web`、`manager-admin-web`、`manager-control` 和
  `manager-executor` 已拆分部署，用户门户不再需要宿主机高权限挂载。
- The global `/admin/*` portal now runs on `manager-admin-web`, while
  per-instance `/admin/` redirects through `manager-user-web`. The legacy
  `manager-web` remains only as a temporary rollback target.
- 全局 `/admin/*` 管理门户现已由 `manager-admin-web` 提供，实例独立端口下的
  `/admin/` 则转入 `manager-user-web`。旧 `manager-web` 仅作为临时回滚目标保留。
- OpenClaw and EvoScientist adapters declare capabilities. Existing runtime
  lifecycle methods accept instance records and resolve targets from
  `runtime_identifier`; OpenClaw creation and legacy Nginx/file paths still use
  `legacy_user_id` compatibility data.
- OpenClaw 与 EvoScientist Adapter 已声明能力。现有运行时生命周期方法接收实例记录，
  并从 `runtime_identifier` 解析目标；OpenClaw 创建流程及旧 Nginx/文件路径仍使用
  `legacy_user_id` 兼容数据。
- Existing instances continue to use per-instance HTTPS ports and Basic Auth
  while unified ingress and instance access authorization remain future work.
- 在统一入口和实例访问授权完成前，现有实例继续使用独立 HTTPS 端口和 Basic Auth。

## Completed Foundations | 已完成基础工作

1. User, identity, instance, endpoint, credential, and audit data models.
2. Historical OpenClaw metadata migration with compatibility reads.
3. Local authentication, platform sessions, and configurable external
   OAuth2/OIDC authentication.
4. Multi-instance user portal and instance UUID authorization.
5. Initial Adapter objectification and product capability declarations.
6. Control, executor, user Web, and admin Web process split with a legacy admin
   compatibility path.
7. Product capability enforcement in Control and Executor.
8. Structured Executor actions for instance creation, lifecycle, retention,
   version, Basic Auth, Skill, device, and model-provider operations.
9. Admin feature migration and production `/admin/*` routing to
   `manager-admin-web`.

1. 用户、身份、实例、端点、凭据和审计数据模型。
2. 历史 OpenClaw 元数据迁移与兼容读取。
3. 本地认证、平台 Session 和可配置的外部 OAuth2/OIDC 认证。
4. 多实例用户门户与实例 UUID 权限校验。
5. Adapter 初步实例对象化与产品能力声明。
6. control、executor、用户 Web 和管理员 Web 进程拆分，以及旧管理员兼容入口。
7. Control 与 Executor 的产品能力校验。
8. 实例创建、生命周期、保留策略、版本、Basic Auth、Skill、设备和模型供应商的
   结构化 Executor 动作。
9. 管理员功能迁移以及生产 `/admin/*` 切换到 `manager-admin-web`。

## Current Transition: Retire Legacy manager-web | 当前过渡：退出旧 manager-web

- Verify that existing as well as newly generated instance `/admin/` routes use
  `manager-user-web`, not the compatibility container.
- 验证历史及新生成的实例 `/admin/` 路由均使用 `manager-user-web`，不再依赖兼容容器。
- Keep the legacy container during the production rollback window, then remove
  the service and its Docker Socket, Nginx, repository, and writable runtime
  mounts.
- 在生产回滚观察期内保留旧容器，随后删除该服务及其 Docker Socket、Nginx、仓库和
  可写运行时挂载。
- Continue using `legacy_user_id` only for existing OpenClaw filesystem and
  Nginx compatibility until those paths are replaced.
- 在对应路径替换前，`legacy_user_id` 仅用于既有 OpenClaw 文件系统与 Nginx 兼容。

## Next: Hermes MVP | 下一步：Hermes MVP

- Support Hermes registration and single-container creation plus start, stop,
  restart, status, logs, access, recoverable deletion, restore, and version
  updates using the shared instance and capability model.
- 基于统一实例和能力模型支持 Hermes 实例登记、单容器创建、启停、重启、状态、日志、
  访问、可恢复删除、恢复和版本升级。
- 已登记 Hermes 的 Dashboard 通过独立外部端口经 `openclaw-nginx` 转发到单容器
  `9119`；Nginx 负责 TLS 和路由，Dashboard 登录继续使用 Hermes 自身认证。
- Hermes 创建固定使用官方 `v2026.7.20` 单容器镜像；旧版双容器部署不在支持范围内。
- Require owner/member authorization and operation audit for every action.
- 每个动作必须校验所有者或成员权限并记录审计日志。
- Do not require OpenClaw-only file, device, or Skill features.
- 不要求实现 OpenClaw 专属的文件、设备或 Skill 功能。

## Later: Runtime and Ingress Separation | 后续：运行时与入口解耦

- Separate runtime lifecycle behavior from endpoint publication after at least
  two products expose concrete variation.
- 至少两个产品体现明确差异后，再拆分运行时生命周期与入口发布逻辑。
- Keep `LegacyPortIngress` for existing instances, then add unified HTTPS with
  instance subdomains and short-lived access authorization.
- 现有实例保留 `LegacyPortIngress`，随后增加实例子域名、统一 HTTPS 和短时访问授权。
- Serve Manager and instance subdomains through fixed ports `80/443`; instance
  creation and deletion should update routing with a graceful Nginx reload,
  without changing Docker published ports or recreating the shared gateway.
- Manager 与实例子域名统一使用固定 `80/443`；实例创建和删除仅更新路由并无损 reload
  Nginx，不再修改 Docker published ports 或重建共享网关。
- Plan wildcard DNS/TLS, access authorization, migration, and rollback before
  moving existing `LegacyPortIngress` instances to subdomains.
- 迁移既有 `LegacyPortIngress` 实例前，先完成通配符 DNS/TLS、访问授权、迁移与回滚方案。
- Remove per-instance Basic Auth only after gateway authorization is deployed
  and verified.
- 仅在网关授权上线并验证后，逐步取消实例 Basic Auth。

## Ongoing Rules | 持续规则

- Any lifecycle behavior change must update consistency and security checks.
- 任何生命周期行为变化都必须同步更新一致性与安全检查。
- Runtime targets and data paths are resolved from server-side instance data,
  never accepted directly from a browser request.
- 运行目标和数据路径必须从服务端实例数据解析，不得直接接受浏览器传值。
- Production changes use check-first, reversible deployment steps.
- 生产变更采用先检查、可回滚的部署步骤。
- Runtime secrets stay outside the repository.
- 运行时密钥不得进入仓库。
- Kubernetes remains deferred until the Docker deployment and multi-product
  Adapter path are stable.
- Kubernetes 继续延后，直到 Docker 部署和多产品 Adapter 路径稳定。
