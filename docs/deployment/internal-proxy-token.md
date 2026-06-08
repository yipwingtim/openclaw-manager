# Internal Proxy Token Deployment

本文档说明如何启用 `manager-web` 与 Nginx 之间的内部代理令牌校验。

## 目标

OpenClaw Manager 的管理入口由 Nginx 反向代理到 `manager-web`。启用内部代理令牌后，`manager-web` 不再无条件信任代理请求，而是要求请求携带共享密钥请求头：

```text
X-OpenClaw-Internal-Token
```

该机制用于防止能访问 `manager-web:8080` 的其他容器绕过 Nginx 直接访问管理端路由。

## 兼容行为

如果 `OPENCLAW_INTERNAL_TOKEN` 未配置，`manager-web` 会记录 warning，并保持旧行为：

```text
OPENCLAW_INTERNAL_TOKEN is not configured; internal proxy token checks are disabled.
```

这让生产环境可以先升级代码，再单独安排令牌启用和现有 Nginx 配置迁移。

## 启用步骤

风险提示：以下操作会修改生产环境 Nginx 代理配置和 `manager-web` 环境变量。建议先备份配置，并在低峰期执行。

1. 生成随机令牌：

```bash
openssl rand -hex 32
```

2. 写入 OpenClaw Manager 环境变量文件：

```env
OPENCLAW_INTERNAL_TOKEN=<replace-with-generated-token>
```

默认环境变量文件位置通常是：

```text
/data/docker/openclaw-manager/config/openclaw-manager.env
```

3. 更新所有代理到 `manager-web` 的 Nginx 配置，在对应 `location` 中加入：

```nginx
proxy_set_header X-OpenClaw-Internal-Token "<replace-with-generated-token>";
```

需要检查的配置通常包括：

- 管理端入口配置，例如 `/data/docker/nginx/conf/manager-web.conf`
- 各用户实例的 `/admin/` 代理配置，例如 `/data/docker/nginx/conf/<user_id>.conf`

4. 检查并重载 Nginx：

```bash
docker exec openclaw-nginx nginx -t
docker exec openclaw-nginx nginx -s reload
```

5. 重建或重启 `manager-web`，让环境变量生效：

```bash
cd /data/docker/openclaw-manager/services
docker compose up -d --build manager-web
```

## 新实例行为

启用 `OPENCLAW_INTERNAL_TOKEN` 后，后续通过以下脚本生成或启用的实例管理入口会自动写入内部令牌请求头：

- `scripts/create_user.sh`
- `scripts/enable_instance_admin.sh`

已有 Nginx 配置不会被自动批量改写，需要按上面的步骤迁移一次。

## 验证

1. 查看 `manager-web` 日志，确认不再出现未配置令牌的 warning：

```bash
docker logs --tail=80 openclaw-manager-web
```

2. 浏览器访问管理端入口和实例 `/admin/` 页面，确认仍能正常打开。

3. 如果可以从 `manager-net` 内发起无令牌请求，访问受保护管理路由应返回 `403`。

## 回滚

如果启用后出现异常，可以先移除或注释 `OPENCLAW_INTERNAL_TOKEN`，然后重建或重启 `manager-web`。Nginx 中已经添加的 `X-OpenClaw-Internal-Token` 请求头可以暂时保留，不会影响未启用令牌校验的 `manager-web`。

## 安全注意事项

- 不要把真实令牌提交到 Git。
- 令牌应使用足够长的随机值。
- 包含真实令牌的 Nginx 配置和环境变量文件应按敏感配置管理。
