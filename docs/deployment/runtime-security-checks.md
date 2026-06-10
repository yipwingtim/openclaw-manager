# Runtime Security Checks

本文档说明如何检查 OpenClaw Manager 运行环境中的关键安全配置。

## 目标

`scripts/check_runtime_security.sh` 用于检查生产或测试环境中的运行时安全基线，重点覆盖：

- `OPENCLAW_INTERNAL_TOKEN` 是否已配置
- Nginx 代理到 `manager-web` 的配置是否携带 `X-OpenClaw-Internal-Token`
- Nginx 配置中的内部令牌值是否和 `OPENCLAW_INTERNAL_TOKEN` 一致
- `manager-web` 是否只连接 `manager-net`
- `openclaw-nginx` 是否同时连接 `agent-net` 和 `manager-net`
- 用户实例容器是否没有连接 `manager-net`
- 从 Nginx 容器内部不带 token 直连管理路由是否被 `manager-web` 返回 `403`

该脚本检查部署和网络安全状态，不替代 `scripts/check_metadata_consistency.py`。

## 使用方式

在 OpenClaw Manager 项目目录中执行：

```bash
./scripts/check_runtime_security.sh
```

如果存在安全基线错误，脚本会输出 `[ERROR]` 并以非 0 状态退出。

## 配置来源

脚本默认读取：

```text
config/openclaw-manager.env
```

常用变量：

```env
OPENCLAW_INTERNAL_TOKEN=
NGINX_CONF_DIR=/data/docker/nginx/conf
NGINX_CONTAINER_NAME=openclaw-nginx
MANAGER_WEB_CONTAINER_NAME=openclaw-manager-web
USER_CONTAINER_PREFIX=openclaw_
```

## 适合运行的时机

建议在以下场景运行：

- 首次部署完成后
- 修改 Nginx 配置后
- 修改 `OPENCLAW_INTERNAL_TOKEN` 后
- 重建 `manager-web` 或 `openclaw-nginx` 后
- 批量创建或恢复用户实例后

## 和元数据一致性检查的区别

`scripts/check_metadata_consistency.py` 检查用户业务状态是否一致，例如：

- `users.csv`
- SQLite 元数据
- 用户目录
- 用户 compose
- Nginx conf
- htpasswd

`scripts/check_runtime_security.sh` 检查部署安全边界是否仍然成立，例如：

- 网络隔离
- 内部代理令牌
- Nginx 到 `manager-web` 的受信代理路径

两者建议都运行。
