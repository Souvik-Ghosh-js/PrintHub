#!/usr/bin/env bash
# ============================================================
#  PrintHub — install/refresh the daily billing job (systemd timer).
#
#  Runs `python jobs.py daily` every evening at 19:00 India time:
#  moves unpaid shops to grace/suspended and emails monthly vendors
#  whose coming month is unpaid (27th..1st). Called by setup.sh and
#  redeploy.sh; safe to run on its own and to re-run.
#
#  Check it:   systemctl list-timers printhub-daily.timer
#  Run now:    sudo systemctl start printhub-daily.service
#  Logs:       sudo journalctl -u printhub-daily -n 50
# ============================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/printhub}"
ENV_FILE="/etc/printhub.env"
RUN_USER="${RUN_USER:-$USER}"

sudo tee /etc/systemd/system/printhub-daily.service >/dev/null <<UNIT
[Unit]
Description=PrintHub daily billing job (status sweep + reminder emails)
After=network-online.target mysql.service
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${APP_DIR}/server
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/server/.venv/bin/python jobs.py daily
UNIT

# 19:00 IST: after the 27th's early-morning autopay debit has had the day
# to settle. Persistent=true runs a missed job after a reboot.
sudo tee /etc/systemd/system/printhub-daily.timer >/dev/null <<UNIT
[Unit]
Description=Run the PrintHub billing job every evening

[Timer]
OnCalendar=*-*-* 19:00:00 Asia/Kolkata
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now printhub-daily.timer
systemctl list-timers printhub-daily.timer --no-pager | head -3 || true
