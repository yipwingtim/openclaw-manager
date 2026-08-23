#!/usr/bin/env python3

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "manager-web"))
from auth_providers import provider_health  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Check Manager authentication provider readiness.")
    parser.add_argument("--probe", action="store_true", help="Probe configured external HTTPS endpoints.")
    args = parser.parse_args()
    result = provider_health(os.environ, probe=args.probe)
    if result["status"] == "ok":
        print(f"[OK] Manager auth provider={result['provider']} mode={result['mode']}")
    else:
        print(f"[ERROR] Manager auth provider={result['provider']}: {result.get('error', 'health check failed')}")
    for check in result["checks"]:
        detail = check.get("detail") or check.get("http_status") or ""
        print(f"[{check['status'].upper()}] {check['name']}{': ' + str(detail) if detail else ''}")
    emergency = "ready" if result["emergency_ready"] else "not ready"
    print(f"[INFO] Local login={'enabled' if result['local_login_enabled'] else 'disabled'} emergency={emergency}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
