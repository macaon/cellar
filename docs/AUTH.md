# Bearer Token Authentication for HTTP(S) Repos

Cellar supports per-repo bearer token auth for HTTP(S) repos. This lets you
share a private repo URL without making it publicly accessible.

## Setup

1. Open Preferences → **Access Control** → **Generate**. Copy the token.
2. Paste it into your web server config (see examples below).
3. When adding the repo in Cellar, enter the URL and paste the token into the
   **Access token** field.

## nginx

```nginx
map $http_authorization $cellar_auth_ok {
    "Bearer YOUR_TOKEN_HERE"  1;
    default                   0;
}

server {
    listen 443 ssl;
    server_name cellar.example.com;

    ssl_certificate     /etc/ssl/certs/cellar.crt;
    ssl_certificate_key /etc/ssl/private/cellar.key;

    # ^~ is required: stops regex location blocks (e.g. image-caching rules)
    # from intercepting asset requests before this block runs.
    location ^~ /cellar/ {
        if ($cellar_auth_ok = 0) { return 401 "Unauthorized\n"; }
        root /;
        autoindex off;
        add_header Accept-Ranges bytes;
        expires 5d;
    }
}
```

## Caddy

```caddy
cellar.example.com {
    handle /cellar/* {
        @unauth not header Authorization "Bearer YOUR_TOKEN_HERE"
        respond @unauth "Unauthorized" 401
        file_server { root / }
    }
}
```
