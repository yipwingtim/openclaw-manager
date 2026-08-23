# Activity Adapter 统计设计

## 文档状态

| 项目 | 值 |
| --- | --- |
| 文档版本 | 1.0.0 |
| 盘点日期 | 2026-08-05 |
| 状态 | 第一版手动采集与累计快照已实现 |

## 目标

Activity Adapter 用于汇总平台托管实例的低敏使用成效，不用于替代产品日志或故障排查。
Manager 只保存抽象统计，不保存 Prompt、回复正文、推理内容、工具参数、文件内容或外部身份凭据。

第一版按实例采集累计计数快照，而不是复制产品内部的每条事件：

```text
instance_id
product
collected_at
source_version
metrics_json
source_cursor
```

`metrics_json` 只允许保存各产品数据字典定义的数值型累计指标白名单。

Manager 根据相邻快照计算日、周、月增量。计数下降通常表示产品升级、数据清理或数据源重建，
此时应建立新基线，不生成负增量。

## 统一指标

| 指标 | 含义 | 注意事项 |
| --- | --- | --- |
| `sessions` | 产品内部会话或研究线程数 | 不等同于独立用户数 |
| `user_interactions` | 可明确识别的用户消息或请求数 | 不采集正文 |
| `messages` | 产品记录的消息总数 | 产品间口径可能不同 |
| `tool_calls` | 工具调用或工具结果数 | 不采集工具名、参数和结果 |
| `task_runs` | 独立任务执行次数 | 可按安全状态枚举拆分 |
| `subagent_runs` | 子智能体执行次数 | 当前仅 OpenClaw 可稳定提供 |
| `scheduled_runs` | 定时任务执行次数 | 可按安全状态枚举拆分 |
| `model_calls` | 模型 API 调用次数 | 不采集请求和响应内容 |
| `checkpoints` | 工作流持久化 checkpoint 数 | 仅表示执行活跃度 |
| `write_tasks` | 工作流写入任务数 | 不等同于用户操作次数 |
| `last_activity_at` | 数据源可确认的最后活动时间 | 缺少时间字段时可使用数据库 mtime，并标明来源 |
| `disk_bytes` | 实例数据目录内普通文件的总字节数 | 不跟随符号链接 |
| `session_files` | 会话目录或文件名可明确识别的文件数 | 仅用于运维预警，不代表会话数量 |

指标必须同时带 `product`，不得直接比较不同产品中语义不一致的原始计数。

## 用户归属

- 单一 Owner 实例可以展示为“该用户拥有实例的活动”，但不声称每次活动都由 Owner 本人发起。
- 共享实例在没有可信产品身份字段时只统计到实例维度。
- Activity Adapter 不从产品内部的自由文本、IP、用户名或显示名推断平台用户。
- 未来统一入口提供签名的平台用户身份后，才可记录可靠的用户级访问事件。

## 隐私边界

严禁集中采集：

- Prompt、回复正文、reasoning、system prompt；
- 工具名称、参数、结果和错误详情；
- 文件名、文件路径、文件内容；
- Session ID、thread ID、任务正文和项目名称；
- Token、密钥、身份 JSON、设备凭据；
- 原始日志行及任意 BLOB/JSON 载荷。

Adapter 应使用明确的表、字段和事件类型白名单。产品升级后出现的新字段默认不采集。

## 采集约束

- 数据源必须以只读模式打开；SQLite 使用 URI `mode=ro` 和 `PRAGMA query_only = ON`。
- 查询只返回聚合计数、低敏状态枚举和时间范围。
- 单个实例采集失败不得影响该实例运行，也不得阻断其他实例采集。
- 快照必须记录镜像版本和 Schema 版本或指纹，以便识别口径变化。
- 数据库锁定、缺失或 Schema 不匹配时跳过采集并记录脱敏错误类型。
- 资源统计由 Executor 在显式快照采集时执行；实例列表只读取最近快照，不同步扫描目录。

## 产品数据字典

- [OpenClaw](openclaw-data-dictionary.md)
- [Hermes](hermes-data-dictionary.md)
- [EvoScientist](evoscientist-data-dictionary.md)

## 后续代码 PR 建议

当前版本提供 Schema v7 活动快照、三个只读 Activity Adapter、管理员手动采集、按产品和
运行状态筛选、分页，以及平台总览中的 Activity 结果统计。平台总览同时展示最近实例和
最近操作，并对长版本 digest 使用截断加悬浮完整值。后续建议：

1. 验证多个生产版本的数据源兼容性，并补充版本白名单。
2. 增加定时采集任务、保留期限和历史增量聚合。
3. 增加按产品和时间范围的趋势图及导出。

## 部署与验证

升级前必须备份并迁移 Manager 数据库：

```bash
python3 scripts/migrate_activity_snapshots.py \
  --db /data/docker/openclaw-public/manager.db

python3 scripts/migrate_activity_snapshots.py \
  --db /data/docker/openclaw-public/manager.db \
  --apply
```

第一条为 dry-run；第二条会先生成 `manager.db.pre-v7-<timestamp>.bak`，再在事务中创建
快照表。迁移完成后重建 `manager-control`、`manager-executor-api`、`manager-admin-web`
和 `manager-user-web`。管理员可从 `/admin/activity` 手动刷新未删除实例。
