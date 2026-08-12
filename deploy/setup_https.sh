#!/usr/bin/env bash
# ============================================================
#  PrintHub — run this LATER, once your domain points at the
#  instance. Sets the nginx server_name, gets an HTTPS cert,
#  and rewrites PRINTHUB_BASE_URL so QR codes / links use the
#  domain instead of the raw IP.
#
#  PREREQS:
#    - DNS A record: <your domain> -> this instance's static IP
#      (verify:  dig +short yourdomain.com)
#    - Lightsail firewall: ports 80 AND 443 open
#
#  RUN:   DOMAIN=print.example.com EMAIL=you@example.com \
#         bash deploy/setup_https.sh
# ============================================================
set -euo pipefail

DOMAIN="${DOMAIN:?set DOMAIN=yourdomain.com}"
EMAIL="${EMAIL:?set EMAIL=you@example.com (cert expiry notices)}"
PORT="8000"
ENV_FILE="/etc/printhub.env"

echo "==> [1/4] certbot..."
sudo apt-get install -y certbot python3-certbot-nginx

echo "==> [2/4] nginx server_name -> ${DOMAIN}"
sudo tee /etc/nginx/sites-available/printhub >/dev/null <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
sudo nginx -t && sudo systemctl reload nginx

echo "==> [3/4] HTTPS certificate (with HTTP->HTTPS redirect)..."
sudo certbot --nginx -d "$DOMAIN" \
  --non-interactive --agree-tos -m "$EMAIL" --redirect

echo "==> [4/4] PRINTHUB_BASE_URL -> https://${DOMAIN} (QR codes, file links)"
sudo sed -i "s|^PRINTHUB_BASE_URL=.*|PRINTHUB_BASE_URL=https://${DOMAIN}|" "$ENV_FILE"
sudo systemctl restart printhub.service

cat <<NOTE

============================================================
 Live at: https://${DOMAIN}

 REMEMBER:
  - Vendors' QR posters generated BEFORE this change encode the
    old IP URL — reprint them from the admin panel / vendor
    dashboard (the QR endpoint now uses the domain).
  - Cashfree webhook URLs:
      platform  -> https://${DOMAIN}/payment/webhook/platform
      vendors   -> https://${DOMAIN}/payment/webhook/vendor
  - Shop-PC workers: set the server URL to https://${DOMAIN}
============================================================
NOTE
