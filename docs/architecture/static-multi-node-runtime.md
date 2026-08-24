# Static Multi-Node Runtime Design

## 1. 目标与范围

本文档定义 OpenClaw Manager 从单机部署演进到“三台固定 Runtime Node + 一个
Control Plane”的首期设计。三台新服务器用于新的生产环境，当前生产服务器暂时保留为
测试环境，不自动迁移现有实例。

首期目标是让一个 Control Plane 管理固定归属在 A、B、C 节点上的 OpenClaw、Hermes 和
EvoScientist 实例，同时保持现有 Docker、产品 Adapter 和独立 HTTPS 端口入口。

首期不实现自动跨节点迁移、自动故障转移、共享实例存储、Control Plane 高可用或统一
443 入口。

## 2. 部署拓扑

```text
测试服务器
└── 测试环境：Control Plane + 测试 Runtime

生产节点 A
├── Control Plane
│   ├── manager-control
│   ├── manager-admin-web
│   ├── manager-user-web
│   └── PostgreSQL
└── Runtime Node A
    └── manager-executor + Docker + Nginx

生产节点 B
└── Runtime Node B
    └── node-executor + Docker + Nginx

生产节点 C
└── Runtime Node C
    └── node-executor + Docker + Nginx
```

A 同时承担 Control Plane 和 Runtime Node A。A 故障时，A 上实例和集中管理能力同时
不可用；B、C 上已经运行的实例不因 Control Plane 短暂不可用而被自动停止。

## 3. 模块职责与接口

### 3.1 Control Plane

Control Plane 是唯一的管理入口，负责用户认证、权限、实例元数据、节点选择、审计和
任务路由。浏览器只能提交服务端解析的 `instance_id` 和结构化动作，不能提交容器名、
宿主机路径或 Shell 命令。

Runtime Node 对 Control Plane 使用最小的结构化接口：

```text
POST /internal/v1/node/register
POST /internal/v1/node/heartbeat
GET  /internal/v1/node/tasks
POST /internal/v1/node/tasks/{task_id}/result
```

动作请求至少包含：

```json
{
  "request_id": "uuid",
  "instance_id": "uuid",
  "action": "start"
}
```

Executor 主动轮询待领取任务，领取后根据经过签名或双向 TLS 认证的响应，从服务端
解析的实例记录获取 `runtime_identifier`，再调用固定的 Adapter/Executor 动作。节点
接口拒绝任意命令和任意运行时目标；任务使用租约和 `request_id` 保证幂等。

### 3.2 Runtime Node

Runtime Node 只负责本机资源：Docker 容器、实例数据目录、产品配置和 Nginx 入口。它
不负责平台用户登录、跨节点查询或修改其他节点的数据。

每个节点运行一个受限的 node-executor。首期可以复用现有 Executor 的白名单动作，先
通过节点标识选择本地执行上下文，再逐步拆出节点侧进程。

## 4. PostgreSQL 与数据边界

生产 Control Plane 使用 PostgreSQL，部署在 A 的独立持久化卷中。数据库只保存平台
控制面元数据：

- 用户、外部身份、本地凭据和 Session；
- 实例、成员、产品、`node_id`、端点和凭据引用；
- 节点注册、心跳、容量/GPU 标签和任务状态；
- 操作审计和资源快照。

实例元数据、Activity/操作记录、审计记录和历史执行结果属于控制面数据，均迁移到
PostgreSQL。实例容器和运行数据仍留在节点：只有少于 10 个仍需使用的实例迁移完整
数据目录与产品配置；历史实例只迁移元数据，标记为 `archived`/`historical`，
`node_id` 置空且不可被生产调度。

实例工作区、会话文件、uploads、skills、extensions、产品配置、Docker 卷和日志仍保留
在实例所属节点，不写入 PostgreSQL。节点数据目录不通过共享 SQLite 文件访问。

PostgreSQL 首期接受 A 单点故障，但必须配置定时备份，并将备份复制到 B、C 或独立存储。
后续可在不改变 Control Plane 数据接口的前提下增加 PostgreSQL 高可用。

当前单机环境仍可继续使用 SQLite 作为测试数据库；迁移到 PostgreSQL 是新生产拓扑的
部署阶段，不与节点模型代码改造混在同一个变更中。

## 5. 节点与实例模型

新增 `runtime_nodes` 实体：

```text
runtime_nodes
- id
- public_id
- name
- endpoint
- status
- labels_json
- capacity_json
- last_heartbeat_at
- created_at
- updated_at
```

`instances` 增加 `node_id`，并要求每个非删除实例有一个固定节点归属。节点选择规则：

1. 首期创建实例时管理员必须明确指定节点；服务端校验节点在线且产品能力/标签满足要求；
2. 未指定节点或节点不满足条件时拒绝创建，不做自动放置、容量扣减或负载调度；
3. 已创建实例的生命周期操作始终路由到其 `node_id`，不会因为节点负载变化而隐式迁移。

端点与端口规则：

- 运行时端口按 `(node_id, port)` 唯一；不同节点可以复用同一个端口；
- 对外端点独立建模，路径入口按 `(ingress_id, host, normalized_path)` 唯一；
- OpenClaw 首期可使用共享域名和 `/instances/{instance_public_id}` 路径，但必须处理尾
  斜杠、路径前缀、WebSocket、重定向、Cookie/base path、冲突和节点离线错误；
- 同一实例可以同时保留旧 IP+端口端点和新域名+路径端点，验证新端点后再停用旧端点；
- Hermes 和 EvoScientist 首期继续使用所属节点的 IP+端口。

## 6. 心跳、容量与故障边界

节点启动后注册自身版本、产品能力、GPU 标签和容量信息，并定期发送心跳。Control
Plane 将节点标记为：

```text
provisioning
online
degraded
offline
draining
disabled
```

心跳超时只阻止新的创建和运行时操作，不删除实例、不修改节点数据，也不自动把实例
迁移到其他节点。管理员可以将节点置为 `draining`，停止向其分配新实例，但现有实例
仍由该节点负责。

节点离线时：

- B/C 上已有容器可继续按其本地策略运行；
- Control Plane 返回明确的节点不可用错误；
- 尚未被节点领取的任务可以自动等待或重新排队，并记录审计；
- 已开始但结果未知的破坏性任务不得自动重跑；查询节点/实例状态后由管理员确认是否重试；
- A 故障期间，B/C 上已有容器不自动停止，但新的登录、实例授权、Session bridge
  和管理访问不保证可用。

## 7. 网络与安全

- Control Plane 到节点只开放内部管理网络端口；不把 Docker Socket 暴露给公网。
- 节点之间不共享 Docker Socket、实例目录或 Nginx 配置目录。
- 节点身份使用独立凭据，支持短期 token 或双向 TLS；凭据只保存在服务器密钥目录。
- Runtime Node 只接受白名单动作，并以服务端解析的实例记录为唯一目标来源。
- 实例继续使用当前独立 HTTPS 端口；入口地址和节点信息记录在 `instance_endpoints`。
- 统一 443、子域名/路径和跨节点访问授权继续作为后续阶段，不能假设 Hermes 或
  EvoScientist 已支持路径前缀。

## 8. 实施阶段

### 阶段一：测试环境数据库适配

- 为 Control Plane 引入 PostgreSQL 连接配置和健康检查；
- 将现有 SQLite 元数据迁移工具扩展为可验证的 PostgreSQL 导入；
- 保留 SQLite 测试路径，确保本地开发无需 PostgreSQL。

### 阶段二：节点注册与只读状态

- 创建 `runtime_nodes` 和 `instances.node_id` migration；
- 实现注册、心跳、节点列表、容量标签和离线判定；
- 不改变实例创建和生命周期执行路径。

### 阶段三：固定节点执行

- 将结构化 Executor 请求写入任务队列，由节点按租约主动拉取并按 `node_id` 执行；
- 在 A、B、C 创建测试实例并验证三种产品；
- 验证节点离线、恢复、任务重试和审计记录。

### 阶段四：新生产环境切换

- 先备份 PostgreSQL 和各节点实例目录；
- 在 A、B、C 部署稳定版本；
- 只创建新实例进行灰度验证；
- 旧服务器继续作为测试环境，确认回滚后再迁移选定实例。

## 9. 验收标准

1. 三个节点可以独立注册并持续报告心跳；
2. 管理员可查看节点状态、标签和容量；
3. 新实例明确写入 `node_id`，且生命周期操作只在所属节点执行；
4. 节点接口拒绝任意容器名、路径和 Shell；
5. 一个节点离线不会导致其他节点实例被删除或迁移；
6. PostgreSQL 重启后元数据和任务状态可恢复；
7. 节点本地实例数据不进入 PostgreSQL；
8. OpenClaw、Hermes、EvoScientist 的现有独立端口入口仍可访问；
9. 测试服务器与新生产节点之间没有隐式共享数据库或实例目录。

## 10. 明确推迟的事项

- 自动跨节点迁移和调度平衡；
- PostgreSQL 自动故障切换；
- 多 Control Plane 高可用；
- 共享存储和跨节点实时会话；
- Kubernetes Runtime Adapter；
- 所有产品统一 443 + 子域名/路径。
