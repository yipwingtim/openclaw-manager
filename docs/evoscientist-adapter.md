# EvoScientist Existing Instance Adapter

The first EvoScientist adapter phase manages existing two-container instances. It does not create, delete, restore, or update EvoScientist versions.

## Runtime Contract

For user ID `<user_id>`:

- Main container: `evoscientist_<user_id>`
- TCP proxy container: `evoscientist_<user_id>-proxy`
- User data: `OPENCLAW_PUBLIC_DIR/users/<user_id>/evoscientist-data`
- Workspace: `OPENCLAW_PUBLIC_DIR/users/<user_id>/workspace`
- Nginx config: `NGINX_USERS_CONF_DIR/<user_id>.conf`

The proxy shares the main container network namespace. Lifecycle ordering is therefore significant:

- Start and restart: main, then proxy.
- Stop: proxy, then main.

## Register An Existing Instance

Load the manager environment and register the existing instance:

```bash
source config/openclaw-manager.env

sudo -E python3 scripts/metadata_cli.py register-instance \
  --user-id evosci-test001 \
  --product evoscientist \
  --container-name evoscientist_evosci-test001
```

The command detects the external port and Basic Auth state from the user Nginx configuration, records the port allocation, and creates an auditable `register_instance` operation.

After registration, restart manager-web and verify the instance list. The Web UI exposes status, start, stop, and restart actions. OpenClaw-only actions remain hidden.

