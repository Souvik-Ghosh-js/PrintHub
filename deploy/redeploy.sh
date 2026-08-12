#!/usr/bin/env bash
# ============================================================
#  PrintHub — redeploy after uploading new code.
#
#  Normal flow — push to GitHub from your PC, then on the instance:
#     bash /home/ubuntu/printhub/deploy/redeploy.sh
#  (pulls origin/main, reinstalls deps, applies schema, restarts)
#
#  Skip the pull (e.g. after an scp of local-only files):
#     NO_PULL=1 bash /home/ubuntu/printhub/deploy/redeploy.sh
# ============================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/printhub}"
BRANCH="${BRANCH:-main}"

if [ -z "${NO_PULL:-}" ] && [ -d "$APP_DIR/.git" ]; then
  echo "==> Pulling latest code from GitHub ($BRANCH)..."
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
  git -C "$APP_DIR" log --oneline -1
fi

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
