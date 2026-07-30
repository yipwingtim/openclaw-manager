# Hermes Registered Instance Adapter

The Hermes MVP manages existing deployments that use the current official
single-container image. It does not create, delete, restore, or upgrade Hermes.

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

Registration writes the instance, endpoint, allocated port, audit record,
Nginx server configuration, port mapping, and external network attachment. A
configuration or Nginx validation failure restores the previous files and
rolls back the metadata transaction.

The legacy v0.12 two-container `hermes-main` plus `hermes-dashboard` layout is
not supported by this adapter.
