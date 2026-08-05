# OpenClaw Activity 数据字典

## 版本基线

| 项目 | 值 |
| --- | --- |
| 文档版本 | 1.0.0 |
| 盘点日期 | 2026-08-05 |
| 产品镜像 | `ghcr.io/openclaw/openclaw:2026.6.6` |
| 镜像 ID | `sha256:4826ca6157377e93463786d5c16852e34eede9f4bd4be55e3773cdc509762857` |
| 数据库 Schema | `schema_meta(role=global, schema_version=1)` |
| Session JSONL 格式 | 观测顶层 `type` 事件结构，无独立 Schema 版本 |

本字典只描述活动统计所需字段，不是 OpenClaw 完整数据库字典。

## 数据源

| 数据源 | 容器内路径 | 用途 |
| --- | --- | --- |
| 状态数据库 | `/home/node/.openclaw/state/openclaw.sqlite` | 任务、子智能体、定时任务等结构化计数 |
| Session JSONL | `/home/node/.openclaw/agents/main/sessions/*.jsonl` | 会话及用户交互计数 |

必须排除 `*.trajectory.jsonl`。该文件包含更完整的执行轨迹，不属于统计数据源。

## `openclaw.sqlite` 字段白名单

### `task_runs`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `task_id` | TEXT | 仅用于 `COUNT(*)` 或去重，不导出原值 |
| `status` | TEXT | 按状态统计任务次数 |
| `created_at` | INTEGER | 首次活动时间，Unix 毫秒 |
| `started_at` | INTEGER | 任务开始时间，Unix 毫秒 |
| `ended_at` | INTEGER | 任务结束时间，Unix 毫秒 |

禁止读取 `task`、`error`、`progress_summary`、`terminal_summary`、`terminal_outcome` 及 JSON 字段。

### `subagent_runs`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `run_id` | TEXT | 仅用于计数或去重，不导出原值 |
| `created_at` | INTEGER | 创建时间，Unix 毫秒 |
| `started_at` | INTEGER | 开始时间，Unix 毫秒 |
| `ended_at` | INTEGER | 结束时间，Unix 毫秒 |
| `ended_reason` | TEXT | 按稳定终止原因统计 |

禁止读取 `task`、`frozen_result_text`、`fallback_frozen_result_text`、`payload_json` 及错误详情。

### `cron_run_logs`

| 字段 | 类型 | 统计用途 |
| --- | --- | --- |
| `seq` | INTEGER | 每个 Job 内的增量序号，仅用于计数 |
| `status` | TEXT | 按状态统计运行次数 |
| `run_at_ms` | INTEGER | 运行时间，Unix 毫秒 |
| `duration_ms` | INTEGER | 聚合运行时长 |
| `total_tokens` | INTEGER | 聚合 Token 用量，可为空 |
| `created_at` | INTEGER | 记录创建时间，Unix 毫秒 |

禁止读取 `summary`、`error`、`delivery_error`、`entry_json`、模型名和 Provider 详情。

### 可选表

以下表仅在产生数据且 Schema 仍匹配时启用：

| 表 | 白名单字段 | 指标 |
| --- | --- | --- |
| `acp_sessions` | `state`, `last_activity_at`, `updated_at` | ACP 会话数、最后活动时间 |
| `command_log_entries` | `action`, `timestamp_ms` | 命令动作计数 |
| `flow_runs` | `status`, `created_at`, `ended_at` | Flow 运行次数 |

不得读取这些表中的身份、目录、目标、正文或 JSON 字段。

## Session JSONL 白名单

只处理非 trajectory 文件中的以下结构：

| JSON 路径 | 允许值/类型 | 统计用途 |
| --- | --- | --- |
| `type` | `session` | 会话数 |
| `type` | `message` | 进入消息分类 |
| `message.role` | `user` | `user_interactions` |
| `message.role` | `assistant` | 模型响应计数，可选 |
| `message.role` | `toolResult` | `tool_calls` 的保守近似值 |
| `timestamp` 或 `message.timestamp` | 时间值 | 最后活动时间，需在实现时验证格式 |

禁止读取 `content`、`details`、`summary`、工具名、usage 明细及其他任意字段值。
未知 `type` 或 `role` 默认忽略。

## 观测样本

测试实例在盘点时包含：9 个 Session 文件、1612 条 `user` 消息、1668 条
`assistant` 消息、80 条 `toolResult`、21 次成功任务、32 次完成的子智能体执行和
48 次成功定时任务。样本计数仅用于验证数据源，不作为产品默认值。

## 重新盘点条件

- OpenClaw 镜像 tag 或 digest 变化；
- `schema_meta` 的 `schema_version` 变化；
- Session JSONL 缺少现有字段或出现未识别的时间格式；
- 任一白名单表或字段缺失；
- 产品迁移或清理 Session/状态数据库。

