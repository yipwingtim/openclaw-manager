# EvoScientist Adapter

The adapter manages both newly provisioned and registered two-container instances.
It supports create, start, stop, restart, logs, retained deletion, restore, and
digest-pinned image updates.

Runtime lifecycle methods receive the instance metadata record rather than a
platform user id. The adapter uses `instances.runtime_identifier` as the main
container target and derives the proxy target by appending `-proxy`. The
optional `legacy_user_id` remains only for legacy data and Nginx configuration
paths.

## Runtime Contract

For legacy user ID `<user_id>`:

- Main container: `evoscientist_<user_id>`
- TCP proxy container: `evoscientist_<user_id>-proxy`
- User data: `OPENCLAW_PUBLIC_DIR/users/<user_id>/evoscientist-data`
- Workspace: `OPENCLAW_PUBLIC_DIR/users/<user_id>/workspace`
- Nginx config: `NGINX_USERS_CONF_DIR/<user_id>.conf`

The proxy shares the main container network namespace. Lifecycle ordering is therefore significant:

- Start and restart: main, then proxy.
- Stop: proxy, then main.

The dedicated ingress also joins `instance-auth-net` and requires the Manager
UIS session plus instance owner/member authorization. It does not attach the
EvoScientist application containers to `manager-net`.

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

After registration, deploy `manager-control`, `manager-executor`,
`manager-executor-api`, and `manager-admin-web`, then verify the instance list.
The Web UI exposes status, start, stop, and restart actions. OpenClaw-only
actions remain hidden.

## Images and updates

EvoScientist currently publishes `latest` from its main branch rather than a
stable release tag. Configure and record an immutable image digest:

```bash
EVOSCIENTIST_IMAGE=ghcr.io/evoscientist/evoscientist@sha256:<digest>
```

Before an update, pull the image on the host. The Web UI accepts the
`sha256:<digest>` value and refuses mutable tags. Failed updates recreate both
containers with the previous local image.
