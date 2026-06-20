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

  mkdir -p "$(dirname "$port_file")"
  touch "$lock_file"

  (
    flock -x 9

    if [ ! -f "$port_file" ]; then
      echo "$port_start" > "$port_file"
    fi

    port="$(cat "$port_file")"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt "$port_start" ]; then
      port="$port_start"
    fi

    while true; do
      if [ "$port" -gt "$port_end" ]; then
        echo "[ERROR] No available port in range $port_start-$port_end" >&2
        exit 1
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
