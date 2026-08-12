#!/usr/bin/env bash
# ============================================================
#  PrintHub — full setup on a fresh Lightsail Ubuntu instance.
#
#  Installs MySQL + nginx, creates the database and app user,
#  builds the Python venv, generates secrets into /etc/printhub.env,
#  and runs the app as a systemd service behind nginx on port 80.
#
#  UPLOAD the code first (from your PC):
#     scp -i key.pem -r PrintHub ubuntu@<IP>:/home/ubuntu/printhub
#  then SSH in and RUN:
#     bash /home/ubuntu/printhub/deploy/setup.sh
#
#  Idempotent: safe to re-run after uploading new code.
#  Works BEFORE a domain exists (serves on the instance IP);
#  when you connect the domain later, run deploy/setup_https.sh.
# ============================================================
set -euo pipefail

# ---------------- CONFIG (defaults are fine) ----------------
APP_DIR="${APP_DIR:-/home/ubuntu/printhub}"    # repo root (contains server/)
UPLOAD_DIR="/var/lib/printhub/uploads"         # generated PDFs on the SSD
PORT="8000"                                    # gunicorn (behind nginx)
ENV_FILE="/etc/printhub.env"
DB_NAME="printhub"
DB_USER="printhub_app"
# ------------------------------------------------------------

if [ ! -f "$APP_DIR/server/app.py" ]; then
  echo "ERROR: $APP_DIR/server/app.py not found — upload the code first:"
  echo "  scp -i key.pem -r PrintHub ubuntu@<IP>:$APP_DIR"
  exit 1
fi

echo "==> [1/8] System packages (MySQL, nginx, Python, CV libs)..."
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  mysql-server nginx python3-venv python3-pip \
  libgl1 libglib2.0-0 curl
sudo systemctl enable --now mysql

echo "==> [2/8] Swap (pip installs of opencv/onnxruntime OOM small instances)..."
if ! swapon --show | grep -q .; then
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  grep -q "^/swapfile " /etc/fstab || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
fi

echo "==> [3/8] Secrets + environment file ($ENV_FILE)..."
# Generate once; re-runs keep the existing values (so the DB password,
# session key and admin password never silently change).
if [ ! -f "$ENV_FILE" ]; then
  PUBLIC_IP=$(curl -s --max-time 5 http://checkip.amazonaws.com || echo "127.0.0.1")
  DB_PASSWORD=$(openssl rand -hex 16)
  SECRET_KEY=$(openssl rand -hex 24)
  ADMIN_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')
  sudo tee "$ENV_FILE" >/dev/null <<ENV
# PrintHub environment — loaded by the systemd service.
PRINTHUB_DB=mysql
PRINTHUB_DB_HOST=127.0.0.1
PRINTHUB_DB_PORT=3306
PRINTHUB_DB_NAME=${DB_NAME}
PRINTHUB_DB_USER=${DB_USER}
PRINTHUB_DB_PASSWORD=${DB_PASSWORD}
PRINTHUB_UPLOADS=${UPLOAD_DIR}
# Base URL used in QR codes + worker file links. Currently the instance IP;
# deploy/setup_https.sh rewrites it when you connect the domain.
PRINTHUB_BASE_URL=http://${PUBLIC_IP}
PRINTHUB_SECRET_KEY=${SECRET_KEY}
PRINTHUB_ADMIN_USER=admin
PRINTHUB_ADMIN_PASSWORD=${ADMIN_PASSWORD}
# Platform Cashfree account (GOBT's own) — vendor subscription payments.
# Fill these in, then: sudo systemctl restart printhub
PLATFORM_CASHFREE_APP_ID=
PLATFORM_CASHFREE_SECRET_KEY=
PLATFORM_CASHFREE_WEBHOOK_SECRET=
PLATFORM_CASHFREE_ENV=production
ENV
  sudo chmod 600 "$ENV_FILE"
  echo "    generated new secrets (admin password shown at the end)"
else
  echo "    $ENV_FILE exists — keeping current values"
fi
# shellcheck disable=SC1090
DB_PASSWORD=$(sudo grep '^PRINTHUB_DB_PASSWORD=' "$ENV_FILE" | cut -d= -f2)

echo "==> [4/8] Storage directory..."
sudo mkdir -p "$UPLOAD_DIR"
sudo chown -R "$USER":"$USER" "$UPLOAD_DIR"

echo "==> [5/8] Database, tables, app user..."
sudo mysql < "$APP_DIR/server/schema.sql"
sudo mysql <<SQL
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "==> [6/8] Python venv + dependencies..."
cd "$APP_DIR/server"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> [7/8] systemd service (gunicorn)..."
sudo tee /etc/systemd/system/printhub.service >/dev/null <<UNIT
[Unit]
Description=PrintHub platform (Flask/gunicorn)
After=network.target mysql.service

[Service]
User=${USER}
WorkingDirectory=${APP_DIR}/server
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/server/.venv/bin/gunicorn --workers 2 --timeout 120 \\
    --bind 127.0.0.1:${PORT} app:app
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable printhub.service
sudo systemctl restart printhub.service

echo "==> [8/8] nginx (port 80 -> app; works on the bare IP, domain later)..."
sudo tee /etc/nginx/sites-available/printhub >/dev/null <<NGINX
server {
    listen 80 default_server;
    server_name _;

    client_max_body_size 50M;   # phone photos / PDFs

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/printhub /etc/nginx/sites-enabled/printhub
sudo nginx -t && sudo systemctl reload nginx

sleep 2
HEALTH=$(curl -s http://127.0.0.1:${PORT}/health || echo "no response")
PUBLIC_IP=$(sudo grep '^PRINTHUB_BASE_URL=' "$ENV_FILE" | cut -d= -f2)
ADMIN_PW=$(sudo grep '^PRINTHUB_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2)

cat <<NOTE

============================================================
 PrintHub is deployed.

   Health check : ${HEALTH}
   Public URL   : ${PUBLIC_IP}
   Super Admin  : ${PUBLIC_IP}/admin/login
                  user: admin   password: ${ADMIN_PW}
                  (stored in ${ENV_FILE})

 NEXT STEPS
 1. Lightsail firewall: open port 80 (and 443 for later).
 2. Log in to /admin/login -> register a vendor -> activate ->
    hand over credentials + QR poster.
 3. Platform Cashfree keys: sudo nano ${ENV_FILE}
    then: sudo systemctl restart printhub
 4. When the domain is connected:  bash ${APP_DIR}/deploy/setup_https.sh
    (sets HTTPS + rewrites PRINTHUB_BASE_URL so QR codes use the domain)

 REDEPLOY after uploading new code:
    bash ${APP_DIR}/deploy/redeploy.sh
 LOGS:
    sudo journalctl -u printhub -f
============================================================
NOTE
