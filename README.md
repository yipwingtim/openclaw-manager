<h1 align="center">OpenClaw Manager</h1>
<p align="center">A self-hosted control plane for isolated, multi-user AI agent instances.</p>
<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>

> [!IMPORTANT]
> OpenClaw Manager is an independent community project. It is not affiliated with or endorsed by the upstream OpenClaw project.

## Overview

OpenClaw Manager provisions and operates isolated AI agent instances without modifying the upstream application. Its metadata model separates platform users, authentication identities, and instances, and allows one platform user to own multiple product instances while preserving compatibility with existing OpenClaw deployments.

OpenClaw, Hermes, and EvoScientist are supported managed products. Product
adapters expose only the capabilities each runtime supports, while all runtime
operations resolve a server-side instance record. See the [Hermes adapter](docs/hermes-adapter.md)
and [EvoScientist adapter](docs/evoscientist-adapter.md) for product-specific
contracts.

## Highlights

- **Per-user isolation:** independent containers, workspaces, configuration, and runtime state.
- **Separated identity model:** platform users, login identities, and managed instances use independent IDs and relationships.
- **Selectable manager authentication:** use either Nginx Basic Auth or Local login; multiple identities may map to the same platform user, but only one provider is active at a time.
- **Lifecycle management:** create, start, stop, restart, upgrade, recycle, and restore instances.
- **Web administration:** manage users, status, authentication, skills, and common operations.
- **Stable reverse proxying:** product-aware HTTPS ingress through Nginx; OpenClaw path URLs and legacy per-instance ports coexist.
- **Layered access control:** Nginx Basic Auth, application tokens, and device approval.
- **Recoverable deletion:** deleted data is moved to a recycle directory.
- **Metadata visibility:** SQLite-backed records with runtime consistency checks.
- **Usage effectiveness:** read-only Activity snapshots with product data dictionaries and an administrator overview.
- **Extensible products:** capability-aware adapters for additional agent runtimes.

## Architecture

```text
Users  -> Nginx -> manager-user-web  --+
Admins -> Nginx -> manager-admin-web --+-> manager-control -> manager-executor
                                       |                         |
                                       +-> manager-executor-api -+-> product adapters
                                                                 +-> Docker / Nginx / host data

OpenClaw path or per-instance HTTPS port -> Nginx -> per-tenant network -> OpenClaw / Hermes / EvoScientist

Metadata: manager.db + transitional users.csv / ports.txt
Runtime:  /data/docker/openclaw-public + /data/docker/nginx
```

The user and global admin portals run as unprivileged services. Structured
actions pass through Control and Executor before an Adapter performs privileged
runtime work. The legacy `manager-web` container remains only as a temporary
rollback target; current Nginx templates route global and per-instance manager
traffic to the split Web services.
For the longer-term design, read [Agent Hosting Platform Architecture](docs/architecture/agent-hosting-platform.md).

## Requirements

- Ubuntu 22.04 LTS or Ubuntu 24.04 LTS
- Docker Engine and the Docker Compose plugin
- Bash and Python 3
- `apache2-utils`, `acl`, `setfacl`, `flock`, and standard GNU utilities

Other Linux distributions may work but are not currently validated.

## Quick Start

```bash
git clone https://github.com/yipwingtim/openclaw-manager.git /data/docker/openclaw-manager
cd /data/docker/openclaw-manager
cp config/openclaw-manager.env.example config/openclaw-manager.env
vim config/openclaw-manager.env

./scripts/check_bootstrap_readiness.sh
./scripts/bootstrap_runtime.sh
```

Bootstrap does not install Docker, issue TLS certificates, start services, or create production credentials. Read [Fresh Environment Bootstrap](docs/deployment/bootstrap.md) before production use.

```bash
./scripts/create_user.sh <user_id>
sudo -E python3 scripts/check_metadata_consistency.py
```

## Core Operations

```bash
./scripts/list_users.sh
./scripts/update_instance_version.sh <user_id> <version>
./scripts/delete_user.sh <user_id>
./scripts/restore_user.sh <user_id>
```

Most day-to-day actions are also available in the manager web interface. Product-specific actions appear only when the selected adapter supports them.

## Runtime Layout

```text
/data/docker/openclaw-public/
├── users/          # active instance data
├── deleted/        # recyclable instance data
├── manager.db      # structured metadata
├── users.csv       # transitional user records
├── ports.txt       # transitional port allocation state
└── logs/           # script logs

/data/docker/nginx/
├── compose/        # Nginx Compose project
├── conf/           # active and disabled user configs
├── certs/          # TLS certificate material
├── auth/           # Basic Auth data
└── logs/           # Nginx logs
```

Runtime paths are configurable in `config/openclaw-manager.env` and intentionally live outside the Git repository.

## Documentation

- [Fresh Environment Bootstrap](docs/deployment/bootstrap.md)
- [Runtime Security Checks](docs/deployment/runtime-security-checks.md)
- [User Self-Service Panel](docs/user-self-service-panel.md)
- [EvoScientist Adapter](docs/evoscientist-adapter.md)
- [Hermes Adapter](docs/hermes-adapter.md)
- [Activity Adapter statistics](docs/activity-adapters/README.md)
- [Model Proxy Deployment](docs/deployment/model-proxy.md)
- [Manager Web Authentication](docs/deployment/local-auth.md)
- [User, Identity, and Instance Migration](docs/architecture/user-identity-instance-migration.md)
- [Metadata Storage Plan](docs/architecture/metadata-storage-plan.md)
- [Metadata Data Dictionary](docs/architecture/metadata-data-dictionary.md)
- [Web Service Split](docs/architecture/web-service-split.md)
- [Roadmap](docs/architecture/roadmap.md)

## Security

OpenClaw Manager coordinates privileged container and reverse-proxy operations. Review authentication, Docker socket access, filesystem permissions, and network boundaries before production deployment. Never commit production credentials, API keys, certificates, `.htpasswd` files, or runtime data.

Use [Runtime Security Checks](docs/deployment/runtime-security-checks.md) as the operational baseline.

## Project Status

This project is under active development and is best suited to controlled self-hosted environments. Issues and focused pull requests are welcome; include the relevant action, container status, and sanitized logs when reporting a problem.

## License

Licensed under the [Apache License 2.0](LICENSE). Copyright 2026 yipwingtim and OpenClaw Manager contributors.
