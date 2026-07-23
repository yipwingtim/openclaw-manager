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

render_instance_admin_provider_guard() {
  local provider="${1:-nginx-basic}"
  local public_host="${2:-}"
  case "$provider" in
    nginx-basic)
      return 0
      ;;
    local)
      [ -n "$public_host" ] || return 1
      printf '        return 302 https://%s:30015/; # managed-by-openclaw-manager-auth\n' "$public_host"
      ;;
    *)
      return 1
      ;;
  esac
}

nginx_user_htpasswd_file() {
  local user_id="$1"
  local htpasswd_file="$2"

  printf '%s/users/%s/.htpasswd' "$(dirname "$htpasswd_file")" "$user_id"
}

nginx_user_htpasswd_file_in_container() {
  local user_id="$1"
  local htpasswd_file_in_container="$2"

  printf '%s/users/%s/.htpasswd' "$(dirname "$htpasswd_file_in_container")" "$user_id"
}

nginx_user_htpasswd_ref() {
  local user_id="$1"
  local htpasswd_file_in_container="$2"

  printf 'nginx-auth:%s' "$(nginx_user_htpasswd_file_in_container "$user_id" "$htpasswd_file_in_container")"
}

ensure_nginx_htpasswd_permissions() {
  local htpasswd_file="$1"

  chmod 755 "$(dirname "$(dirname "$htpasswd_file")")" 2>/dev/null || true
  chmod 755 "$(dirname "$htpasswd_file")" 2>/dev/null || true
  chmod 644 "$htpasswd_file"
}
