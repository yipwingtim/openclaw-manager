# Hermes Activity 数据字典

## 版本基线

| 项目 | 值 |
| --- | --- |
| 文档版本 | 1.0.0 |
| 盘点日期 | 2026-08-05 |
| 产品镜像 | `nousresearch/hermes-agent:v2026.7.30` |
| 镜像 ID | `sha256:b869e64d6496d4763d5e4fb675b5f504cb23b0e35ec9b790481a56118602b10f` |
| `state.db` Schema | `schema_version=23` |
| 其他数据库 Schema | 无显式版本，使用白名单表字段作为指纹 |

本字典只描述活动统计所需字段，不是 Hermes 完整数据库字典。

## 数据源

| 数据源 | 容器内路径 | 用途 |
| --- | --- | --- |
| 状态数据库 | `/opt/data/state.db` | 会话、消息、工具和模型调用统计 |
| Kanban 数据库 | `/opt/data/kanban.db` | 任务和任务运行统计 |
| Cron 数据库 | `/opt/data/cron/executions.db` | 定时任务统计 |

文本日志仅用于人工排障，不作为第一版 Activity Adapter 数据源。

## `state.db` 字段白名单

### `sessions`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `id` | TEXT | 仅用于 `COUNT(*)` 或去重，不导出原值 |
| `started_at` | REAL | 会话开始时间，Unix 秒 |
| `ended_at` | REAL | 会话结束时间，Unix 秒 |
| `end_reason` | TEXT | 按稳定结束原因统计，可为空 |
| `message_count` | INTEGER | 消息累计数 |
| `tool_call_count` | INTEGER | 工具调用累计数 |
| `api_call_count` | INTEGER | 模型 API 调用累计数 |
| `input_tokens` | INTEGER | 聚合输入 Token，可选 |
| `output_tokens` | INTEGER | 聚合输出 Token，可选 |

禁止读取 `system_prompt`、标题、目录、Git 信息、用户/聊天/线程标识和任何模型配置 JSON。

### `session_model_usage`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `api_call_count` | INTEGER | 模型调用累计数 |
| `input_tokens` | INTEGER | 聚合输入 Token |
| `output_tokens` | INTEGER | 聚合输出 Token |
| `cache_read_tokens` | INTEGER | 聚合缓存读取 Token |
| `cache_write_tokens` | INTEGER | 聚合缓存写入 Token |
| `reasoning_tokens` | INTEGER | 聚合推理 Token |
| `first_seen` | REAL | 首次调用时间，Unix 秒 |
| `last_seen` | REAL | 最后调用时间，Unix 秒 |

禁止读取 Session ID、模型名、Provider、Base URL、任务名和费用明细。

不得读取 `messages`、`messages_fts*`、`delivery_obligations` 或任何包含正文、推理、任务和投递载荷的表。

## `kanban.db` 字段白名单

### `tasks`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `id` | TEXT | 仅用于计数或去重，不导出原值 |
| `status` | TEXT | 按状态统计任务数 |
| `created_at` | INTEGER | 创建时间；实现前需确认单位 |
| `started_at` | INTEGER | 开始时间；实现前需确认单位 |
| `completed_at` | INTEGER | 完成时间；实现前需确认单位 |

禁止读取标题、正文、结果、路径、分支、Skill、模型配置和错误字段。

### `task_runs`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `id` | INTEGER | 仅用于计数或去重 |
| `status` | TEXT | 按状态统计运行次数 |
| `started_at` | INTEGER | 开始时间；实现前需确认单位 |
| `ended_at` | INTEGER | 结束时间；实现前需确认单位 |

禁止读取 `outcome`、`summary`、`metadata` 和 `error`。

## `cron/executions.db` 字段白名单

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `id` | TEXT | 仅用于计数或去重，不导出原值 |
| `status` | TEXT | 按状态统计执行次数 |
| `started_at` | TEXT | 开始时间；实现时需解析并验证格式 |
| `finished_at` | TEXT | 结束时间；实现时需解析并验证格式 |

禁止读取 `job_id`、`source`、进程信息和 `error`。

## 观测样本

测试实例在盘点时包含 1 个会话、4 条消息、1 次工具调用和 2 次模型 API 调用。
Kanban 任务、任务运行和 Cron 执行表当时为空。样本计数仅用于验证数据源，不作为产品默认值。

## 重新盘点条件

- Hermes 镜像 tag 或 digest 变化；
- `state.db` Schema 不再为 23；
- 任一白名单表或字段缺失；
- `kanban.db` 或 Cron 时间字段出现真实数据后，首次实现前必须确认时间格式与单位；
- 数据目录或数据库路径发生变化。

