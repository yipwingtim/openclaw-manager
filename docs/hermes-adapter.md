# Hermes Instance Adapter

The Hermes MVP manages and creates deployments using the official
`v2026.7.20` (Hermes Agent v0.19.0) single-container image. It does not support
the legacy two-container topology, restore, or upgrade.

## Runtime and ingress contract

- One metadata instance maps to one Hermes container through the server-side
  `runtime_identifier`.
- The container exposes the built-in Dashboard on port `9119` and belongs to
  exactly one dedicated Docker network.
- `openclaw-nginx` publishes one external TLS port per instance and joins that
  network persistently through its Compose configuration.
- Nginx provides TLS and routing only. Hermes Dashboard authentication remains
  responsible for username/password, Nous OAuth, or self-hosted OIDC. The
  OpenAI-compatible API on `8642` uses a separate `API_SERVER_KEY`.
- Creation runs `gateway run` with `HERMES_DASHBOARD=1`, mounts a dedicated
  instance directory at `/opt/data`, and publishes no host ports directly.
- Dashboard Basic Auth is mandatory for creation. Only the official Hermes
  scrypt password hash and a random session secret are persisted in `.env`;
  the plaintext password is consumed from the one-shot provisioning secret.

Registration writes the instance, endpoint, allocated port, audit record,
Nginx server configuration, port mapping, and external network attachment. A
configuration or Nginx validation failure restores the previous files and
rolls back the metadata transaction.

The legacy v0.12 two-container `hermes-main` plus `hermes-dashboard` layout is
not supported by this adapter and must be replaced before using these actions.
