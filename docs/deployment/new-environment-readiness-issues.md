# New environment readiness issues

This document records deployment failures caused by incomplete host or runtime
preparation. Python and operating-system compatibility defects are tracked in
`anolis-python36-compatibility.md` and are intentionally excluded here.

## Docker registry access

- Symptom: image builds timed out while fetching anonymous Docker Hub tokens.
- Cause: Docker daemon registry traffic depended on a local HTTP proxy, and
  connectivity was intermittent even though the daemon inherited proxy
  variables.
- Resolution: verify the proxy from the host, restart Docker after confirming
  its service environment, and pre-pull required base images before deployment.

## Missing Nginx runtime

- Symptom: Manager services became healthy, but deployment stopped because
  container `openclaw-nginx` did not exist.
- Cause: Nginx had not received its documented one-time initial Compose start.
- Resolution: validate `/data/docker/nginx/compose/docker-compose.yml`, perform
  the initial Nginx Compose start, then use `scripts/deploy_services.sh` for
  Manager deployments.

## Parent directory traversal permission

- Symptom: the operator could not re-enter `/data/docker/agent-manager` after
  leaving the directory.
- Cause: `/data/docker` was mode `710` and the operator was not in its group.
- Resolution: restore the intended traversal-only mode `711` without changing
  ownership or granting directory listing access.

## Empty internal service tokens

- Symptom: `manager-control` returned HTTP 503 with
  `service_tokens_configured=false`.
- Cause: Manager control service tokens in the runtime environment file were
  empty.
- Resolution: generate distinct random values for the user-web, admin-web,
  executor, and instance-auth service tokens, then redeploy through the project
  deployment script.

## Nginx runtime security prerequisites

- Symptom: runtime security checks reported a missing internal token header and
  that Nginx was not attached to `instance-auth-net`.
- Cause: `OPENCLAW_INTERNAL_TOKEN` was empty and the external Nginx Compose file
  did not declare `instance-auth-net`.
- Resolution: set a random internal token, persist the network in the Nginx
  Compose file, apply that Compose change, and rerun the Manager deployment.

## Tenant network pool

- Symptom: instance creation stopped because `OPENCLAW_TENANT_SUBNET_POOL` was
  empty.
- Cause: no operator-selected, non-overlapping RFC1918 pool had been configured.
- Resolution: inspect host routes and Docker subnets, reserve a non-overlapping
  pool, and configure the pool and per-tenant prefix before creating instances.

## Runtime lock-file paths

- Symptom: instance creation failed while creating tenant-network or port lock
  files under a root-owned runtime directory.
- Cause: lock files defaulted to directory roots that the deployment operator
  could traverse or read but could not write.
- Resolution: configure `OPENCLAW_TENANT_NETWORK_LOCK_FILE` and `PORT_LOCK_FILE`
  under an existing operator-owned runtime subdirectory. Do not loosen the
  permissions of the entire public data directory.
- Prevention: rerun `scripts/check_bootstrap_readiness.sh`; it now validates
  the parent directories of the effective lock-file paths.

## PostgreSQL writes from a Python 3.6 host

- Symptom: instance runtime creation succeeded, but metadata registration
  ended with `psycopg is required for postgres backend` and PostgreSQL had no
  instance row.
- Cause: Anolis OS 8.6 provides Python 3.6, while the project's PostgreSQL
  driver is psycopg 3 and runs in the Python 3.12 Manager control container.
- Resolution: use the Manager Admin UI and containerized control-plane flow for
  instance lifecycle operations. If a runtime was already created, verify its
  actual files, container, ingress, and port before recovering its metadata in
  one PostgreSQL transaction.
- Prevention: treat the readiness warning about a missing host `psycopg` as a
  prohibition on direct host lifecycle scripts, not as a request to replace
  the distribution Python.

## Readiness checks before deployment

Run `scripts/check_bootstrap_readiness.sh` after editing the environment file
and again after the first Nginx start. The check validates the effective tenant
network pool, internal service tokens, manager service tokens, lock-file parent
directories, and Nginx container state without printing secret values. A
missing initial Nginx container is a warning before its documented one-time
startup; it should be running before Manager services are deployed.

## Instance Basic Auth input

- Symptom: `htpasswd` stopped instance creation with a password verification
  error.
- Cause: the two interactive password entries did not match. Disabling workspace
  Basic Auth does not disable the separate instance administration credential.
- Resolution: confirm cleanup completed, restore the unused port cursor if
  necessary, and retry with matching experimental credentials.
