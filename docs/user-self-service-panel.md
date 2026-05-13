# User Self-Service Panel

## 1. 功能定位

`manager-web` 是 OpenClaw Manager 的第一版用户自助面板。

它的目标不是给用户开放 SSH、容器 shell 或宿主机权限，而是把常用管理动作封装成受控 Web 操作。用户只看到自己的实例信息和平台允许执行的白名单动作。

当前 MVP 重点解决一个问题：

- 用户首次登录 OpenClaw Control UI 时，如果出现 Device Pairing，可通过管理面板触发审批流程，而不必让管理员手工进入服务器执行脚本。

## 2. 当前 MVP 能力

当前版本提供以下能力：

- 输入 `user_id` 打开实例页面
- 查看 OpenClaw 实例状态
- 查看实例访问端口
- 查看实例访问 URL
- 查看设备缓存 `devices.txt`
- 审批最新 pending device request

审批动作背后调用：

```bash
scripts/approve_device.sh <user_id> --latest
```

## 3. 访问链路

推荐访问链路如下：

```text
User Browser
  -> https://<PUBLIC_HOST>:30015
  -> openclaw-nginx
  -> HTTPS + Basic Auth
  -> openclaw-manager-web:8080
  -> scripts/approve_device.sh / Docker API
```

`manager-web` 自身仍然只通过本机端口暴露：

```text
127.0.0.1:18082 -> openclaw-manager-web:8080
```

外部用户不应直接访问 `18082`，也不应将该端口加入公网白名单。

## 4. 服务部署

`manager-web` 定义在：

```text
services/docker-compose.yml
```

启动或更新服务：

```bash
cd /data/docker/openclaw-manager/services
docker compose up -d --build manager-web
```

检查服务状态：

```bash
docker ps --filter name=openclaw-manager-web
docker logs --tail 50 openclaw-manager-web
```

本机测试：

```bash
curl -I http://127.0.0.1:18082/
curl -I http://127.0.0.1:18082/users/<user_id>
```

## 5. Nginx 外部入口

当前建议通过 Nginx 暴露管理面板，而不是直接开放 Flask 服务端口。

示例入口：

```text
https://<PUBLIC_HOST>:30015
```

Nginx 配置文件：

```text
/data/docker/nginx/conf/manager-web.conf
```

示例配置：

```nginx
server {
    listen 30015 ssl;
    server_name _;

    ssl_certificate /etc/nginx/certs/nginx.crt;
    ssl_certificate_key /etc/nginx/certs/nginx.key;

    client_max_body_size 10M;

    location / {
        auth_basic "OpenClaw Manager";
        auth_basic_user_file /etc/nginx/auth/.htpasswd;

        proxy_pass http://openclaw-manager-web:8080;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }
}
```

Nginx compose 需要映射外部端口：

```yaml
ports:
  - "30015:30015"
```

更新 Nginx 前应先检查配置：

```bash
docker exec openclaw-nginx nginx -t
```

重新应用 Nginx compose：

```bash
cd /data/docker/nginx/compose
docker compose up -d
```

外部访问前，需要在数据中心或云防火墙中放行 TCP `30015`。

## 6. 安全边界

`manager-web` 需要访问 Docker API，并会调用管理脚本。因此它比普通 Web 页面权限更高。

必须遵守以下约束：

- 不直接公网开放 `18082`
- 不开放容器 shell
- 不给用户 SSH 权限
- 所有用户动作必须是白名单动作
- 管理入口必须放在 Nginx HTTPS 后面
- 管理入口必须启用 Basic Auth 或更强认证
- 后续应增加平台登录、用户与实例绑定、审计日志

当前 MVP 仍然通过输入 `user_id` 进入实例页面，因此它适合内测，不适合作为最终多租户权限模型。

## 7. 与现有脚本的关系

当前实现不会替代现有脚本，而是把脚本能力包装成 Web 动作。

现阶段对应关系：

```text
Approve Latest Device
  -> scripts/approve_device.sh <user_id> --latest
```

后续可以继续纳入：

- `restart_instance`
- `view_logs`
- `upload_file`
- `update_skill`
- `refresh_device_cache`
- `get_access_info`

## 8. 后续计划

建议按以下顺序演进：

- 使用 `gunicorn` 替代 Flask development server
- 增加平台登录
- 建立用户和实例绑定关系
- 增加审计日志
- 增加 CSRF 防护
- 将脚本调用收敛到 action dispatcher
- 支持更多用户自助动作
- 后续使用域名和 443 入口替代临时端口访问
