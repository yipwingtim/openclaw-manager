#!/usr/bin/env python3

import argparse
import fcntl
import ipaddress
import json
import subprocess
import sys
from pathlib import Path


TENANT_NETWORK_LABEL_KEY = "com.openclaw.tenant-network"


class AllocationError(RuntimeError):
    pass


def run(command, check=True):
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise AllocationError(f"{' '.join(command)}: {detail}")
    return result


def parse_ipv4_network(value, label):
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise AllocationError(f"invalid {label} CIDR {value!r}: {exc}") from exc
    if network.version != 4:
        raise AllocationError(f"{label} must be an IPv4 CIDR: {value}")
    return network


def load_host_routes():
    result = run(["ip", "-j", "-4", "route", "show", "table", "main"])
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AllocationError(f"could not parse host routes: {exc}") from exc

    routes = []
    for entry in entries:
        destination = entry.get("dst")
        device = entry.get("dev", "")
        if not destination or destination == "default":
            continue
        if device == "docker0" or device.startswith("br-"):
            continue
        try:
            route = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        if route.version == 4:
            routes.append((route, device or "unknown"))
    return routes


def inspect_network(network):
    result = run(["docker", "network", "inspect", network], check=False)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AllocationError(f"could not parse Docker network {network}: {exc}") from exc
    if not data:
        return None
    return data[0]


def network_subnets(data):
    subnets = []
    for config in data.get("IPAM", {}).get("Config", []) or []:
        value = config.get("Subnet")
        if not value:
            continue
        try:
            subnet = ipaddress.ip_network(value, strict=False)
        except ValueError:
            continue
        if subnet.version == 4:
            subnets.append(subnet)
    return subnets


def load_docker_subnets():
    result = run(["docker", "network", "ls", "-q"])
    subnets = []
    for network_id in result.stdout.splitlines():
        data = inspect_network(network_id.strip())
        if data:
            subnets.extend(network_subnets(data))
    return subnets


def validate_pool(pool, subnet_prefix, host_routes):
    if subnet_prefix < pool.prefixlen:
        raise AllocationError(
            f"tenant subnet prefix /{subnet_prefix} is larger than pool {pool}"
        )
    if subnet_prefix > 30:
        raise AllocationError("tenant subnet prefix must be between the pool prefix and /30")
    for route, device in host_routes:
        if pool.overlaps(route):
            raise AllocationError(
                f"tenant subnet pool {pool} overlaps host route {route} on {device}"
            )


def validate_existing(network, data, pool, subnet_prefix, excluded, host_routes):
    labels = data.get("Labels") or {}
    if labels.get(TENANT_NETWORK_LABEL_KEY) != network:
        raise AllocationError(f"existing tenant network {network} is not owned by OpenClaw")
    subnets = network_subnets(data)
    if len(subnets) != 1:
        raise AllocationError(f"existing tenant network {network} must have one IPv4 subnet")
    subnet = subnets[0]
    if not subnet.subnet_of(pool):
        raise AllocationError(
            f"existing tenant network {network} subnet {subnet} is outside configured tenant pool {pool}"
        )
    if subnet.prefixlen != subnet_prefix:
        raise AllocationError(
            f"existing tenant network {network} uses {subnet}; expected /{subnet_prefix}"
        )
    for blocked in excluded:
        if subnet.overlaps(blocked):
            raise AllocationError(
                f"existing tenant network {network} subnet {subnet} overlaps excluded CIDR {blocked}"
            )
    for route, device in host_routes:
        if subnet.overlaps(route):
            raise AllocationError(
                f"existing tenant network {network} subnet {subnet} overlaps host route {route} on {device}"
            )
    return subnet


def build_plan(args):
    pool = parse_ipv4_network(args.pool, "tenant subnet pool")
    excluded = [parse_ipv4_network(value, "excluded") for value in args.exclude]
    if len(args.network) != len(set(args.network)):
        raise AllocationError("tenant network list contains duplicate names")

    host_routes = load_host_routes()
    validate_pool(pool, args.subnet_prefix, host_routes)
    existing_by_name = {
        network: inspect_network(network)
        for network in args.network
    }
    existing_subnets = {}
    for network, existing in existing_by_name.items():
        if existing:
            existing_subnets[network] = validate_existing(
                network, existing, pool, args.subnet_prefix, excluded, host_routes
            )

    used = load_docker_subnets()
    planned = []
    reserved = []

    for network in args.network:
        if network in existing_subnets:
            planned.append((network, existing_subnets[network], "existing"))
            continue

        for candidate in pool.subnets(new_prefix=args.subnet_prefix):
            if any(candidate.overlaps(blocked) for blocked in excluded):
                continue
            if any(candidate.overlaps(subnet) for subnet in used):
                continue
            if any(candidate.overlaps(subnet) for subnet in reserved):
                continue
            planned.append((network, candidate, "create"))
            reserved.append(candidate)
            break
        else:
            needed = sum(1 for _, _, status in planned if status == "create") + 1
            raise AllocationError(
                f"tenant subnet pool {pool} does not have capacity for all requested "
                f"networks; failed while planning {network} (needed at least {needed} new subnets)"
            )

    return planned


def print_plan(plan):
    for network, subnet, status in plan:
        print(f"{network}\t{subnet}\t{status}")


def plan_networks(args):
    print_plan(build_plan(args))


def prepare_networks(args):
    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    created = []

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        plan = build_plan(args)
        try:
            for network, subnet, status in plan:
                if status == "existing":
                    continue
                run(
                    [
                        "docker", "network", "create",
                        "--driver", "bridge",
                        "--subnet", str(subnet),
                        "--label", f"{TENANT_NETWORK_LABEL_KEY}={network}",
                        network,
                    ]
                )
                created.append(network)
        except AllocationError as exc:
            rollback_errors = []
            for network in reversed(created):
                result = run(["docker", "network", "rm", network], check=False)
                if result.returncode != 0:
                    rollback_errors.append(network)
            if rollback_errors:
                raise AllocationError(
                    f"{exc}; rollback failed for: {', '.join(rollback_errors)}"
                ) from exc
            raise

    print_plan(plan)


def ensure_network(args):
    prepare_networks(args)


def add_common_arguments(parser):
    parser.add_argument("--network", action="append", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--subnet-prefix", type=int, default=28)
    parser.add_argument("--exclude", action="append", default=[])


def build_parser():
    parser = argparse.ArgumentParser(description="Manage isolated tenant Docker networks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    add_common_arguments(plan)
    plan.set_defaults(handler=plan_networks)

    prepare = subparsers.add_parser("prepare")
    add_common_arguments(prepare)
    prepare.add_argument("--lock-file", required=True)
    prepare.set_defaults(handler=prepare_networks)

    ensure = subparsers.add_parser("ensure")
    add_common_arguments(ensure)
    ensure.add_argument("--lock-file", required=True)
    ensure.set_defaults(handler=ensure_network)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except AllocationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
