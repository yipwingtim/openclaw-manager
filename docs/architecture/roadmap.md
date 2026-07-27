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
- The legacy `manager-web` remains as an admin compatibility service because
  batch creation, version management, Basic Auth, Skill, and batch-device
  operations have not all moved to structured control and executor actions.
- 旧 `manager-web` 仍作为管理员兼容服务，因为批量创建、版本管理、Basic Auth、
  Skill 和批量设备操作尚未全部迁入结构化 control/executor 动作。
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

1. 用户、身份、实例、端点、凭据和审计数据模型。
2. 历史 OpenClaw 元数据迁移与兼容读取。
3. 本地认证、平台 Session 和可配置的外部 OAuth2/OIDC 认证。
4. 多实例用户门户与实例 UUID 权限校验。
5. Adapter 初步实例对象化与产品能力声明。
6. control、executor、用户 Web 和管理员 Web 进程拆分，以及旧管理员兼容入口。

## Next 1: Adapter Contract and Capability Enforcement | 下一步 1：收口 Adapter 契约与能力校验

- Use instance records for all existing runtime lifecycle operations.
- 现有运行时生命周期操作统一使用实例记录。
- Enforce product capabilities in the backend and executor before dispatch;
  frontend visibility remains presentation only.
- 后端与执行器在分发前统一校验产品能力，前端隐藏仅用于展示。
- Keep `legacy_user_id` for current OpenClaw filesystem and Nginx compatibility.
- 当前 OpenClaw 文件系统和 Nginx 路径继续保留 `legacy_user_id` 兼容。
- Do not change authentication, Web routes, instance creation, or ingress in
  this step.
- 本阶段不修改认证、Web 路由、实例创建或入口发布方式。

## Next 2: Complete Admin Feature Migration | 下一步 2：完成管理员功能迁移

- Move batch creation, version management, Basic Auth, Skill management,
  device operations, metadata views, and operation history to
  `manager-admin-web` without reducing existing functionality.
- 将批量创建、版本管理、Basic Auth、Skill 管理、设备操作、元数据查看和操作记录
  迁入 `manager-admin-web`，不得降低旧页面现有功能。
- Address instance creation separately because it must coordinate platform
  records, runtime provisioning, endpoint publication, rollback, and audit.
- 单独处理实例创建，因为它需要协调平台记录、运行时部署、入口发布、回滚和审计。
- Keep production `/admin/*` on the compatibility service until each migrated
  operation passes parity and rollback verification.
- 每项迁移功能通过等价性与回滚验证前，生产 `/admin/*` 继续使用兼容服务。

## Next 3: Move Privileged Actions Behind Executor | 下一步 3：高权限操作收敛到 Executor

- Define structured, allowlisted actions identified by instance UUID.
- 定义以实例 UUID 为目标的结构化白名单动作。
- Move Docker, Nginx, host filesystem, create, delete, restore, version, Skill,
  and device operations out of Web processes.
- 将 Docker、Nginx、宿主机文件、创建、删除、恢复、版本、Skill 和设备操作移出 Web 进程。
- Preserve authorization, idempotency, audit records, bounded output, and safe
  rollback for every action.
- 每个动作均保留权限校验、幂等性、审计记录、输出限制和安全回滚。

## Next 4: Retire Legacy manager-web | 下一步 4：退出旧 manager-web

- Route `/admin/*` to `manager-admin-web` only after full admin parity.
- 仅在管理员功能完整对齐后，将 `/admin/*` 切换到 `manager-admin-web`。
- Remove Docker Socket, Nginx, repository, and writable runtime mounts from all
  Web services.
- 从全部 Web 服务移除 Docker Socket、Nginx、仓库和可写运行时挂载。
- Remove the compatibility container only after production acceptance and a
  tested rollback window.
- 生产验收和回滚观察期通过后，再移除兼容容器。

## Next 5: Hermes MVP | 下一步 5：Hermes MVP

- Add Hermes registration or creation, start, stop, restart, status, logs, and
  access using the shared instance and capability model.
- 基于统一实例和能力模型增加 Hermes 注册或创建、启停、重启、状态、日志和访问。
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
