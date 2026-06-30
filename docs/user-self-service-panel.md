# User Self-Service Panel

## 1. 功能定位

`manager-web` 是 OpenClaw Manager 的第一版用户自助面板。

它的目标不是给用户开放 SSH、容器 shell 或宿主机权限，而是把常用管理动作封装成受控 Web 操作。用户只看到自己的实例信息和平台允许执行的白名单动作。

当前 MVP 重点解决三个问题：

- 用户首次登录 OpenClaw Control UI 时，如果出现 Device Pairing，可通过管理面板触发审批流程，而不必让管理员手工进入服务器执行脚本。
- 用户不需要额外记住 `30015` 管理端口，可直接在自己的实例端口访问 `/admin/`。
- 用户可以通过受控页面上传文件，并下载工作区中生成的常见导出文件。

## 2. 当前 MVP 能力

当前版本提供以下能力：

- 通过实例端口 `/admin/` 自动进入当前实例的管理页面
- 通过管理员入口输入 `user_id` 打开实例页面
- 查看 OpenClaw 实例状态
- 查看实例访问端口
- 查看实例访问 URL
- 查看设备缓存 `devices.txt`
- 审批最新 pending device request
- 生成微信插件绑定链接
- 上传文件到实例 `uploads` 目录
- 查看并下载用户工作区中的常见导出文件
- 删除顶层用户生成文件或上传文件
- 支持实例端口内的直链下载，例如 `/admin/files/report.pdf`
- 提供实例端口内的中文操作说明页面 `/admin/help`
- 管理员可在 `30015` 的 `/admin/create-user` 创建单个实例
- 管理员可在 `30015` 的 `/admin/users` 启停、重启、删除实例，并切换 Basic Auth
- 管理员可在 `30015` 的 `/admin/users` 为多个运行中的实例批量安装白名单 Skill

审批动作背后调用：

```bash
scripts/approve_device.sh <user_id> --latest
```

## 3. 访问链路

### 3.1 用户推荐入口

推荐用户只记自己的实例端口：

```text
https://<PUBLIC_HOST>:<USER_PORT>/
```

如果需要审批设备、上传文件或查看下载列表，访问：

```text
https://<PUBLIC_HOST>:<USER_PORT>/admin/
```

中文说明页面：

```text
https://<PUBLIC_HOST>:<USER_PORT>/admin/help
```

该入口由每个用户实例的 Nginx 配置代理到 `manager-web`：

```text
User Browser
  -> https://<PUBLIC_HOST>:<USER_PORT>/admin/
  -> openclaw-nginx
  -> HTTPS + Basic Auth
  -> openclaw-manager-web:8080/instance-admin/
  -> scripts/approve_device.sh / Docker API / user workspace
```

Nginx 会通过 `X-OpenClaw-User` header 将当前实例的 `user_id` 传给 `manager-web`，因此用户不需要在 `/admin/` 页面再次选择自己的账号。

`X-OpenClaw-User` 是内部信任 header，只应由 Nginx 注入。用户 OpenClaw 容器应只加入 `agent-net`，`manager-web` 应只加入 `manager-net`，Nginx 同时加入两个网络并作为唯一反向代理入口。

生产环境启用网络隔离时，应先创建 `manager-net` 并让 Nginx 同时加入 `agent-net` 和 `manager-net`，再重建 `manager-web`。如果先把 `manager-web` 移到 `manager-net`，但 Nginx 尚未加入该网络，实例端口 `/admin/` 会返回 502。

还需要把 `manager-net` 写入 Nginx 的 compose 文件；只执行 `docker network connect manager-net openclaw-nginx` 属于运行时变更，未来 Nginx 容器被 compose 重建后可能丢失该网络。

可用以下命令验证隔离是否生效：

```bash
docker exec openclaw-nginx sh -lc 'wget -qO- -T 3 http://openclaw-manager-web:8080/ >/dev/null && echo "[OK] nginx can reach manager-web"'
docker exec openclaw_<user_id> sh -lc 'getent hosts openclaw-manager-web || true'
docker exec openclaw_<user_id> sh -lc 'wget -qO- -T 3 http://openclaw-manager-web:8080/ 2>&1 || echo "[OK] blocked"'
```

预期结果：Nginx 可以访问 `manager-web`；普通用户实例容器不能解析或不能访问 `openclaw-manager-web:8080`。

如果文件名在允许目录中唯一，也可以使用直链下载：

```text
https://<PUBLIC_HOST>:<USER_PORT>/admin/files/<filename>
```

例如：

```text
https://<PUBLIC_HOST>:30007/admin/files/report.pdf
```

### 3.2 管理员兼容入口

`30015` 仍可作为管理员入口或兼容入口：

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

### 5.1 实例端口 `/admin/`

新建实例时，`scripts/create_user.sh` 会在用户 Nginx 配置中自动加入：

```nginx
location = /admin {
    return 302 /admin/;
}

location /admin/ {
    auth_basic "OpenClaw Login";
    auth_basic_user_file /etc/nginx/auth/.htpasswd;

    proxy_pass http://openclaw-manager-web:8080/instance-admin/;

    proxy_set_header X-OpenClaw-User "<user_id>";
}
```

已有实例不会自动更新 Nginx 配置。需要执行：

```bash
cd /data/docker/openclaw-manager
./scripts/enable_instance_admin.sh <user_id> [user_id ...]
```

然后检查并 reload Nginx：

```bash
docker exec openclaw-nginx nginx -t
docker exec openclaw-nginx nginx -s reload
```

### 5.2 管理员入口 `30015`

示例管理员入口：

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

如果仍使用 `30015` 管理员入口，外部访问前需要在数据中心或云防火墙中放行 TCP `30015`。普通用户优先使用自己的实例端口 `/admin/`，无需额外记住 `30015`。

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

实例端口 `/admin/` 依赖 Nginx 注入的 `X-OpenClaw-User` header 来绑定当前实例用户；全局 `30015` 管理入口仍依赖认证用户或管理员权限。

Basic Auth 可按实例关闭。关闭后，该实例端口的 `/` 和 `/admin/` 都不再弹出 Nginx Basic Auth，但仍保留 OpenClaw Token 和 Device Approval。该模式只建议用于可信内网培训实例。

微信绑定功能只执行固定的腾讯微信 OpenClaw CLI 安装命令，并从命令输出中提取绑定 URL；它不向用户开放任意 shell 命令。该功能会在后台等待用户打开链接或扫码确认，依赖实例容器可访问 npm registry，并可运行 `npx -y @tencent-weixin/openclaw-weixin-cli install`。绑定命令默认等待 300 秒，可通过 `MANAGER_WECHAT_BIND_TIMEOUT` 调整。

已有实例切换示例：

```bash
./scripts/set_basic_auth.sh false <user_id>
docker exec openclaw-nginx nginx -t
docker exec openclaw-nginx nginx -s reload
```

管理员也可以在 `https://<PUBLIC_HOST>:30015/admin/users` 的用户列表中切换 Basic Auth。页面会先备份用户 Nginx 配置，测试配置有效后 reload Nginx；如果测试或 reload 失败，会恢复原配置。

## 7. 与现有脚本的关系

当前实现不会替代现有脚本，而是把脚本能力包装成 Web 动作。

现阶段对应关系：

```text
Approve Latest Device
  -> scripts/approve_device.sh <user_id> --latest

Refresh Device Cache
  -> scripts/approve_device.sh <user_id> --list-only

Generate WeChat Bind URL
  -> docker exec openclaw_<user_id> sh -lc 'timeout ${MANAGER_WECHAT_BIND_TIMEOUT:-300}s npx -y @tencent-weixin/openclaw-weixin-cli install'

Enable instance-local /admin
  -> scripts/enable_instance_admin.sh <user_id> [user_id ...]

Set Basic Auth
  -> scripts/set_basic_auth.sh <true|false> <user_id> [user_id ...]

Create Instance
  -> scripts/create_user.sh <user_id> --basic-auth-enabled <true|false> [--password <password>]

Start / Stop / Restart Instance
  -> docker compose up -d / stop / restart in the user directory

Delete Instance
  -> scripts/delete_user.sh <user_id>

Bulk Install Skill
  -> docker exec openclaw_<user_id> openclaw skills install <skill_id>
```

后续可以继续纳入：

- `restart_instance`
- `view_logs`
- `update_skill`
- `get_access_info`

`https://<PUBLIC_HOST>:30015/admin/create-user` 已支持管理员创建单个实例和 Web 批量创建实例。单实例创建用于临时补开实例；批量创建用于培训名单，页面会上传或读取 CSV，先预检用户 ID、重复实例、端口余量和运行配置，再调用固定脚本 `scripts/batch_create_users.sh`。
创建成功后页面会显示访问地址、Basic Auth 状态、OpenClaw Login Token，并支持复制或下载账号 CSV。单实例记录会写入 `/data/docker/openclaw-public/accounts/<user_id>_account.csv`；批量创建结果保存在 `/data/docker/openclaw-public/batches/.../results.csv`，结果 CSV 包含账号、访问地址、端口、容器名、Token 和状态。

批量创建输入 CSV 表头：

```csv
user_id,basic_auth_password,basic_auth_enabled
training01,example-password,true
training02,,false
```

`basic_auth_password` 可留空，脚本会自动生成；`basic_auth_enabled` 可省略，默认启用。

`https://<PUBLIC_HOST>:30015/admin/users` 已支持管理员对单个实例执行 Start、Stop、Restart 和 Delete。Delete 是回收站删除，会移动用户数据并清理 Nginx 用户配置与端口映射。
用户列表默认隐藏 stopped 实例，可通过筛选条件查看全部或指定状态。

批量安装 Skill 功能只允许选择 `MANAGER_SKILL_PRESETS` 中配置的白名单 Skill。页面会默认填入当前筛选结果中的运行中实例，管理员可在提交前编辑目标实例列表。该功能不会开放任意 shell 命令，实际执行的是固定模板 `docker exec openclaw_<user_id> openclaw skills install <skill_id>`。

文件能力当前由 `manager-web` 直接处理：

- 上传文件写入 `/data/docker/openclaw-public/users/<user_id>/uploads`
- 下载只允许读取用户目录下的 `workspace`、`workspaces` 和 `uploads`
- 下载列表只显示这些目录的顶层文件，不递归展示 OpenClaw 运行过程中在子目录生成的内部文件
- 默认允许常见导出文件后缀，例如 `.md`、`.pdf`、`.docx`、`.xlsx`、`.csv`、`.zip`
- 允许后缀可通过 `MANAGER_DOWNLOAD_EXTENSIONS` 配置
- `/admin/files/<filename>` 只在文件名唯一时返回文件；如果重名，应使用页面中带目录信息的下载链接
- 删除只允许删除顶层、允许后缀、非保护名单文件
- 默认保护文件名包括 `AGENTS.md`、`SOUL.md`、`TOOLS.md`、`IDENTITY.md`、`USER.md`、`HEARTBEAT.md`、`BOOTSTRAP.md`、`MEMORY.md`
- 保护文件名可通过 `MANAGER_PROTECTED_FILENAMES` 配置

实例升级相关脚本不直接暴露给普通用户，管理员在服务器执行：

```bash
./scripts/check_instance_upgrade.sh <user_id>
./scripts/update_instance_version.sh <user_id> <version>
./scripts/update_instance_version.sh <user_id> <version> --restore-model-provider
```

`update_instance_version.sh` 会在升级前后自动调用 `check_instance_upgrade.sh`。升级前检查失败会中止升级；升级后如果发现模型 Provider 可能缺失，可手动执行 `set_model_provider.sh`，也可使用 `--restore-model-provider` 自动尝试恢复。

检查报告保存在：

```text
/data/docker/openclaw-public/users/<user_id>/backups/version-upgrades/<timestamp>/pre-check.txt
/data/docker/openclaw-public/users/<user_id>/backups/version-upgrades/<timestamp>/post-check.txt
```

`manager-web` 通过 Web 页面调用管理脚本，并需要 Docker API 管理实例容器。容器内需要 Docker CLI、Docker Compose plugin、`/var/run/docker.sock`，并挂载 OpenClaw Manager 项目目录、OpenClaw public 数据目录、Nginx conf/auth/compose 目录。`manager-web` 应只加入 `manager-net`；用户实例容器应只加入 `agent-net`；Nginx 需要同时加入两个网络。

Web 创建实例时，`create_user.sh` 会在成功后把用户目录、用户 Nginx 配置和 `users.csv` 的 owner 归还给宿主机数据目录 owner，避免后续宿主机脚本因为 root-owned 文件失败。Web 批量创建还要求 `/data/docker/openclaw-public/batches`、`users.csv`、`ports.txt`、Nginx 用户配置目录和 `auth/users` 对 manager-web 执行用户可写；可通过 `scripts/check_bootstrap_readiness.sh` 做部署前检查。

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
