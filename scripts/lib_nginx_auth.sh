#!/usr/bin/env bash

normalize_basic_auth_enabled() {
  local value="${1:-true}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | xargs)"

  case "$value" in
    true|yes|y|1|on|enabled|"")
      printf 'true'
      ;;
    false|no|n|0|off|disabled)
      printf 'false'
      ;;
    *)
      return 1
      ;;
  esac
}

render_nginx_auth_lines() {
  local enabled="$1"
  local htpasswd_path="$2"

  if [ "$enabled" = "true" ]; then
    cat <<EOF
        auth_basic "OpenClaw Login";
        auth_basic_user_file $htpasswd_path;

EOF
  else
    cat <<'EOF'
        auth_basic off;

EOF
  fi
}
