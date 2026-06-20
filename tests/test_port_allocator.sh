#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib_port_allocator.sh"

PORT_AVAILABILITY_COMMAND=true

fail() {
  echo "[FAIL] $*" >&2
  exit 1
}

test_concurrent_allocations_are_unique() {
  local tmp_dir port_file output_file expected_count
  tmp_dir="$(mktemp -d)"
  port_file="$tmp_dir/ports.txt"
  output_file="$tmp_dir/allocated.txt"
  expected_count=12

  for _ in $(seq 1 "$expected_count"); do
    (
      allocate_port "$port_file" 41000 41050 >> "$output_file"
    ) &
  done

  wait

  local actual_count unique_count next_port
  actual_count="$(wc -l < "$output_file" | tr -d ' ')"
  unique_count="$(sort -n "$output_file" | uniq | wc -l | tr -d ' ')"
  next_port="$(cat "$port_file")"

  [ "$actual_count" = "$expected_count" ] || fail "expected $expected_count allocations, got $actual_count"
  [ "$unique_count" = "$expected_count" ] || fail "expected unique ports, got $unique_count unique values"
  [ "$next_port" = "41012" ] || fail "expected next port 41012, got $next_port"

  rm -rf "$tmp_dir"
}

test_invalid_port_file_resets_to_start() {
  local tmp_dir port_file allocated next_port
  tmp_dir="$(mktemp -d)"
  port_file="$tmp_dir/ports.txt"
  echo "not-a-port" > "$port_file"

  allocated="$(allocate_port "$port_file" 42000 42099)"
  next_port="$(cat "$port_file")"

  [ "$allocated" = "42000" ] || fail "expected allocated port 42000, got $allocated"
  [ "$next_port" = "42001" ] || fail "expected next port 42001, got $next_port"

  rm -rf "$tmp_dir"
}

test_range_exhaustion_fails() {
  local tmp_dir port_file
  tmp_dir="$(mktemp -d)"
  port_file="$tmp_dir/ports.txt"
  echo "43001" > "$port_file"

  if allocate_port "$port_file" 43000 43000 >/dev/null 2>&1; then
    fail "expected allocation to fail when range is exhausted"
  fi

  rm -rf "$tmp_dir"
}

test_concurrent_allocations_are_unique
test_invalid_port_file_resets_to_start
test_range_exhaustion_fails

echo "[OK] port allocator tests passed"
