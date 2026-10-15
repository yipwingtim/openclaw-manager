#!/bin/bash

port_is_available() {
  if [ -n "${PORT_AVAILABILITY_COMMAND:-}" ]; then
    "$PORT_AVAILABILITY_COMMAND" "$1"
    return
  fi

  python3 - "$1" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

try:
    sock.bind(("0.0.0.0", port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()

raise SystemExit(0)
PY
}

allocate_port() {
  local port_file="$1"
  local port_start="$2"
  local port_end="$3"
  local lock_file="${PORT_LOCK_FILE:-${port_file}.lock}"
  local port
  local next_port
  local released_ports
  local metadata_db_file="${METADATA_DB_FILE:-${OPENCLAW_PUBLIC_DIR:-}/manager.db}"
  local occupied_ports

  mkdir -p "$(dirname "$port_file")"
  touch "$lock_file"

  (
    flock -x 9

    if [ ! -f "$port_file" ]; then
      echo "$port_start" > "$port_file"
    fi

    released_ports=""
    if [ "${METADATA_DB_BACKEND:-sqlite}" = "sqlite" ] && [ -f "$metadata_db_file" ]; then
      released_ports="$(python3 - "$metadata_db_file" "$port_start" "$port_end" <<'PY'
import sqlite3
import sys

try:
    with sqlite3.connect(sys.argv[1]) as conn:
        rows = conn.execute(
            "SELECT port FROM ports WHERE status = 'released' AND port BETWEEN ? AND ? ORDER BY port",
            (int(sys.argv[2]), int(sys.argv[3])),
        )
        print("\n".join(str(row[0]) for row in rows))
except (OSError, sqlite3.Error, ValueError):
    pass
PY
)"
    fi

    occupied_ports=""
    if [ "${METADATA_DB_BACKEND:-sqlite}" = "postgres" ]; then
      if ! occupied_ports="$(python3 <<'PY'
import os
import sys
try:
    import psycopg
    with psycopg.connect(os.environ["METADATA_DATABASE_URL"], connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT port FROM instances WHERE port IS NOT NULL AND status IN ('provisioning', 'active', 'stopped', 'failed')")
            print("\n".join(str(row[0]) for row in cur.fetchall()))
except Exception:
    print("[ERROR] PostgreSQL port lookup failed; check executor psycopg dependency, database configuration and connectivity", file=sys.stderr)
    raise SystemExit(1)
PY
)"; then
        exit 1
      fi
    fi

    port=""
    for candidate in $released_ports; do
      if port_is_available "$candidate"; then
        port="$candidate"
        break
      fi
    done
    if [ -z "$port" ]; then
      port="$(cat "$port_file")"
    fi
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt "$port_start" ]; then
      port="$port_start"
    fi

    while true; do
      if [ "$port" -gt "$port_end" ]; then
        echo "[ERROR] No available port in range $port_start-$port_end" >&2
        exit 1
      fi

      if printf '%s\n' "$occupied_ports" | grep -qx "$port"; then
        echo "[INFO] Port $port is already reserved in metadata, skip" >&2
        port=$((port + 1))
        continue
      fi
      if port_is_available "$port"; then
        break
      fi

      echo "[INFO] Port $port is already in use, skip" >&2
      port=$((port + 1))
    done

    next_port=$((port + 1))
    echo "$next_port" > "$port_file"
    echo "$port"
  ) 9>"$lock_file"
}
