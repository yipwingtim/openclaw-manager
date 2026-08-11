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
  local auth_type="${3:-}"
  case "$provider" in
    nginx-basic)
      return 0
      ;;
    local)
      [ -n "$public_host" ] || return 1
      printf '        return 302 https://%s:30015/; # managed-by-openclaw-manager-auth\n' "$public_host"
      ;;
    *)
      case "$auth_type" in oidc|oauth2) ;; *) return 1 ;; esac
      [ -n "$public_host" ] || return 1
      printf '        return 302 https://%s:30015/; # managed-by-openclaw-manager-auth\n' "$public_host"
      ;;
  esac
}

render_instance_auth_upstream() {
  local instance_id="$1"
  [ -n "$instance_id" ] || return 1
  local upstream="instance_auth_${instance_id//[^A-Za-z0-9_]/_}"
  cat <<EOF
upstream $upstream {
    zone $upstream 64k;
    resolver 127.0.0.11 valid=10s ipv6=off;
    server openclaw-instance-auth-proxy:8084 resolve;
}
EOF
}

render_instance_auth_location() {
  local instance_id="$1"
  local public_host="${PUBLIC_HOST:-}"
  [ -n "$instance_id" ] && [ -n "$public_host" ] || return 1
  local upstream="instance_auth_${instance_id//[^A-Za-z0-9_]/_}"
  cat <<EOF
    location = /_instance_auth {
        internal;
        proxy_pass http://$upstream/authorize/$instance_id;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie \$http_cookie;
    }
    location @instance_login {
        return 302 https://$public_host:30015/login?instance=$instance_id;
    }
EOF
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
