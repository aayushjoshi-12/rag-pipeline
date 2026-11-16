#!/bin/sh
# Optional HTTPS for the dashboard container. Runs after 20-envsubst-on-templates.sh (nginx image entrypoint hook).
# Set TLS_CERT_FILE and TLS_KEY_FILE (PEM, readable by the nginx user) to also serve the same site on :8443.
# Unset: nothing changes and the container stays HTTP-only on :8080.
set -eu

[ -n "${TLS_CERT_FILE:-}" ] || [ -n "${TLS_KEY_FILE:-}" ] || exit 0
if [ ! -r "${TLS_CERT_FILE:-}" ] || [ ! -r "${TLS_KEY_FILE:-}" ]; then
  echo "30-enable-tls: TLS_CERT_FILE and TLS_KEY_FILE must both be set and readable; refusing to start" >&2
  exit 1
fi

src=/etc/nginx/conf.d/default.conf
dst=/etc/nginx/conf.d/tls.conf
# Reuse the rendered HTTP server block so the locations (SPA, /api proxy) are defined once.
sed -e "s|listen 8080;|listen 8443 ssl;\n  http2 on;\n  ssl_certificate ${TLS_CERT_FILE};\n  ssl_certificate_key ${TLS_KEY_FILE};\n  ssl_protocols TLSv1.2 TLSv1.3;\n  ssl_session_cache shared:SSL:5m;\n  ssl_session_timeout 1h;|" \
  -e 's|proxy_set_header Host \$host;|proxy_set_header Host $host;\n    proxy_set_header X-Forwarded-Proto https;|' \
  "$src" > "$dst"
echo "30-enable-tls: HTTPS enabled on :8443"
