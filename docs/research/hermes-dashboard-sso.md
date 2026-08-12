# Hermes Dashboard 接入校内 UIS 的可行性研究

## 结论

Hermes Agent `v2026.7.20` 可以把 Dashboard 的内置用户名/密码登录替换为统一身份认证，但不能简单关闭 Hermes 认证或信任 Nginx 注入的用户 Header。

安全可行的路径有两条：

1. 如果校内 UIS 是完整、标准的 OpenID Connect Provider，直接使用 Hermes 内置的 `self-hosted` OIDC Provider。
2. 如果 UIS 只有当前 Manager 已使用的 OAuth2 `authorize`、`token` 和 `userInfo` 接口，则实现一个 Hermes `DashboardAuthProvider` 插件，把 UIS OAuth2 登录结果转换为 Hermes 自己验证和签发的 Dashboard session。

第二条路径更符合本仓库目前掌握的 UIS 接口。用户仍会经过一次 Hermes OAuth 跳转，但浏览器已有 UIS 会话时，通常不会再次输入账号密码；是否无感取决于 UIS 的 SSO 会话策略。

## 已确认的 Hermes 官方能力

### Dashboard 认证门

Hermes 在非 loopback 地址上运行 Dashboard 时启用认证门。未认证请求必须持有经过 Provider 验证的 Hermes session；未配置 Provider 时服务 fail closed。官方 `v2026.7.20` 文档列出 Basic、Nous OAuth 和 self-hosted OIDC 三种内置方式：

- [Hermes v2026.7.20 Web Dashboard：Authentication](https://github.com/NousResearch/hermes-agent/blob/v2026.7.20/website/docs/user-guide/features/web-dashboard.md#authentication-gated-mode)

因此不应以 `--insecure` 或取消下游认证作为生产方案。Dashboard 可以读写 API key、配置、会话、Cron 和技能，单靠外围网络位置不足以保护这些高权限功能。

### 内置 self-hosted OIDC

Hermes 的 self-hosted Provider 要求：

- 标准 OIDC discovery：`{issuer}/.well-known/openid-configuration`
- authorization-code + PKCE S256
- public client，当前不支持需要 `client_secret` 的 confidential client
- ID Token 和 JWKS
- 校验 ID Token 的签名、`iss`、`aud`，并以 `sub` 作为稳定用户 ID
- 回调地址为 `<dashboard public URL>/auth/callback`

配置入口为 `HERMES_DASHBOARD_OIDC_ISSUER`、`HERMES_DASHBOARD_OIDC_CLIENT_ID` 和可选的 scopes：

- [Hermes v2026.7.20 Web Dashboard：Self-hosted OIDC provider](https://github.com/NousResearch/hermes-agent/blob/v2026.7.20/website/docs/user-guide/features/web-dashboard.md#self-hosted-oidc-provider)

只有校内 UIS 同时满足上述 OIDC 契约时，才能直接使用内置 Provider。现有 Manager 配置只证明 UIS 提供 OAuth2 authorization、token 和 userInfo 端点，不能据此推断其支持 OIDC、ID Token 或 JWKS。

### 自定义 DashboardAuthProvider

Hermes 官方提供认证插件扩展点。插件继承 `DashboardAuthProvider`，实现登录开始、授权码交换、session 验证、刷新与撤销，并通过 `ctx.register_dashboard_auth_provider()` 注册：

- [Hermes v2026.7.20 Web Dashboard：Custom providers](https://github.com/NousResearch/hermes-agent/blob/v2026.7.20/website/docs/user-guide/features/web-dashboard.md#custom-providers)
- [Hermes 官方插件注册实现](https://github.com/NousResearch/hermes-agent/blob/v2026.7.20/hermes_cli/plugins.py)

这使仅提供 OAuth2 + userInfo 的 UIS 也能接入，而无需修改 Hermes 上游源码。

## 与当前 Manager 架构的关系

当前 `instance-auth-proxy` 已完成第一层授权：验证 Manager UIS session，并确认访问者是管理员、实例 Owner 或显式成员。Hermes 入口随后仍保留官方 session，见：

- [`docs/deployment/instance-auth-proxy.md`](../deployment/instance-auth-proxy.md)
- [`docs/hermes-adapter.md`](../hermes-adapter.md)

现有代理返回 `X-OpenClaw-Authenticated-User`，但 Hermes 官方认证门没有把该 Header 定义为可信登录凭据。直接把 Header 当作 Hermes 身份会扩大信任边界，并可能在 Nginx 配置、网络连接或 Header 清理出错时形成认证绕过。

UIS Provider 方案应保留两层职责：

- Manager UIS 入口：决定某个平台用户是否有权访问该实例。
- Hermes UIS Provider：完成 Hermes 官方 session 的建立和后续请求验证。

用户体验上仍是一次 UIS 身份；两层服务分别做平台授权和产品 session，不共享或伪造对方的 Cookie。

## 推荐实施顺序

### 首选：先确认 UIS 是否支持 OIDC

向 UIS 管理方确认以下项目：

- 是否提供 HTTPS OIDC issuer 和 discovery 文档
- discovery 是否发布 `authorization_endpoint`、`token_endpoint`、`jwks_uri`
- authorization-code + PKCE S256 是否支持 public client
- 是否返回签名 ID Token
- ID Token 是否包含稳定 `sub`，并正确设置 `iss`、`aud`
- 是否允许为每个 Hermes 实例登记独立的 `/auth/callback`，或支持受控的回调地址模式

全部满足时，优先使用 Hermes 内置 self-hosted OIDC。这样代码最少，认证验证由 Hermes 官方实现承担。

### 备选：实现 campus-uis DashboardAuthProvider

若 UIS 不是 OIDC，则编写随 Hermes 数据目录部署的用户插件，复用 Manager 已验证过的 UIS OAuth2 契约：

1. `start_login` 生成 state 与 PKCE 数据并跳转 UIS。
2. `complete_login` 在服务端交换 code，并调用 UIS userInfo。
3. 只用 UIS 的稳定 `user_id/work_id` 作为 Hermes `user_id`；姓名、邮箱不能作为身份键。
4. `verify_session` 必须验证 Hermes session 的签名、有效期和 Provider，不接受浏览器自报 Header。
5. Token、code、client secret 和 session 不得写入普通日志。
6. Provider 或 UIS 不可用时 fail closed。

该插件应通过 Hermes 官方插件接口加载，不应 patch 容器内的 Hermes 源码。

## 不推荐方案

- `--insecure` 关闭 Hermes 认证。
- 只依赖 Nginx `auth_request`，让 Hermes Dashboard 裸奔在租户网络内。
- 让 Hermes 直接信任任意 `X-Remote-User`、`X-Forwarded-User` 或 `X-OpenClaw-Authenticated-User`。
- 在 Nginx 中复用或伪造 Hermes session Cookie。
- 使用姓名、邮箱等可变字段代替 UIS 稳定 subject。

## 上线前验证

建议先用一个非生产 Hermes 实例做 PoC，并至少验证：

- 未登录 UIS 时跳转至 UIS，不能访问任何 Dashboard API 或 WebSocket。
- 已登录 UIS 时无需再次输入账号密码，并成功建立 Hermes session。
- 非 Owner/成员即使 UIS 登录成功，也被 Manager 边缘授权拒绝。
- 篡改身份 Header、state、code、Cookie 或 callback 均失败。
- Session 过期、退出 UIS、Provider 不可用和 Hermes 重启时行为符合预期。
- `/api/status` 显示认证门开启且仅列出预期 Provider。
- Dashboard Auth 日志不包含 access token、refresh token、code、Cookie 或 client secret。

## 尚待确认

目前没有 UIS 官方公开规范可证明它支持标准 OIDC。因此“直接配置内置 self-hosted OIDC”仍是条件性方案；在拿到 UIS discovery URL 或协议文档前，不应上线该配置。若只有仓库现有的 OAuth2 + userInfo 接口，则采用自定义 Provider。

## Hermes v2026.7.20 插件接口核对（2026-08-12）

已对照 `v2026.7.20` 标签下的官方源码确认：

- 基类从 `hermes_cli.dashboard_auth` 导入，名称为 `DashboardAuthProvider`。
- Session Provider 必须实现 `start_login`、`complete_login`、`verify_session`、`refresh_session` 和 `revoke_session`。
- `complete_login` 与 `verify_session` 返回 `Session`，其必填字段为 `user_id`、`email`、`display_name`、`org_id`、`provider`、`expires_at`、`access_token`、`refresh_token`。Bridge 第一版不提供刷新时，`refresh_token` 使用空字符串。
- 用户插件目录为 `~/.hermes/plugins/<name>/`，必须包含 `plugin.yaml` 和带 `register(ctx)` 的 `__init__.py`。
- Provider 通过 `ctx.register_dashboard_auth_provider(provider)` 注册。
- 用户插件默认不加载，插件名必须加入 `config.yaml` 的 `plugins.enabled`；也可使用 `hermes_agent.plugins` Python entry point，但本项目不需要该打包方式。
- Hermes 登录路由负责核对 cookie 中的 state；Provider 的 `start_login` 返回 `LoginStart(redirect_url, cookie_payload)`，`complete_login` 负责后端兑换并返回官方 Session。

因此本项目固定采用 Manager Bridge + 自定义 Provider 契约，不复用 Hermes 内置 self-hosted OIDC，也不提供 discovery 或 ID Token。
