# Manager Web Authentication / Manager Web 认证

## Authentication policy / 认证策略

`MANAGER_AUTH_PROVIDER` selects the primary authentication provider:

`MANAGER_AUTH_PROVIDER` 用于选择主认证 Provider：

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

With an external primary provider, `MANAGER_LOCAL_AUTH_ENABLED=true` also exposes
the local username/password form. Both providers use the same `users` table.
`user_identities.provider` distinguishes identities and `local_credentials`
stores local password credentials. One platform user may bind both identities.

外部 Provider 作为主认证方式时，可设置 `MANAGER_LOCAL_AUTH_ENABLED=true` 同时开放
本地用户名密码登录。两类用户共用 `users` 表，通过 `user_identities.provider` 区分
身份，本地密码仍保存在 `local_credentials`；同一平台用户也可以同时绑定两类身份。

Local and external sessions can coexist in mixed mode. OpenClaw application
Token login is independent and is not changed by this setting.

混合模式下本地和外部 Session 可以并存。OpenClaw 应用自身的 Token 登录与该设置相互
独立，不受影响。

## Identity mapping / 身份映射

Every manager login resolves to an existing `users.id` through
`user_identities(provider, subject)`:

每次管理端登录都必须通过 `user_identities(provider, subject)` 映射到一个已经存在的 `users.id`：

```text
nginx-basic + Basic Auth username ─┐
local       + normalized username ├──> users.id
OAuth2/OIDC + stable subject       ┘
```

External authentication must not create a platform user on first login. OAuth2,
OIDC, and UIS deployments must pre-provision the platform user and matching
identity record. Use the external provider's stable subject, not a display name,
email address, or other mutable field.

外部认证首次登录不得自动创建平台用户。OAuth2、OIDC 和 UIS 部署必须预先创建平台用户
并绑定对应身份；身份标识应使用外部系统提供的稳定 subject，不能默认使用姓名、邮箱等
可变字段。

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
MANAGER_OAUTH_LOGOUT_URL=https://sso.example.test/logout
MANAGER_OAUTH_POST_LOGOUT_REDIRECT_URI=https://manager.example.test/login
```

### Campus UIS OAuth2 / 校内 UIS OAuth2

UIS identities can be imported in bulk without creating a second user table.
The CSV must contain `user_id` and `name`; `email` and
`status` (`active`, `disabled`, or `locked`) are optional. The command is a
dry-run by default and is idempotent when applied.

UIS 身份可以批量导入，无需新建第二张用户表。CSV 必须包含 `user_id` 和
`name`，可选字段为 `email` 和 `status`（`active`、`disabled` 或
`locked`）。命令默认只做预校验，使用 `--apply` 后可幂等执行。

```csv
user_id,name,email,status
12345,文杰,wenjie@example.edu.cn,active
```

```bash
python3 scripts/import_uis_users.py \
  --csv /data/docker/openclaw-public/uis-users.csv \
  --db /data/docker/openclaw-public/manager.db

python3 scripts/import_uis_users.py \
  --csv /data/docker/openclaw-public/uis-users.csv \
  --db /data/docker/openclaw-public/manager.db --apply
```

The import refuses duplicate CSV identities, updates users already linked by
`campus-uis + user_id`, creates an internal platform user for new identities, binds
`user_identities(provider=campus-uis)`, and records an `identity.import_uis`
operation. It never accepts or stores passwords.

导入会拒绝 CSV 内重复身份；按 `campus-uis + user_id` 更新已绑定的平台用户，
为新身份创建内部平台用户，写入 `user_identities(provider=campus-uis)` 并记录
`identity.import_uis` 操作；不会接收或保存密码。

管理员可通过 `/admin/platform-users` 查看 Local/UIS 身份来源并管理
`active`、`disabled`、`locked` 状态。停用或锁定用户会注销其现有平台 Session。

该页面也支持每批最多 100 行的 CSV 导入。一次导入必须选择一种身份来源：

```csv
user_id,name,email,status
20260001,张三,zhangsan@example.edu,active
```

UIS 导入按 `campus-uis + user_id` 幂等创建或更新用户；`email` 和 `status` 可留空。

```csv
username,name,email,password
alice,Alice,alice@example.edu,initial-pass-123
```

Local 导入仅创建新用户，默认角色和状态分别为 `user`、`active`。初始密码至少
12 位并以 scrypt 哈希保存，用户首次登录必须修改。任意一行失败时整批回滚。
Local CSV 包含明文初始密码，应通过受控渠道传递并在导入后安全删除。

New identities receive a deterministic internal username and are not merged with
an unbound Local user. Existing bindings keep their platform username. A blank
`status` defaults new users to `active` and preserves the status of existing users.

新身份使用确定性的内部平台用户名，不会与未绑定的 Local 用户自动合并；已有绑定保留
原平台用户名。`status` 留空时，新用户默认为 `active`，已有用户保持当前状态。

For an OAuth2 authorization-code provider whose UserInfo response uses
`user_id` as the stable campus identity, configure the production environment
with the assigned client credentials and registered callback URLs:

对于 UserInfo 使用 `user_id` 作为稳定校内身份的 OAuth2 授权码 Provider，在生产环境
配置分配的客户端凭据和已登记的回调地址：

```dotenv
MANAGER_AUTH_PROVIDER=campus-uis
MANAGER_AUTH_TYPE=oauth2
MANAGER_LOCAL_AUTH_ENABLED=true
MANAGER_SESSION_SECRET=<random-high-entropy-secret>
MANAGER_OAUTH_CLIENT_ID=<assigned-client-id>
MANAGER_OAUTH_CLIENT_SECRET=<assigned-client-secret>
MANAGER_OAUTH_REDIRECT_URI=https://<manager-host>:<port>/auth/callback
MANAGER_OAUTH_AUTHORIZE_URL=https://<uis-host>/idp/authCenter/authenticate
MANAGER_OAUTH_TOKEN_URL=https://<uis-host>/idp/api/v3/oauth2/token
MANAGER_OAUTH_USERINFO_URL=https://<uis-host>/idp/api/v3/oauth2/userInfo
MANAGER_OAUTH_SCOPES=
MANAGER_OAUTH_SUBJECT_CLAIM=user_id
MANAGER_OAUTH_LOGOUT_URL=https://<uis-host>/idp/authCenter/GLO
MANAGER_OAUTH_POST_LOGOUT_REDIRECT_URI=https://<manager-host>:<port>/login
```

Register these application callbacks with UIS:

向 UIS 登记以下应用回调：

```text
Login callback / 登录回调: https://<manager-host>:<port>/auth/callback
Server logout callback / 服务端登出回调: https://<manager-host>:<port>/auth/uis/logout
```

The token endpoint uses `client_secret_basic`. The application stores only a
SHA-256 hash of the UIS access token and links that hash to Manager sessions.
The server logout callback hashes the received `token` and revokes every linked
Manager session. The Nginx exact-match location disables access logging, moves
the query token into an internal header, and removes the query string before
proxying. It also discards error logs for this exact location so the access
token does not enter Nginx or application request logs. UIS profiles persist
only `user_id`, `user_name`, `user_type`, `email`, and `department`; mobile and
UIS session identifiers are discarded.

Token 端点使用 `client_secret_basic`。应用仅保存 UIS access token 的 SHA-256
哈希，并将其关联到 Manager Session。服务端登出回调对收到的 `token` 计算哈希，撤销
所有关联的 Manager Session。Nginx 精确匹配该路径并关闭访问日志，将查询参数 token
转为内部请求头，同时在反向代理前移除查询串，避免 access token 进入应用请求日志。
该精确路径的错误日志也会被丢弃，避免 token 进入 Nginx 日志。UIS Profile 仅保留
`user_id`、`user_name`、`user_type`、`email` 和 `department`，不保存手机号和 UIS
会话标识。

Before enabling the provider, pre-bind each returned `user_id` to an existing
platform user. First login never creates a platform user automatically:

启用 Provider 前，将每个 UIS 返回的 `user_id` 预绑定到已有平台用户。首次登录不会自动
创建平台用户：

```bash
python3 scripts/metadata_cli.py bind-identity \
  --username alice \
  --provider campus-uis \
  --subject '<user_id>'
```

The migration is a database write. Schedule a short Manager maintenance window:
stop the split Manager Web, Control, and Executor services first so the backup
and migration see one consistent database state. Tenant runtime containers do
not need to stop. Then migrate the metadata database to schema v6:

```bash
python3 scripts/migrate_external_session_tokens.py \
  --db /data/docker/openclaw-public/manager.db --apply
```

The migration creates a `manager.db.pre-v6-<timestamp>.bak` backup before it
creates the external-token/session association table. Then rerun
`scripts/update_manager_auth.sh` to render the protected logout location,
validate Nginx, and rebuild the split Manager Web services according to the
normal deployment procedure.

If migration or verification fails, keep Manager services stopped, restore the
generated `manager.db.pre-v6-<timestamp>.bak` over `manager.db`, and redeploy the
previous code revision before restarting Manager services.

该迁移会写数据库。请安排短暂的 Manager 维护窗口，先停止拆分后的 Manager Web、
Control 和 Executor 服务，确保备份与迁移期间数据库不再写入；租户实例容器无需停止。
随后使用上述命令将元数据数据库迁移到 v6。迁移会先创建
`manager.db.pre-v6-<timestamp>.bak` 备份，再创建外部 token 与 Session 关联表。随后
重新运行 `scripts/update_manager_auth.sh` 生成受保护的登出路由并校验 Nginx，再按常规
部署流程重建拆分后的 Manager Web 服务。

如果迁移或验证失败，保持 Manager 服务停止，用生成的
`manager.db.pre-v6-<timestamp>.bak` 覆盖恢复 `manager.db`，部署上一版代码后再启动
Manager 服务。

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
