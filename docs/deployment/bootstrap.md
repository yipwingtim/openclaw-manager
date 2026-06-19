# Fresh Environment Bootstrap

This guide explains how to prepare a clean host for OpenClaw Manager.

本文档说明如何在全新服务器上准备 OpenClaw Manager 的运行环境。

## Supported Environment | 支持环境

Recommended operating systems:

- Ubuntu 22.04 LTS
- Ubuntu 24.04 LTS

建议使用以下操作系统：

- Ubuntu 22.04 LTS
- Ubuntu 24.04 LTS

Other Linux distributions may work, but they are not the primary target yet.

其他 Linux 发行版可能可以运行，但当前不是主要支持目标。

## Required Software | 预装软件

Install these before running `scripts/bootstrap_runtime.sh`:

运行 `scripts/bootstrap_runtime.sh` 前，应先安装：

- `bash`
- `python3`
- `sudo`
- Docker Engine
- Docker Compose plugin

User and auth operations also need:

用户创建和认证相关操作还需要：

- `apache2-utils`, for `htpasswd`

On Ubuntu, the dependency installation is typically:

Ubuntu 上通常可使用以下方式安装依赖：

```bash
sudo apt update
sudo apt install -y python3 apache2-utils ca-certificates curl gnupg
```

Install Docker Engine and the Docker Compose plugin using Docker's official Ubuntu installation guide.

Docker Engine 和 Docker Compose plugin 建议按 Docker 官方 Ubuntu 安装文档安装。

The current shell user must either be `root` or have permission to run Docker commands:

当前执行用户必须是 `root`，或具有运行 Docker 命令的权限：

```bash
docker ps
docker compose version
```

If using a non-root user, add the user to the `docker` group and re-login:

如果使用非 root 用户，可加入 `docker` 用户组后重新登录：

```bash
sudo usermod -aG docker "$USER"
```

## What Bootstrap Does | Bootstrap 会做什么

`scripts/bootstrap_runtime.sh` creates missing runtime scaffolding only. It does not overwrite existing runtime files.

`scripts/bootstrap_runtime.sh` 只创建缺失的运行骨架，不覆盖已有运行文件。

It creates or initializes:

- `/data/docker/openclaw-public/users`
- `/data/docker/openclaw-public/deleted`
- `/data/docker/openclaw-public/logs`
- `/data/docker/nginx/conf`
- `/data/docker/nginx/certs`
- `/data/docker/nginx/logs`
- `/data/docker/nginx/auth/users`
- `/data/docker/nginx/compose`
- `users.csv`
- `ports.txt`
- `manager.db`
- external Docker networks: `agent-net`, `manager-net`
- Nginx compose file, if missing
- `manager-web.conf`, if missing
- `config/openclaw-manager.env`, if missing

脚本会创建或初始化：

- `/data/docker/openclaw-public/users`
- `/data/docker/openclaw-public/deleted`
- `/data/docker/openclaw-public/logs`
- `/data/docker/nginx/conf`
- `/data/docker/nginx/certs`
- `/data/docker/nginx/logs`
- `/data/docker/nginx/auth/users`
- `/data/docker/nginx/compose`
- `users.csv`
- `ports.txt`
- `manager.db`
- 外部 Docker 网络：`agent-net`、`manager-net`
- Nginx compose 文件，如不存在
- `manager-web.conf`，如不存在
- `config/openclaw-manager.env`，如不存在

## What Bootstrap Does Not Do | Bootstrap 不会做什么

The bootstrap script does not:

- install Docker
- install system packages
- request or generate public TLS certificates
- create production Basic Auth users
- overwrite existing runtime files
- move or migrate Docker/containerd runtime data
- start Nginx
- start `manager-web`
- create OpenClaw user instances

脚本不会：

- 安装 Docker
- 安装系统软件包
- 申请或生成公网 TLS 证书
- 创建生产 Basic Auth 用户
- 覆盖已有运行文件
- 移动或迁移 Docker/containerd 运行时数据
- 启动 Nginx
- 启动 `manager-web`
- 创建 OpenClaw 用户实例

## Docker Runtime Data Paths | Docker 运行时数据路径

OpenClaw instances can consume significant Docker and containerd storage. On production hosts, keep both Docker and containerd runtime data on the data disk, not the small system root disk.

OpenClaw 实例会占用较多 Docker 和 containerd 存储。生产环境应将 Docker 和 containerd 运行时数据放在数据盘，而不是较小的系统根盘。

Expected paths:

推荐路径：

- Docker Root Dir: `/data/docker`
- containerd root: `/data/docker/containerd`
- containerd state: `/run/containerd`

`scripts/check_bootstrap_readiness.sh` checks these paths and prints warnings if Docker or containerd still points to defaults such as `/var/lib/docker` or `/var/lib/containerd`.

`scripts/check_bootstrap_readiness.sh` 会检查这些路径。如果 Docker 或 containerd 仍指向 `/var/lib/docker`、`/var/lib/containerd` 等默认路径，脚本会输出 warning。

The bootstrap script does not migrate existing runtime data automatically. Existing hosts should migrate Docker/containerd data manually during a maintenance window after reviewing current mounts, running containers, and disk usage.

bootstrap 脚本不会自动迁移已有运行时数据。已有主机应在维护窗口内人工确认当前挂载、运行容器和磁盘占用后，再手动迁移 Docker/containerd 数据。

If `/var/lib/containerd` still contains a large amount of data, migrate it before creating many instances.

如果 `/var/lib/containerd` 仍占用大量空间，应先完成迁移，再批量创建实例。

## Run Bootstrap | 执行初始化

Clone the repository and review the config example:

克隆仓库并检查配置示例：

```bash
git clone <repo-url> /data/docker/openclaw-manager
cd /data/docker/openclaw-manager
cp config/openclaw-manager.env.example config/openclaw-manager.env
vim config/openclaw-manager.env
```

Run the read-only readiness check:

先执行只读环境检查：

```bash
./scripts/check_bootstrap_readiness.sh
```

Review Docker/containerd path warnings before continuing. Do not ignore large `/var/lib/containerd` usage on production hosts.

继续前应检查 Docker/containerd 路径 warning。生产主机不要忽略 `/var/lib/containerd` 的大容量占用。

Run bootstrap:

执行初始化：

```bash
./scripts/bootstrap_runtime.sh
```

For local dry-run tests without Docker, use:

如需在无 Docker 的本地环境进行 dry-run 测试，可使用：

```bash
BOOTSTRAP_SKIP_DOCKER=1 ./scripts/bootstrap_runtime.sh
```

`BOOTSTRAP_SKIP_DOCKER=1` skips Docker dependency checks and Docker network creation. It still renders files and initializes local runtime data.

`BOOTSTRAP_SKIP_DOCKER=1` 会跳过 Docker 依赖检查和 Docker 网络创建，但仍会渲染文件并初始化本地运行数据。

## Clean Host Setup Flow | 全新主机操作流程

Recommended order:

建议顺序：

1. Install Ubuntu 22.04 LTS or Ubuntu 24.04 LTS.
2. Install `python3`, Docker Engine, Docker Compose plugin, and `apache2-utils`.
3. Ensure the current user can run `docker ps`.
4. Clone this repository to `/data/docker/openclaw-manager`.
5. Copy `config/openclaw-manager.env.example` to `config/openclaw-manager.env`.
6. Edit `config/openclaw-manager.env`, especially `PUBLIC_HOST`, paths, OpenClaw version, port range, and certificate paths.
7. Run `./scripts/check_bootstrap_readiness.sh` to see what is still missing.
8. Run `./scripts/bootstrap_runtime.sh` to create missing runtime scaffolding.
9. Place TLS certificate and key files.
10. Create the global manager Basic Auth user.
11. Start Nginx from `/data/docker/nginx/compose`.
12. Start manager services from `/data/docker/openclaw-manager/services`.
13. Verify Nginx can reach `openclaw-manager-web:8080`.
14. Run `./scripts/check_metadata_consistency.py`.
15. Create one test instance before creating real users.

中文步骤：

1. 安装 Ubuntu 22.04 LTS 或 Ubuntu 24.04 LTS。
2. 安装 `python3`、Docker Engine、Docker Compose plugin 和 `apache2-utils`。
3. 确认当前用户可以执行 `docker ps`。
4. 将本仓库克隆到 `/data/docker/openclaw-manager`。
5. 复制 `config/openclaw-manager.env.example` 为 `config/openclaw-manager.env`。
6. 编辑 `config/openclaw-manager.env`，重点检查 `PUBLIC_HOST`、路径、OpenClaw 版本、端口范围和证书路径。
7. 执行 `./scripts/check_bootstrap_readiness.sh` 查看仍缺少哪些内容。
8. 执行 `./scripts/bootstrap_runtime.sh` 创建缺失的运行骨架。
9. 放置 TLS 证书和 key。
10. 创建全局管理端 Basic Auth 用户。
11. 从 `/data/docker/nginx/compose` 启动 Nginx。
12. 从 `/data/docker/openclaw-manager/services` 启动管理端服务。
13. 验证 Nginx 可以访问 `openclaw-manager-web:8080`。
14. 执行 `./scripts/check_metadata_consistency.py`。
15. 先创建一个测试实例，再创建正式用户。

## Post-Bootstrap Steps | 初始化后的步骤

After bootstrap:

1. Review `config/openclaw-manager.env`.
2. Place TLS certificate and key files at the configured Nginx certificate paths.
3. Create the global manager Basic Auth user in the configured `.htpasswd` file.
4. Review `/data/docker/nginx/compose/docker-compose.yml`.
5. Start Nginx.
6. Start manager services.
7. Run the metadata consistency check.

初始化后：

1. 检查 `config/openclaw-manager.env`。
2. 将 TLS 证书和 key 放到配置的 Nginx 证书路径。
3. 在配置的 `.htpasswd` 文件中创建全局管理端 Basic Auth 用户。
4. 检查 `/data/docker/nginx/compose/docker-compose.yml`。
5. 启动 Nginx。
6. 启动管理端服务。
7. 执行元数据一致性检查。

Example commands:

示例命令：

```bash
cd /data/docker/nginx/compose
docker compose up -d
docker exec openclaw-nginx nginx -t
docker exec openclaw-nginx nginx -s reload

cd /data/docker/openclaw-manager/services
docker compose up -d --build

cd /data/docker/openclaw-manager
./scripts/check_metadata_consistency.py
```

## Notes | 注意事项

- Keep runtime secrets outside the repository.
- 运行时密钥应保留在仓库外。
- Do not commit production `config/openclaw-manager.env`.
- 不要提交生产 `config/openclaw-manager.env`。
- If the script uses `sudo` to create runtime files, it restores newly created file ownership to the current user by default.
- 如果脚本使用 `sudo` 创建运行文件，默认会把新建文件 owner 恢复为当前用户。
- Set `BOOTSTRAP_OWNER=<uid>:<gid>` if a different runtime owner is required.
- 如需指定不同运行文件 owner，可设置 `BOOTSTRAP_OWNER=<uid>:<gid>`。
