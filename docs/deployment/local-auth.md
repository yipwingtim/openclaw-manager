# Manager Web Authentication / Manager Web 认证

## Authentication policy / 认证策略

`manager-web` has exactly one active authentication provider at a time:

`manager-web` 同时只允许启用一种认证 Provider：

- `nginx-basic` (default): Nginx performs Basic Auth and forwards the
  authenticated username to manager-web.
- `nginx-basic`（默认）：由 Nginx 完成 Basic Auth，并将认证后的用户名传给 manager-web。

- `local`: manager-web displays its own login page and uses a server-side
  session.
- `local`：由 manager-web 提供登录页面并使用服务端 Session。

- A named external provider uses `MANAGER_AUTH_TYPE=oidc` or `oauth2` and
  Authlib's Authorization Code flow. OIDC Discovery is preferred.
- 命名的外部 Provider 使用 `MANAGER_AUTH_TYPE=oidc` 或 `oauth2`，通过 Authlib
  执行 Authorization Code 流程；优先使用 OIDC Discovery。

One platform user may bind both `nginx-basic` and `local` identities. Binding
multiple identities does not enable multiple login paths simultaneously; only
`MANAGER_AUTH_PROVIDER` is accepted.

同一个平台用户可以同时绑定 `nginx-basic` 和 `local` 身份，但这不代表两种入口同时可用；系统只接受 `MANAGER_AUTH_PROVIDER` 当前指定的方式。

Changing the active provider invalidates existing manager sessions. OpenClaw
application Token login is independent and is not changed by this setting.

切换 Provider 会使现有管理端 Session 失效。OpenClaw 应用自身的 Token 登录与该设置相互独立，不受影响。

## Identity mapping / 身份映射

Every manager login resolves to an existing `users.id` through
`user_identities(provider, subject)`:

每次管理端登录都必须通过 `user_identities(provider, subject)` 映射到一个已经存在的 `users.id`：

```text
nginx-basic + Basic Auth username ─┐
local       + normalized username ├──> users.id
future UIS  + stable subject       ┘
```

External authentication must not create a platform user on first login. Future
OIDC or UIS integrations must pre-provision the platform user and matching
identity record. Use the external provider's stable subject, not a display
name, email address, or other mutable field.

外部认证首次登录不得自动创建平台用户。未来接入 OIDC 或 UIS 时，必须预先创建平台用户并绑定对应身份；身份标识应使用外部系统提供的稳定 subject，不能默认使用姓名、邮箱等可变字段。

## External OAuth2/OIDC provider / 外部 OAuth2/OIDC Provider

Use a deployment-specific provider name such as `campus-uis` or `company-sso`.
Do not use a school name or endpoint in committed defaults. For OIDC:

使用部署侧 Provider 名称，例如 `campus-uis` 或 `company-sso`。仓库默认配置不得写入
真实学校名称或地址。OIDC 配置示例：

```dotenv
MANAGER_AUTH_PROVIDER=campus-uis
MANAGER_AUTH_TYPE=oidc
MANAGER_SESSION_SECRET=<random-high-entropy-secret>
MANAGER_OAUTH_CLIENT_ID=<client-id>
MANAGER_OAUTH_CLIENT_SECRET=<client-secret>
MANAGER_OAUTH_REDIRECT_URI=https://manager.example.test:30015/auth/callback
MANAGER_OIDC_DISCOVERY_URL=https://sso.example.test/.well-known/openid-configuration
MANAGER_OAUTH_SCOPES=openid profile email
MANAGER_OAUTH_SUBJECT_CLAIM=sub
```

Generic OAuth2 providers additionally require authorization, token, and
UserInfo endpoints. `MANAGER_OAUTH_SUBJECT_CLAIM` must identify an immutable,
unique subject returned by UserInfo. The callback rejects identities that are
not already linked through `user_identities(provider, subject)`.

通用 OAuth2 还需要授权、Token 和 UserInfo 地址。`MANAGER_OAUTH_SUBJECT_CLAIM`
必须指向 UserInfo 返回的稳定唯一标识。回调只接受已通过
`user_identities(provider, subject)` 绑定的平台用户。

预绑定外部身份：

```bash
python3 scripts/metadata_cli.py bind-identity \
  --username alice \
  --provider campus-uis \
  --subject '<stable-subject>'
```

```dotenv
MANAGER_OAUTH_AUTHORIZE_URL=https://sso.example.test/authorize
MANAGER_OAUTH_TOKEN_URL=https://sso.example.test/token
MANAGER_OAUTH_USERINFO_URL=https://sso.example.test/userinfo
```

### Emergency entry / 应急入口

For an external provider only, `/emergency/login` is protected by the existing
Nginx administrator Basic Auth file. Set `MANAGER_EMERGENCY_USERS` to an
allowlist of existing active platform administrators. Keep this path restricted
to an internal or controlled network and monitor manager-web warning logs.

仅外部 Provider 模式提供 `/emergency/login`。该精确路径使用现有 Nginx 管理员
Basic Auth 文件，并由 `MANAGER_EMERGENCY_USERS` 再次限制为已有且启用的平台管理员。
应通过网络策略限制该路径，并监控 manager-web 警告日志。
应急入口还要求配置非空的 `OPENCLAW_INTERNAL_TOKEN`，否则应用拒绝建立 Session。

## Prerequisite: identity model / 前置条件：身份模型

Local Auth requires the user/identity/instance model. Complete that migration
first, then run the Local Auth migration to create or finalize schema v3:

Local Auth 依赖用户、身份和实例模型。必须先完成该模型迁移，再执行 Local Auth 迁移以创建或补全 schema v3：

[`User, Identity, and Instance Migration`](../architecture/user-identity-instance-migration.md)

Resolve the configured database and administrator list:

确认实际数据库和管理员配置：

```bash
source config/openclaw-manager.env
public_dir="${OPENCLAW_PUBLIC_DIR:-/data/docker/openclaw-public}"
db_file="${METADATA_DB_FILE:-$public_dir/manager.db}"
admin_users="${MANAGER_ADMIN_USERS:-openclaw}"
```

Run the migration plan:

执行迁移规划：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_local_auth_model.py \
  --db "$db_file" \
  --admins "$admin_users"
```

Expected output includes at least one administrator:

预期输出中至少应匹配一名管理员：

```text
[PLAN] users=<count> admins=<count greater than zero> provider=nginx-basic
```

If `admins=0`, do not apply the migration. The configured Basic Auth
administrator exists only in `.htpasswd` and must first be provisioned as a
platform user.

如果 `admins=0`，不要执行 apply。这表示配置的 Basic Auth 管理员只存在于 `.htpasswd`，尚未预置为平台用户。

Apply the migration after preflight succeeds:

预检查通过后应用迁移：

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 \
  python3 scripts/migrate_local_auth_model.py \
  --db "$db_file" \
  --admins "$admin_users" \
  --apply
```

The migration creates `manager.db.pre-v3-<timestamp>.bak`.

迁移会自动创建 `manager.db.pre-v3-<timestamp>.bak`。

## Configure a Local credential / 配置 Local 凭据

Local credentials can only be added to an existing, non-deleted platform user.
The helper:

Local 密码只能配置给已经存在且未删除的平台用户。密码工具会：

- require a password of at least 12 characters;
- 使用至少 12 位密码；

- store a one-way scrypt password hash;
- 仅保存 scrypt 单向密码哈希；

- create or update the user's `local` identity;
- 创建或更新该用户的 `local` 身份；

- reset the failed-login counter and lock state.
- 重置登录失败次数和锁定状态。

Run the helper interactively inside manager-web:

在 manager-web 容器内交互式设置密码：

```bash
docker exec -it openclaw-manager-web \
  python /opt/openclaw-manager/scripts/set_local_password.py <username> --role user
```

For a platform administrator:

平台管理员使用：

```bash
docker exec -it openclaw-manager-web \
  python /opt/openclaw-manager/scripts/set_local_password.py <username> --role admin
```

Do not place the password in a command argument. Self-service password changes
are not currently implemented; an administrator must run this helper to set or
reset a Local password.

不要把密码放入命令参数。当前尚未实现用户自助修改密码，Local 密码的设置或重置需要由管理员执行该工具。

## Switch to Local Auth / 切换到 Local 认证

Set:

配置：

```dotenv
MANAGER_AUTH_PROVIDER=local
MANAGER_SESSION_HOURS=8
MANAGER_COOKIE_SECURE=true
```

`MANAGER_COOKIE_SECURE=true` requires HTTPS and should remain enabled in
production.

生产环境应保持 `MANAGER_COOKIE_SECURE=true`，并通过 HTTPS 访问。

Deploy the services:

部署服务：

```bash
bash scripts/deploy_services.sh
```

Deployment builds manager-web, updates the manager and legacy instance Nginx
routes, validates Nginx, reloads it, and then starts the updated services.

部署过程会构建 manager-web、更新管理端及旧实例 Nginx 路由、校验并 reload Nginx，然后启动更新后的服务。

In Local mode:

Local 模式下：

- `https://<PUBLIC_HOST>:30015/` displays the Local login page.
- `https://<PUBLIC_HOST>:30015/` 显示 Local 登录页。

- A legacy per-instance `https://<host>:<instance-port>/admin/` route redirects
  to port 30015 and no longer accepts Basic Auth.
- 实例独立端口的旧版 `/admin/` 路由会跳转到 30015，不再接受 Basic Auth。

- Five failed password attempts lock the Local credential for 15 minutes.
- Local 密码连续失败 5 次后会锁定 15 分钟。

- Sessions are stored server-side; the browser receives an `HttpOnly`,
  `Secure`, `SameSite=Lax` cookie.
- Session 保存在服务端；浏览器 Cookie 使用 `HttpOnly`、`Secure` 和 `SameSite=Lax`。

## Switch back to nginx-basic / 切回 nginx-basic

Set:

配置：

```dotenv
MANAGER_AUTH_PROVIDER=nginx-basic
```

Deploy again:

重新部署：

```bash
bash scripts/deploy_services.sh
```

Both the unified manager entry and legacy per-instance `/admin/` entry then use
Basic Auth. Local credentials remain stored but cannot be used until the
provider is switched back to `local`.

此时统一管理入口和旧版实例 `/admin/` 入口都会使用 Basic Auth。Local 凭据仍保留在数据库中，但切回 `local` 前不能用于登录。

## Apply Nginx routing only / 仅更新 Nginx 认证路由

If the manager-web container already uses the desired provider but existing
instance configs have not been updated, run:

如果 manager-web 容器已经使用目标 Provider，但历史实例配置尚未更新，可执行：

```bash
bash scripts/update_manager_auth.sh
```

The script updates active, disabled, legacy-disabled, and deleted-instance
Nginx configs. It creates a backup, runs `nginx -t`, reloads Nginx, and restores
the previous configs if validation or reload fails.

脚本会更新运行中、已停止、旧版停止目录及已删除实例的 Nginx 配置；它会先备份，再执行 `nginx -t` 和 reload，校验或 reload 失败时自动恢复。

Example success output:

成功输出示例：

```text
[INFO] Updated legacy instance admin entry for <count> config(s)
[INFO] Backup: /data/docker/nginx/conf/.manager-auth-backups/<backup>
[INFO] manager-web authentication provider configured: local
```

## Validation / 验证

Confirm that configuration and the running container agree:

确认配置文件和运行中的容器使用相同 Provider：

```bash
grep '^MANAGER_AUTH_PROVIDER=' config/openclaw-manager.env

docker inspect openclaw-manager-web \
  --format '{{range .Config.Env}}{{println .}}{{end}}' |
  grep '^MANAGER_AUTH_PROVIDER='
```

For Local mode, verify one legacy instance route without using an HTTP proxy:

Local 模式下，选择一个旧实例入口并绕过 HTTP 代理验证：

```bash
curl --noproxy '*' -skI \
  "https://127.0.0.1:<instance-port>/admin/"
```

Expected:

预期：

```text
HTTP/1.1 302 Moved Temporarily
Location: https://<PUBLIC_HOST>:30015/
```

Finally verify in a browser that:

最后通过浏览器确认：

1. port 30015 accepts a configured Local account;
2. an ordinary user cannot access another user's management page;
3. an ordinary user receives `403` on administrator routes;
4. a legacy per-instance `/admin/` URL redirects to port 30015.

1. 30015 可以使用已配置的 Local 账号登录；
2. 普通用户不能访问其他用户的管理页面；
3. 普通用户访问管理员路由时返回 `403`；
4. 实例独立端口的旧版 `/admin/` 会跳转到 30015。

## Rollback / 回滚

The deployment and Nginx update scripts print the authentication-config backup
directory. To restore that routing state:

部署和 Nginx 更新脚本会输出认证配置备份目录，可使用以下命令恢复：

```bash
bash scripts/update_manager_auth.sh --restore <backup-directory>
```

Then restore the previous `MANAGER_AUTH_PROVIDER` value and redeploy
manager-web. Restoring Nginx alone is not sufficient if the running
manager-web container uses a different provider.

随后恢复原来的 `MANAGER_AUTH_PROVIDER` 并重新部署 manager-web。仅恢复 Nginx 不足以完成回滚，Nginx 与实际运行的 manager-web Provider 必须保持一致。
