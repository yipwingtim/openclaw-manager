upstream manager_user_web_backend {
    zone manager_user_web_backend 64k;
    resolver 127.0.0.11 valid=10s ipv6=off;
    resolver_timeout 5s;
    server openclaw-manager-user-web:8080 resolve;
}

upstream manager_admin_web_backend {
    zone manager_admin_web_backend 64k;
    resolver 127.0.0.11 valid=10s ipv6=off;
    resolver_timeout 5s;
    server openclaw-manager-admin-web:8080 resolve;
}

server {
    listen 30015 ssl;
    server_name _;

    ssl_certificate {{NGINX_SSL_CERT}};
    ssl_certificate_key {{NGINX_SSL_KEY}};

    client_max_body_size 20M;

{{MANAGER_NGINX_AUTH_DIRECTIVES}}

{{MANAGER_EMERGENCY_LOCATION}}

    location = /auth/uis/logout {
        access_log off;
        error_log /dev/null;
        proxy_pass http://manager_user_web_backend/auth/uis/logout?;
        proxy_set_header X-UIS-Logout-Token $arg_token;
{{MANAGER_INTERNAL_TOKEN_HEADER}}
    }

    location ^~ /admin {
        proxy_pass http://manager_admin_web_backend;

        proxy_buffering off;
        proxy_request_buffering off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Remote-User $remote_user;
{{MANAGER_INTERNAL_TOKEN_HEADER}}
    }

    location / {
        proxy_pass http://manager_user_web_backend;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Remote-User $remote_user;
{{MANAGER_INTERNAL_TOKEN_HEADER}}

        proxy_read_timeout 300;
        proxy_send_timeout 300;
    }
}
