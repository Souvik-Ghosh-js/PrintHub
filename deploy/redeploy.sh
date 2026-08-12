#!/usr/bin/env bash
# ============================================================
#  PrintHub — redeploy after uploading new code.
#
#  From your PC (uploads changed files, keeps the venv):
#     scp -i key.pem -r PrintHub/server PrintHub/deploy ubuntu@<IP>:/home/ubuntu/printhub/
#  Then on the instance:
#     bash /home/ubuntu/printhub/deploy/redeploy.sh
# ============================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/printhub}"

echo "==> Dependencies (only installs what changed)..."
cd "$APP_DIR/server"
.venv/bin/pip install -r requirements.txt --quiet

echo "==> Apply any new schema objects (CREATE TABLE IF NOT EXISTS is safe)..."
sudo mysql < "$APP_DIR/server/schema.sql" || true

echo "==> Restart..."
sudo systemctl restart printhub.service
sleep 2
curl -s http://127.0.0.1:8000/health && echo
sudo systemctl status printhub.service --no-pager | head -5
