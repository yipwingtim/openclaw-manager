# Hermes Instance Adapter

The Hermes MVP creates deployments using the official `v2026.7.20` (Hermes
Agent v0.19.0) single-container image. Managed instances can later be upgraded
to another explicitly selected `nousresearch/hermes-agent` tag. It does not
support the legacy two-container topology.

## Runtime and ingress contract

- One metadata instance maps to one Hermes container through the server-side
  `runtime_identifier`.
- The container exposes the built-in Dashboard on port `9119` and belongs to
  exactly one dedicated Docker network.
- `openclaw-nginx` publishes one external TLS port per instance and joins that
  network persistently through its Compose configuration.
- Nginx requires Manager UIS instance authorization before routing. New Hermes
  instances use the custom `campus-uis-bridge` `DashboardAuthProvider` as the
  second layer and establish an official Hermes Session without another password.
  The
  OpenAI-compatible API on `8642` uses a separate `API_SERVER_KEY`.
- Creation runs `gateway run` with `HERMES_DASHBOARD=1`, mounts a dedicated
  instance directory at `/opt/data`, and publishes no host ports directly.
- Each new instance receives a unique Bridge `client_id` and `client_secret`.
  The secret exists only in the instance's mode `0600` `.env`; Manager metadata
  stores only its scrypt verifier. The Provider is installed under
  `/opt/data/plugins/campus-uis-bridge` and enabled in `config.yaml` before startup.
  Its registered HTTPS callback is pinned in `HERMES_UIS_BRIDGE_REDIRECT_URI`;
  values dynamically inferred by Hermes from reverse-proxy requests are ignored.
- Runtime dependency lazy installation is disabled in `config.yaml` so a
  missing optional package cannot block agent initialization. Optional
  dependencies must be included in the image before enabling their features.
- The existing administrator model-provider batch creates an instance-scoped
  Model Proxy token and allowlist, joins the proxy to the Hermes tenant
  network, and writes the proxy URL and token through `hermes config set`.
  Upstream API keys remain only in the shared Model Proxy service.
- Delete moves `/opt/data` into `deleted/hermes/<instance UUID>` together with
  a private manifest containing the image, tenant network, and previous running
  state. Restore recreates the same deployment, port, ingress, and state.
- Version updates follow the OpenClaw pre-pull rule: the target image must
  already exist locally (`docker pull nousresearch/hermes-agent:<version>`).
  A failed recreate automatically restores the previous image and state.

Registration writes the instance, endpoint, allocated port, audit record,
Nginx server configuration, port mapping, and external network attachment. A
configuration or Nginx validation failure restores the previous files and
rolls back the metadata transaction.

## Platform integration

Hermes instances use the shared `/me` user portal and instance detail route.
Authorized users can view the instance status, access URL, and recent logs, then
enter the Hermes Dashboard through its registered endpoint. OpenClaw-only device,
file, Skill, and WeChat actions are not exposed for Hermes because they are not
declared by the Hermes capability set.

Administrators manage Hermes instances from the global `/admin/*` portal. The
Control and Executor services resolve the instance by its public ID and pass the
instance record to the Hermes Adapter; callers cannot provide an arbitrary
container name or data path.

The legacy v0.12 two-container `hermes-main` plus `hermes-dashboard` layout is
not supported by this adapter and must be replaced before using these actions.

## Existing instance migration

Deploying Manager services does not alter or restart existing Hermes instances.
Preview one exact instance first:

```bash
python3 scripts/migrate_hermes_uis_auth.py \
  --db /data/docker/openclaw-public/manager.db \
  --instance <instance-public-uuid> \
  --issuer https://<manager-host>:30015/auth/hermes
```

The `--apply` operation must run as root. Before changing files it verifies
that privilege is available, then copies the Provider with the Hermes data
directory owner and explicit modes (`0750` for directories and `0640` for
files). Template ownership and inherited ACLs are never trusted.

After reviewing the plan, repeat with `--apply`. Apply removes the legacy Basic
Auth entries, installs/enables the Provider, creates one client, and restarts only
the selected container. A failure restores the original files, deletes the new
client, and attempts to restart the original configuration.
