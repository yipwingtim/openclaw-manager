server {
    listen 30015 ssl;
    server_name _;

    ssl_certificate {{NGINX_SSL_CERT}};
    ssl_certificate_key {{NGINX_SSL_KEY}};

    client_max_body_size 20M;

    auth_basic "OpenClaw Login";
    auth_basic_user_file {{NGINX_HTPASSWD_FILE_IN_CONTAINER}};

    location = /admin {
        return 302 /admin/;
    }

    location /admin/ {
        proxy_pass http://openclaw-manager-web:8080/admin/;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Remote-User $remote_user;

        proxy_read_timeout 300;
        proxy_send_timeout 300;
    }

    location / {
        proxy_pass http://openclaw-manager-web:8080/;

        proxy_buffering off;
        proxy_request_buffering off;

        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Remote-User $remote_user;

        proxy_read_timeout 300;
        proxy_send_timeout 300;
    }
}
