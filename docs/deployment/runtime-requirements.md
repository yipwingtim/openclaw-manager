# Runtime Requirements / 运行环境要求

OpenClaw Manager runtime scripts support Python 3.6 and newer Python 3 releases, including Python 3.10 and 3.11. The scripts use the Python standard library and avoid version-specific `pathlib`/SQLite APIs.

OpenClaw Manager 运行脚本支持 Python 3.6 及更新的 Python 3 版本，包括 Python 3.10 和 3.11。脚本使用 Python 标准库，并避免依赖特定版本的 `pathlib`/SQLite 接口。

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
