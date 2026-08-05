# EvoScientist Activity 数据字典

## 版本基线

| 项目 | 值 |
| --- | --- |
| 文档版本 | 1.0.0 |
| 盘点日期 | 2026-08-05 |
| 产品镜像 | `ghcr.io/evoscientist/evoscientist@sha256:ca1fd303d7ca2d1bfad97d9872b4ee910eea67c46047be1bf59463941fff3c47` |
| 镜像 ID | `sha256:ca1fd303d7ca2d1bfad97d9872b4ee910eea67c46047be1bf59463941fff3c47` |
| 数据库 Schema | 无显式版本 |
| Schema 指纹 | `checkpoints` 与 `writes` 白名单字段 |

本字典只描述活动统计所需字段，不尝试解析 LangGraph checkpoint 内容。

## 数据源

| 数据源 | 容器内路径 | 用途 |
| --- | --- | --- |
| Session 数据库 | `/home/evosci/.evoscientist/sessions.db` | 研究线程、checkpoint 和任务写入统计 |
| 数据库文件 mtime | 同上 | 缺少结构化时间字段时的最后活动近似值 |

## `checkpoints`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `thread_id` | TEXT | 仅用于 `COUNT(DISTINCT ...)`，不导出原值 |
| `checkpoint_ns` | TEXT | 仅用于命名空间计数，不导出原值 |
| `checkpoint_id` | TEXT | 仅用于 checkpoint 计数或去重，不导出原值 |
| `parent_checkpoint_id` | TEXT | 不采集；仅列入 Schema 指纹 |
| `type` | TEXT | 当前不作为指标；语义确认后方可加入白名单 |

严禁读取或解析 `checkpoint` 和 `metadata` BLOB。

## `writes`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `thread_id` | TEXT | 仅用于 `COUNT(DISTINCT ...)`，不导出原值 |
| `checkpoint_ns` | TEXT | 不采集；仅列入 Schema 指纹 |
| `checkpoint_id` | TEXT | 仅用于关联计数，不导出原值 |
| `task_id` | TEXT | 仅用于 `COUNT(DISTINCT ...)`，不导出原值 |
| `idx` | INTEGER | 不采集；仅列入 Schema 指纹 |
| `channel` | TEXT | 当前不采集，避免暴露内部工作流语义 |
| `type` | TEXT | 当前不采集，语义确认后方可加入白名单 |

严禁读取或解析 `value` BLOB。

## 指标口径

| 指标 | 计算方式 | 限制 |
| --- | --- | --- |
| `sessions` | `COUNT(DISTINCT checkpoints.thread_id)` | 表示研究线程，不是用户数 |
| `checkpoints` | `COUNT(*) FROM checkpoints` | 仅表示工作流持久化活跃度 |
| `write_tasks` | `COUNT(DISTINCT writes.task_id)` | 不等同于用户发起任务数 |
| `writes` | `COUNT(*) FROM writes` | 可用于技术活跃度，不建议作为核心成效指标 |
| `last_activity_at` | `sessions.db` 文件 mtime | 是实例级近似值，不是单条事件时间 |

checkpoint 数量可能被单个长线程显著放大，不能直接解释为对话次数或用户操作次数。

## 观测样本

测试实例在盘点时包含 4 个 thread、1101 个 checkpoint、1948 条 write 和
1118 个不同 task ID。每线程 checkpoint 数量范围为 10 至 1031，说明原始 checkpoint
数不适合直接作为用户使用次数。样本计数仅用于验证数据源，不作为产品默认值。

## 重新盘点条件

- EvoScientist 镜像 digest 变化；
- `sessions.db` 路径变化；
- `checkpoints` 或 `writes` 表及字段指纹变化；
- 产品新增可用的结构化时间、运行状态或低敏统计 API；
- 需要解析 BLOB 才能获得指标时，必须另行进行隐私和兼容性评审。

