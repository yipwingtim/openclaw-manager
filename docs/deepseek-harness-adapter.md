# DeepSeek Harness instances

Administrators can provision Harness 0.2.2-rc.5 for an existing active campus-uis
identity in the single-instance creation form. Build the pinned runtime on the
executor's Docker host before provisioning. This downloads Node and npm packages
and creates a local image.

```sh
docker build -t openclaw-manager/deepseek-harness:0.2.2-rc.5 templates/deepseek-harness
```

The runtime runs as UID/GID 1000 with networking disabled. Provisioning checks that the image exists before creating data directories, then
waits for its authenticated loopback listener. The executor and Docker daemon must
share OPENCLAW_PUBLIC_DIR at the same absolute path. State (including model
credentials) and workspace files live under instances/deepseek-harness/<public_id>/
in state/ and workspace/. Retained deletion removes the container and preserves
both directories; restore uses the same image and directories.

This lifecycle-only release has no public endpoint, model proxy provisioning or
interactive sign-in. Public path routing and UIS-to-Harness session exchange
require a subsequent change. The RC image should be retained for restores.

## Failed provisioning

Duplicate instance IDs and duplicate names within an owner's product return a
conflict response. Failed Harness instances have a dedicated cleanup action. The admin instance list
returns the cleanup capability so the UI can display this action.
Cleanup tolerates an absent container, removes the failed metadata record, and
releases its instance ID. The UUID-based state and workspace directories remain
on disk for recovery; a new instance receives new directories. A Docker failure
other than an absent container prevents metadata cleanup.

## User access

Provisioned instances currently have no access URL. The user portal therefore
has no Enter application button. UIS authorization, path routing and the Harness
session exchange must be implemented before enabling that entry point.
