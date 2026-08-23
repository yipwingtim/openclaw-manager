# Agent Hosting Platform

## 1. 文档目标

本文档用于定义一个面向多租户、多产品的 Agent Hosting Platform / Agent Runtime Control Plane 的最小架构模型，并作为当前 `OpenClaw Manager` 向通用平台演进的设计基础。

本文档关注以下目标：

- 明确平台的长期定位，不再局限于 `OpenClaw Manager`
- 为后续支持 `OpenClaw`、`Hermes` 等多种智能体产品提供统一抽象
- 定义平台层、产品适配层、运行时层的职责边界
- 指导后续 Web 控制台、API、脚本重构和目录演进

当前用户、身份、实例、产品能力以及 Control/Executor 执行边界已经落地；全局用户与
管理员入口也已拆分；实例 `/admin/` 已转入 `manager-user-web`。旧 `manager-web`
已从生产 Compose 和活动入口退役。统一实例入口仍属于后续演进内容。本文档用于统一
长期方向和术语，不代表所有模块均已实现。

## 2. 历史背景与当前剩余问题

项目最初的目标是在一台服务器上为多个用户创建并管理独立的 OpenClaw 实例；当前已经
纳管 OpenClaw、Hermes 和 EvoScientist，并计划从单机运行演进到单 Control Plane 加多个
固定 Runtime Node。现有方案已具备实例隔离、端口分配、Nginx 反向代理、删除与恢复等能力。

但是，随着实例数量增加，当前设计开始暴露明显的扩展问题。

### 2.1 历史问题：管理动作集中在管理员

早期多个高频动作依赖管理员进入宿主机或容器执行，包括但不限于：

- `device approval`
- `skill update`
- 手工协助用户准备容器可访问的文件

当前这些动作已逐步收口为结构化 Control/Executor 操作；后续重点是资源监控、
Provider 管理和多节点执行，而不是向用户开放宿主机或容器 Shell。

### 2.2 已解决：安全的用户自助入口

当前 `manager-user-web` 已提供受控用户入口，并通过 Control/Executor、实例 UUID、
成员权限和产品能力执行允许的动作。

平台仍不得直接开放 SSH、容器 Shell 或共享宿主机目录；新增自助能力必须继续遵循
结构化动作、服务端目标解析和审计原则。

### 2.3 迁移中：OpenClaw 兼容路径

核心数据模型已经支持 OpenClaw、Hermes 和 EvoScientist，但部分历史脚本和路径仍绑定
OpenClaw，例如：

- 用户目录直接对应 `OpenClaw` 实例目录
- 运行时路径命名具有明显的 `openclaw` 专属性
- 维护动作与 `OpenClaw` 的具体命令耦合

这些路径通过 `legacy_user_id` 兼容保留；新功能应使用实例 UUID、`runtime_identifier`
和服务端解析的 `data_path`，不再扩大旧耦合。

### 2.4 已建立：平台能力抽象

当前项目已经从“单产品脚本集合”演进为多租户实例托管平台，并已落地：

- 平台用户
- 产品类型
- 实例
- 产品能力
- 统一动作入口
- 审计和权限控制

后续演进应继续复用这些抽象，避免重新在产品分支中累积平台级特例。

## 3. 平台定位

本平台的目标不是替代各个 agent 产品自身的 Web UI，也不是做一个通用聊天前端。它更适合被定义为一个多租户的 Agent Hosting Platform，即一个面向智能体实例托管和运行时控制的管理平面。

平台负责以下职责：

- 管理平台用户和实例归属关系
- 创建、删除、恢复、启动、停止实例
- 维护访问入口、运行资源和配套配置
- 将高频管理动作封装为受控能力
- 提供统一审计、权限校验和运维控制

平台不应承担以下职责：

- 替代各产品自身的业务 UI
- 暴露原始宿主机控制权或容器 shell 给用户
- 强行统一所有产品的内部鉴权模型

因此，这个平台更像是“用户和底层运行环境之间的一层受控控制面”，而不是“产品内部功能的重新实现层”。

## 4. 核心设计目标

后续平台演进应满足以下目标：

- 支持多产品实例托管，而非仅支持 `OpenClaw`
- 支持多租户隔离和最小权限原则
- 支持将高频维护动作封装为受控 API / Web 操作
- 支持统一审计与操作记录
- 支持逐步演进，并兼容现有脚本资产
- 支持未来扩展不同运行时实现，而不是把全部逻辑写死在单一脚本中

这些目标共同指向一个原则：平台核心要尽量稳定，产品差异应尽量被限制在适配层内。

## 5. 非目标

为了控制范围，当前阶段不以以下目标为优先：

- 不追求一开始就构建完整插件市场
- 不追求 Kubernetes 优先的复杂编排设计
- 不要求统一所有产品的内部账号体系
- 不要求替代产品自身的全部 UI 能力
- 不要求所有产品共享完全相同的生命周期实现

当前更重要的是先建立统一抽象，保证平台以后可以扩展，而不是一开始就覆盖所有可能场景。

## 6. 核心抽象模型

### 6.1 User

`User` 表示登录平台的人，是平台层的主体，而不是某个产品内部的账号对象。

建议最小字段包括：

- `id`
- `public_id`
- `username`
- `normalized_username`
- `display_name`
- `email`
- `role`
- `status`
- `created_at`

当前实现通过 `user_identities(provider, subject)` 将 `nginx-basic`、`local` 和已配置的
OAuth2/OIDC（包括可选 UIS）身份映射到同一个 `users.id`。外部认证首次登录不自动创建
平台用户。

平台角色至少区分为：

- `admin`
- `user`

后续如果需要，可以在此基础上继续扩展更细的权限模型。

### 6.2 Product

`Product` 表示可托管的一类 agent 产品，例如：

- `openclaw`
- `hermes`

它描述的是“某一类产品”，而不是“某个具体实例”。

建议最小字段包括：

- `id`
- `code`
- `name`
- `adapter_name`
- `status`
- `version_policy`

其中 `adapter_name` 用于将产品与具体的适配器实现绑定。

### 6.3 Product Capability

`ProductCapability` 用于描述某个产品在平台层支持哪些能力。平台前端和平台 API 不应假设所有产品都有相同按钮或相同行为，而应根据能力声明进行展示和分发。

能力示例包括：

- `device_approval`
- `skill_update`
- `file_upload`
- `web_access`
- `token_login`
- `snapshot_restore`

这个抽象的目的，是将“产品是否支持某能力”变成显式模型，而不是硬编码在前端页面或脚本分支里。

### 6.4 Instance

`Instance` 表示某个用户创建的某个产品实例。

建议最小字段包括：

- `id`
- `public_id`
- `owner_user_id`
- `product`
- `instance_name`
- `runtime_identifier`
- `status`
- `restore_state`
- `data_path`
- `metadata_json`
- `created_at`
- `deleted_at`

这里有两个重要原则：

- 一个用户可以拥有多个实例
- 一个实例只属于一个产品类型

后续平台不应再默认“一个用户只对应一个 OpenClaw 容器”。

当前 metadata schema 已允许一个用户拥有多个实例，并通过
`UNIQUE(owner_user_id, product, instance_name)`、全局唯一
`runtime_identifier` 和唯一非空 `data_path` 防止资源冲突。旧生命周期脚本仍通过
`legacy_user_id` 兼容历史的一用户一 OpenClaw 实例流程。

### 6.5 Instance Resource

`InstanceResource` 用于保存实例对应的基础设施资源信息，将平台元数据和底层运行细节分开。

建议最小字段包括：

- `instance_id`
- `container_name`
- `compose_project`
- `network_name`
- `host_port`
- `data_path`
- `upload_path`
- `log_path`

这样做的好处是，平台层可以围绕“实例”建模，而不必在每处逻辑里直接拼接容器名、宿主机路径或端口。

### 6.6 Instance Action Log

`InstanceActionLog` 用于记录实例相关操作，既承担审计作用，也可作为任务记录基础。

建议最小字段包括：

- `id`
- `instance_id`
- `operator_user_id`
- `action_type`
- `action_params`
- `status`
- `result_summary`
- `created_at`
- `finished_at`

典型动作包括：

- 创建实例
- 删除实例
- 恢复实例
- 批准设备
- 更新 skill
- 上传文件
- 启动或停止实例

## 7. 模块边界

### 7.1 Portal Layer

`Portal Layer` 是平台的 Web 控制台层，面向管理员和普通用户提供不同视角的入口。

管理员侧典型能力包括：

- 创建实例
- 删除或恢复实例
- 查看状态和日志
- 执行受控维护动作

用户侧典型能力包括：

- 查看自己的实例
- 查看访问入口
- 执行产品允许的自助动作
- 上传文件
- 查看操作结果

这一层负责用户交互，但不应直接承担产品实现细节。

### 7.2 Platform API Layer

`Platform API Layer` 是统一的平台接口层，负责：

- 登录与认证
- 权限校验
- 实例查询
- 动作入口管理
- 审计记录

这一层不应直接写大量 `OpenClaw` 或 `Hermes` 专属逻辑。它的主要职责，是对外提供稳定接口，并把具体实现交给后面的动作分发和适配层。

### 7.3 Action Dispatcher

`Action Dispatcher` 是平台动作分发中心。

它负责：

- 接收统一动作请求
- 识别目标实例所属产品
- 根据产品选择对应 adapter
- 统一处理输入校验、权限、状态记录和结果返回

所有高权限动作都应尽量收敛到这一层，而不是由前端或宿主机脚本被直接调用。

### 7.4 Product Adapter Layer

`Product Adapter Layer` 是产品适配层，用于屏蔽不同产品之间的创建方式、维护动作和访问模型差异。

典型实现示例：

- `OpenClawAdapter`
- `HermesAdapter`

每个 adapter 至少应负责：

- 创建实例
- 删除实例
- 查询实例状态
- 返回访问入口信息
- 执行产品专属动作
- 定义文件上传或初始化处理逻辑

平台核心通过统一接口调用 adapter，而不直接依赖产品命令细节。

### 7.5 Runtime Layer

`Runtime Layer` 负责与底层基础设施打交道，例如：

- Docker / Compose
- Nginx
- 宿主机文件系统
- 网络、端口和挂载
- 健康检查和日志读取

建议保持一条清晰边界：

- `Adapter` 负责产品逻辑
- `Runtime Layer` 负责基础设施逻辑

这样可以避免每个产品 adapter 里都重复实现大量容器和文件系统细节。

## 8. 统一动作模型

平台需要定义一套统一动作入口，供前端、脚本封装层和后续 API 使用。

### 8.1 通用动作

建议优先统一以下动作：

- `create_instance`
- `delete_instance`
- `restore_instance`
- `start_instance`
- `stop_instance`
- `restart_instance`
- `get_status`
- `get_access_info`

这些动作不应绑定到某个具体产品，而应作为平台标准动作存在。

### 8.2 产品专属动作

产品特有动作通过统一入口传入，再由 adapter 解释和执行。

例如：

- `OpenClaw`
  - `approve_device`
  - `update_skill`
- `Hermes`
  - 后续根据实际运行模型补充

这样可以避免平台前端和 API 被某个产品的专属按钮直接绑死。

## 9. 权限模型

### 9.1 平台权限

平台权限描述的是“用户是否可以在平台上执行某类动作”，例如：

- 创建实例
- 删除实例
- 恢复实例
- 上传文件
- 执行维护动作
- 查看日志

这是平台控制面的授权问题。

### 9.2 产品内部权限

产品内部权限描述的是产品本身的认证或鉴权机制，例如：

- `OpenClaw` 的 token 登录
- `device approval`
- `Hermes` 自身的账号体系

平台不应强行统一这些机制，而应负责平台入口认证和平台动作授权。必要时，平台可以为用户提供受控跳转、代执行或状态展示，但不应简单把产品内部权限与平台权限混为一谈。

### 9.3 统一实例认证契约

所有产品适配器都通过同一个 `instance_auth_contract()` 接口声明实例入口认证，
但不把只读认证契约混入可执行的产品 capability，也不伪造产品内部认证能力。
契约分为两层：

- `edge_authorization`：平台入口是否要求 UIS 实例授权；当前三种产品均为 `uis`。
- `product_auth`：下游产品当前实际使用的认证方式。OpenClaw 旧实例为 `token`，
  Manager 新建实例为 `trusted_proxy`，
  Hermes 为官方 `session`，EvoScientist 为 `none`。

因此，适配器的生命周期动作和认证契约入口保持统一，产品差异只存在于契约值及其内部实现。
OpenClaw 在迁移期按实例配置区分 `token` 和 `trusted_proxy`；
Hermes 的官方 session 不能被 Nginx 的 Basic `Authorization` Header 直接替代，
后续若要免登录，必须接入 Hermes 支持的认证 provider 或 OIDC 流程。

## 10. 文件与数据路径设计

当前项目中已有的路径命名明显偏向 `OpenClaw` 专属结构。未来如果要支持多产品，不应继续扩大这种命名绑定。

新的平台设计应优先按 `instance_id` 组织资源，而不是按具体产品名字或用户名硬编码。

建议的统一抽象结构如下：

- `instances/<instance_id>/data`
- `instances/<instance_id>/uploads`
- `instances/<instance_id>/logs`
- `instances/<instance_id>/runtime`
- `instances/<instance_id>/meta`

当前阶段可以保留已有 `OpenClaw` 路径结构，以保障现有脚本可继续运行。但新增平台层设计不应继续强化产品专属路径概念。

## 11. 访问入口模型

当前 `OpenClaw` 的访问方式是 `Nginx + 独立 HTTPS 端口`。这在现有部署场景中是合理的，但不应被上升为平台唯一访问模型。

平台层应记录“实例的访问方式”和“实例的访问地址”，而不是假设所有实例都必须通过独立端口暴露。

访问模型未来可以包括：

- 独立端口
- 子域名
- 路径代理
- 内网地址
- API endpoint

因此，`OpenClaw` 当前的端口模型应视为某个产品的一种实现，而不是平台公共前提。

## 12. 审计与安全原则

平台后续要支持更多自助动作，就必须同步加强安全边界和审计能力。

建议遵循以下原则：

- 所有高权限动作必须记录审计日志
- 用户不得直接获得宿主机或容器 shell 权限
- 平台只暴露白名单动作
- 文件上传必须限制到实例所属目录
- 产品专属命令必须经过 adapter 封装
- 平台操作结果应可追踪、可排障

从平台角度看，安全设计重点不是“给不给权限”，而是“以什么粒度暴露什么动作，并且是否可审计”。

## 13. 与当前 OpenClaw Manager 的关系

当前仓库已纳管 OpenClaw、Hermes 和 EvoScientist；OpenClaw 仍保留最多历史脚本和兼容路径。

因此，当前阶段的演进原则应是：

- 不推翻现有脚本
- 不急于重写全部实现
- 先收敛架构边界
- 再逐步把脚本纳入统一动作体系

换句话说，现有脚本可以视为第一版 `OpenClawAdapter` 的事实基础，而不是未来平台设计的阻碍。

## 14. 演进路线

### Phase 1：用户、身份与实例基础模型（已完成）

已完成：

- `users`、`user_identities` 与独立 `instances`
- 用户和实例 UUID `public_id`
- 一个用户拥有多个实例的数据约束
- 凭据、端点、端口和操作记录关联 `instance_id`
- 历史数据迁移、已删除实例恢复状态和兼容字段

### Phase 2：认证框架基础（已完成）

已完成：

- `nginx-basic` 与 `local` Provider
- 可配置 OAuth2/OIDC Provider，以及 UIS 作为可选 OAuth2 配置的验证
- 一个用户绑定多个认证身份
- 同时只启用一种管理端认证方式
- Local 密码哈希、服务端 Session、CSRF 和失败锁定
- 外部认证用户必须预置、不在首次登录时自动创建的策略

### Phase 3：实例 Adapter 与用户门户基础（已完成）

已完成：

- OpenClaw 与 EvoScientist Adapter 的产品路由和能力声明
- 运行时生命周期操作接收实例记录，并从 `runtime_identifier` 解析目标
- 基于实例 UUID 的用户多实例门户和权限校验
- 结构化 control、executor 和操作记录基础
- 用户 Web 与管理员 Web 进程拆分
- 在后端与 executor 统一执行产品能力校验
- OpenClaw 实例创建使用明确的 provisioning 契约和 Executor 任务
- 在迁移期保留 `legacy_user_id`，仅用于既有 OpenClaw 文件和 Nginx 路径

### Phase 4：管理员功能与高权限边界收口（已完成）

`manager-admin-web` 已覆盖实例创建、批量创建、生命周期、保留策略、版本、
Basic Auth、Skill、设备管理、模型供应商和元数据页面。高权限写操作通过结构化
Control/Executor 任务执行，全局 `/admin/*` 已切换到 `manager-admin-web`。

生产切换和回滚观察期已经完成；活动 Nginx 配置与 Compose 均不再引用旧
`manager-web` 容器。

### Phase 5：Hermes MVP（已完成）

Hermes 已使用统一实例模型和执行器边界：

- 登记已有实例，并支持启动、停止、重启、状态、日志和访问入口
- 所有操作使用实例 UUID、成员权限和审计记录
- 按 Hermes 实际能力声明功能，不强制对齐 OpenClaw 的文件、设备或 Skill 功能
- Hermes 创建契约固定为官方 `v2026.7.20` 单容器 `gateway run` 部署
- 已登记 Hermes 使用 Nginx 独立端口转发到容器 Dashboard `9119`；Nginx 持久加入
  Hermes 的唯一租户网络。Dashboard 由 `campus-uis-bridge` 建立 Hermes Session，API
  `8642` 的 `API_SERVER_KEY` 不作为 Dashboard 登录凭据。
- Hermes 创建固定使用官方 `v2026.7.20` 单容器镜像、`gateway run` 和
  `HERMES_DASHBOARD=1`。每个实例的数据目录独立挂载到 `/opt/data`，不直接发布
  容器端口；旧版双容器拓扑不在支持范围内。

### Phase 6：后续平台演进

按以下顺序推进：

1. 增强可配置认证 Provider、身份绑定和 Provider 健康检查。
2. 增加实例磁盘大小、会话文件数量和阈值预警的只读资源监控。
3. 盘点并迁移 OpenClaw `token` 实例，逐步退出符合条件实例的 Basic Auth。
4. 建立单 Control Plane 加三个固定 Runtime Node 的静态节点模型。
5. 至少两个产品明确支持后，再拆分 Runtime 与 Ingress，并设计统一入口。

Hermes 和 EvoScientist 尚未证明统一路径/子域名兼容性；在此之前保留独立 HTTPS
端口，不强制所有产品统一到 `443 + 子域名/路径`。三节点首期不包含自动跨节点迁移
和高可用。

## 15. 待确认问题

当前仍有一些问题需要在后续设计和实现中逐步确认：

- 产品版本由平台统一控制，还是实例级可选
- 文件上传是走本地目录还是对象存储
- 是否要引入异步任务队列
- 审计日志先落文件、SQLite，还是数据库
- Hermes 的部署、健康检查和入口模型与 OpenClaw 的实际差异
- 实例创建时平台记录、运行环境和入口发布的提交及回滚顺序
- 统一入口应优先使用子域名还是仅为明确兼容的产品提供子路径
- 三节点部署中的 Control Plane 数据库应继续使用单机 SQLite，还是迁移到 PostgreSQL

## 16. 当前结论

基于现阶段讨论，可以先得出以下结论：

- 当前项目正在从 `OpenClaw` 专用管理脚本向通用 Agent Hosting Platform 演进
- 平台核心应优先抽象 `Product`、`Instance`、`Capability`、`Adapter` 和 `Action Dispatcher`
- 后续 Web 化应以“受控管理平面”为目标，而不是简单替代产品现有 UI
- 现有 `OpenClaw` 脚本应被保留，并逐步纳入新的平台边界

本文档作为第一版架构初稿，后续应随着 API、目录结构、适配器实现和 Web 控制台演进而持续更新。
