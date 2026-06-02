#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <input.csv> <output.csv>"
  echo
  echo "Input CSV supported columns:"
  echo "  user_id"
  echo "  user_id,request_id"
  exit 1
fi

INPUT_CSV="$1"
OUTPUT_CSV="$2"

if [ ! -f "$INPUT_CSV" ]; then
  echo "[ERROR] Input CSV not found: $INPUT_CSV" >&2
  exit 1
fi

HEADER="$(head -n 1 "$INPUT_CSV" | tr -d '\r')"

read -r USER_ID_INDEX <<EOF
$(python3 - "$HEADER" <<'PY'
import csv
import sys

header = sys.argv[1]
columns = [item.strip() for item in next(csv.reader([header]))]
print(columns.index("user_id") + 1 if "user_id" in columns else 0)
PY
)
EOF

if [ "$USER_ID_INDEX" -eq 0 ]; then
  echo "[ERROR] Invalid input CSV header. Missing user_id column." >&2
  echo "[ERROR] Actual: $HEADER" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_CSV")"
echo "user_id,pending,status,message" > "$OUTPUT_CSV"

csv_escape() {
  local value="${1:-}"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

trim() {
  local value="${1:-}"
  value="${value//$'\r'/}"
  printf '%s' "$value" | xargs
}

csv_field() {
  local line="$1"
  local index="$2"
  python3 - "$line" "$index" <<'PY'
import csv
import sys

line, index = sys.argv[1], int(sys.argv[2])
row = next(csv.reader([line]))
if index <= 0 or index > len(row):
    print("")
else:
    print(row[index - 1])
PY
}

write_output_row() {
  local user_id="$1"
  local pending="$2"
  local status="$3"
  local message="$4"
  {
    csv_escape "$user_id"; printf ","
    csv_escape "$pending"; printf ","
    csv_escape "$status"; printf ","
    csv_escape "$message"; printf "\n"
  } >> "$OUTPUT_CSV"
}

line_no=0
while IFS= read -r line || [ -n "$line" ]; do
  line_no=$((line_no + 1))
  if [ "$line_no" -eq 1 ]; then
    continue
  fi

  line="${line//$'\r'/}"
  user_id="$(trim "$(csv_field "$line" "$USER_ID_INDEX")")"

  if [ -z "$user_id" ]; then
    continue
  fi

  if ! [[ "$user_id" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo "[WARN] Skip invalid user_id at line $line_no: $user_id" >&2
    write_output_row "$user_id" "unknown" "invalid_user_id" "Invalid user_id"
    continue
  fi

  echo "[INFO] Preview device requests for user: $user_id"
  if output="$("$SCRIPT_DIR/approve_device.sh" "$user_id" --list-only 2>&1)"; then
    if echo "$output" | grep -Eq '^Pending[[:space:]]*\([1-9][0-9]*\)|^Pending$'; then
      write_output_row "$user_id" "yes" "ok" "Pending request found"
    else
      write_output_row "$user_id" "no" "ok" "No pending request"
    fi
  else
    echo "[WARN] Failed to preview device requests for user: $user_id" >&2
    write_output_row "$user_id" "unknown" "failed" "$output"
  fi
done < "$INPUT_CSV"

echo "[INFO] Batch device request preview completed: $OUTPUT_CSV"
