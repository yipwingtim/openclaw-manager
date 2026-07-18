# Tenant network address pool

OpenClaw tenant networks must use an operator-selected IPv4 pool. Docker's
automatic address allocation can select a subnet that overlaps a bastion, VPN,
cloud, or LAN route and break host connectivity.

## Select a pool

Collect the host routes and every Docker subnet:

```bash
ip -4 route show table main
docker network inspect $(docker network ls -q) \
  --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' | sort
```

Also collect networks that are reached through the default route and therefore
do not appear as specific host routes, including bastion and VPN client CIDRs.

Choose a dedicated RFC1918 pool that is reserved for OpenClaw and does not
overlap any of those networks. There is intentionally no default because no
private CIDR is universally safe across production environments.

Configure `config/openclaw-manager.env`:

```bash
OPENCLAW_TENANT_SUBNET_POOL=<dedicated-pool-cidr>
OPENCLAW_TENANT_SUBNET_PREFIX=28
OPENCLAW_TENANT_NETWORK_EXCLUDE_CIDRS=<bastion-cidr>,<vpn-cidr>,<other-routed-cidr>
OPENCLAW_TENANT_NETWORK_LOCK_FILE=/data/docker/openclaw-public/tenant-network.lock
```

## Per-tenant network identity

Each tenant receives a dedicated external Docker bridge network. Its name is
`openclaw-user-<sha256>`, where the digest is calculated from the original
tenant ID. This is deliberate: IDs such as `foo_bar` and `foo-bar` must not
collapse onto the same network after service-name normalization.

Networks are labelled with their owning network name. The allocator validates
that label before reusing an existing network, and cleanup only removes an
unused network with the matching ownership label. A same-named unowned Docker
network is rejected rather than adopted.

A `/16` pool divided into `/28` tenant networks supports 4096 tenant networks.
Each `/28` has enough addresses for the tenant container and shared proxy
containers while keeping host route growth bounded.

## Safety behavior

Before creating a tenant network, the allocator:

- serializes allocation with a file lock;
- rejects a pool that overlaps a non-Docker host route;
- skips explicitly excluded and already allocated CIDRs;
- validates all requested tenant networks and pool capacity as one batch;
- creates the bridge with an explicit `/28` subnet;
- rejects existing tenant networks outside the configured pool;
- removes networks created by the current batch if a later create fails.

Creation, restore, and migration stop with an error rather than asking Docker to
select an automatic subnet when the pool is missing or unsafe.

The normal migration repeats preflight under the allocation lock. It creates all
missing tenant networks only after every requested tenant has a valid plan, and
does not modify Compose files or containers until network preparation succeeds.

## Existing automatic networks

Inspect existing tenant networks before migration:

```bash
docker network inspect $(docker network ls --filter name=openclaw-user -q) \
  --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' | sort
```

Networks created by older versions may use the previous normalized naming
scheme. The migration script creates the new hashed network and updates each
tenant Compose file; it does not treat an unowned same-name network as safe.

If an existing tenant subnet is outside the configured pool, disconnect its
containers, remove that network, and rerun the migration for that tenant. Do not
remove a network until `docker network inspect <network>` confirms which
containers are attached.

After migration, verify host routing and runtime isolation:

## Migration dry-run

Run a read-only preflight before the production change:

```bash
./scripts/migrate_tenant_networks.sh --dry-run
```

To inspect selected tenants only:

```bash
./scripts/migrate_tenant_networks.sh --dry-run alice bob
```

The output includes each tenant's current state, network name, planned subnet,
and whether the network already exists or would be created. Dry-run validates
all Compose files, routes, exclusions, existing Docker subnets, and total pool
capacity. It does not write Compose files, create networks, connect containers,
or change running, stopped, or paused container state.

Only run the migration without `--dry-run` after reviewing the complete plan.


```bash
ip -4 route show table main
./scripts/check_runtime_security.sh
```
