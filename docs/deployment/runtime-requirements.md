# Runtime Requirements / 运行环境要求

OpenClaw Manager runtime scripts support Python 3.6 and newer Python 3 releases, including Python 3.10 and 3.11. The scripts use the Python standard library and avoid version-specific `pathlib`/SQLite APIs.

OpenClaw Manager 运行脚本支持 Python 3.6 及更新的 Python 3 版本，包括 Python 3.10 和 3.11。脚本使用 Python 标准库，并避免依赖特定版本的 `pathlib`/SQLite 接口。

This host-script compatibility does not include direct PostgreSQL metadata
writes on Python 3.6. PostgreSQL uses psycopg 3, which requires a newer Python;
on Anolis OS 8.x, perform instance lifecycle operations through the
containerized Manager control plane running Python 3.12. SQLite host operations
remain supported on Python 3.6.

宿主脚本兼容性不包括在 Python 3.6 中直接写入 PostgreSQL 元数据。PostgreSQL
使用 psycopg 3，需要更新版本的 Python；在 Anolis OS 8.x 上，应通过运行
Python 3.12 的容器化 Manager 控制面执行实例生命周期操作。Python 3.6
宿主仍支持 SQLite 操作。

## Required software / 必需软件

- Bash
- Python 3 (`python3`)
- Docker Engine
- Docker Compose plugin (`docker compose`)
- systemd for service management on Linux hosts

## Supported hosts / 支持的主机

- Ubuntu with a supported Python 3 installation
- Anolis OS 8.x with the system Python 3.6

Do not replace the distribution's `/usr/bin/python3` solely for this project. If a newer Python is needed, install it alongside the system interpreter and invoke it explicitly or through a virtual environment.

不要仅因本项目而替换发行版的 `/usr/bin/python3`。如果需要更新版本的 Python，请与系统解释器并行安装，并通过显式命令或虚拟环境使用。

## Bootstrap permissions / 初始化权限

The bootstrap user must be able to run Docker commands without an interactive password prompt, typically by belonging to the `docker` group. The script may use `sudo` to create root-owned runtime directories when needed.

执行 bootstrap 的用户必须能够无需交互输入密码运行 Docker 命令，通常应加入 `docker` 组。必要时脚本可使用 `sudo` 创建 root 所有的运行目录。

Before deployment, run `scripts/check_bootstrap_readiness.sh`. It checks the
effective runtime directories and lock-file parents, required network and
service-token settings, PostgreSQL configuration, and Nginx state. Run it a
second time after the one-time Nginx startup; the initial missing Nginx
container is reported as a warning before that startup.

部署前执行 `scripts/check_bootstrap_readiness.sh`。该脚本检查实际运行目录和
锁文件父目录、必需的网络与服务令牌配置、PostgreSQL 配置以及 Nginx 状态。
完成一次性 Nginx 启动后应再次执行；启动前缺少 Nginx 容器只会报告警告。
