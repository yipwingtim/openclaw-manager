# DeepSeek Harness instances

Administrators can provision Harness 0.2.2-rc.5 for an existing active campus-uis
identity in the single-instance creation form. Build the pinned runtime on the
executor's Docker host before provisioning. This downloads Node and npm packages
and creates a local image.

```sh
docker build -t openclaw-manager/deepseek-harness:0.2.2-rc.5 templates/deepseek-harness
```

The runtime runs as UID/GID 1000 on its own tenant network. Provisioning checks
that the image exists before creating data directories, then waits for the
separate gateway container to exchange its launch token with the Harness listener. The executor and Docker daemon must
share OPENCLAW_PUBLIC_DIR at the same absolute path. State (including model
credentials) and workspace files live under instances/deepseek-harness/<public_id>/
in state/ and workspace/. Retained deletion removes the container and preserves
both directories; restore uses the same image and directories.

The RC image should be retained for restores. Model proxy configuration remains
a separate integration. Users can configure their own providers in Harness.

## Failed provisioning

Duplicate instance IDs and duplicate names within an owner's product return a
conflict response. Failed Harness instances have a dedicated cleanup action. The admin instance list
returns the cleanup capability so the UI can display this action.
Cleanup tolerates an absent container, removes the failed metadata record, and
releases its instance ID. The UUID-based state and workspace directories remain
on disk for recovery; a new instance receives new directories. A Docker failure
other than an absent container prevents metadata cleanup.

## User access

The user portal links to `https://PUBLIC_HOST:30015/<instance-id>/`. Harness
uses the shared Manager HTTPS port and allocates no external instance port. Use the same
PUBLIC_HOST value as the IP or hostname in the browser URL.
An instance ID is a single path segment containing letters, digits, underscores
or hyphens. Manager route names are reserved. Each instance keeps a stable path
when the owner changes.

The Manager Nginx server includes generated `harness-paths/*.inc` fragments and
strips the instance prefix before forwarding. The gateway checks the expected
Host and Origin, calls the existing instance authorization service for every
HTTP request and WebSocket handshake, and permits only an active campus-uis
session belonging to the instance owner. Administrator roles and instance
membership alone do not grant Harness access. Anonymous GET requests return to
the Manager login flow with the instance ID as the return target.

The application and a read-only gateway run as separate containers. The application
never receives the Manager browser cookie. Only the gateway joins instance-auth-net.
The shared gateway-auth directory carries the application launch token; it is
mounted read-only in the gateway and is retained with instance data.
The gateway exchanges the process launch token server-side and forwards its
Harness cookie internally. Manager credentials and Harness cookies are never
forwarded to the browser as part of this exchange. The gateway requires
same-origin Origin headers for writes and upgrades, drops incoming authorization
and Manager identity headers, and disables response caching. Browser storage is
still shared between paths on the same origin; path routing does not isolate
client-side storage.

## Enable existing instances

Deploy the updated Manager control, executor, executor API, admin web and
instance-auth-proxy services through `scripts/deploy_services.sh`. This refreshes
the Manager Nginx include. Rebuild the Harness image using the command above.

Then select **接入／刷新入口** for each existing active or stopped Harness instance.
Legacy containers are stopped and renamed while a new container starts with the
same state and workspace mounts. A failed replacement restores the previous
container. Successful completion persists the relative access URL and enables
the user portal button. Stopped instances remain stopped and must be started
before users open them.

This action briefly interrupts the target instance. Keep the previous image
and back up instance state before rollout. Reverting only the Manager changes
does not restore an upgraded runtime. Restore its previous image and network
configuration as part of rollback.

## Validation

```sh
node --test tests/test_harness_gateway.mjs
python3 -m unittest tests.test_deepseek_harness_adapter tests.test_manager_control_api
```

The Node test binds local ports 3080 and 3081. Validate real campus login, logout,
a second campus user's denial, and a saved session through the production HTTPS
entry after rollout.
