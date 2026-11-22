# Deploying PrintHub on AWS Lightsail

Everything below assumes a fresh **Ubuntu 22.04/24.04** Lightsail instance.
The scripts install MySQL on the same instance, create the database, and run
the app behind nginx. A domain is **not** required to start — the platform
runs on the instance IP, and `deploy/setup_https.sh` switches it to the
domain later.

## 1. Create the instance

1. Lightsail → Create instance → Linux → **Ubuntu 22.04 LTS** (or 24.04).
2. Plan: **1 GB RAM minimum** (2 GB recommended — the AI stack loads
   OpenCV + onnxruntime; the setup script adds swap either way).
3. Create a **static IP** and attach it to the instance (Networking tab).
   The QR codes embed this IP until the domain is connected.
4. Networking → Firewall: keep **SSH (22)**, add **HTTP (80)** and
   **HTTPS (443)**.
5. Download the SSH key (`.pem`) from Account → SSH keys if you haven't.

## 2. Get the code onto the instance

Deployment is git-based — push from your PC, clone/pull on the server:

```bash
ssh -i key.pem ubuntu@<STATIC_IP>
sudo apt-get update -y && sudo apt-get install -y git
git clone https://github.com/Souvik-Ghosh-js/PrintHub.git /home/ubuntu/printhub
```

(The ~8 MB ONNX model in `server/weights/` is committed, so it comes along.
For a private repo, use a deploy key or a PAT in the clone URL.)

## 3. Run the setup script (on the instance)

```bash
ssh -i key.pem ubuntu@<STATIC_IP>
bash /home/ubuntu/printhub/deploy/setup.sh
```

The script is idempotent (safe to re-run) and does, in order: apt packages
(mysql-server, nginx, python, CV libs) → swap file → generates
`/etc/printhub.env` with **random DB password, session key and Super Admin
password** → creates the `printhub` database + tables from
`server/schema.sql` and a least-privilege app user → Python venv + pip
install → `printhub` systemd service (gunicorn on 127.0.0.1:8000) → nginx
on port 80.

At the end it prints the **Super Admin password** — save it (it stays in
`/etc/printhub.env`).

Verify: open `http://<STATIC_IP>/` in a browser → the PrintHub landing page.
`http://<STATIC_IP>/health` should show `{"app":"ok","db":"ok",...}` — `db:ok`
proves MySQL is wired up.

## 4. Configure payments

```bash
sudo nano /etc/printhub.env      # fill PLATFORM_RAZORPAY_* (GOBT's account)
sudo systemctl restart printhub
```

- **Platform account** (vendor subscription money): set the three
  `PLATFORM_RAZORPAY_*` values. In that Razorpay dashboard set the webhook
  to `http://<STATIC_IP>/payment/webhook/platform` (switch to the https://
  domain URL later). Until these are set, activate vendors manually from
  the admin panel ("Record first payment").
- **Vendor accounts** (print-job money): nothing to do server-side — each
  vendor enters their own keys on their dashboard, webhook
  `…/payment/webhook/vendor`. Every shop has a different Razorpay account.
- **Autopay webhook**: add `…/payment/webhook/autopay` in the platform
  Razorpay dashboard with the `subscription.*` events (authenticated,
  activated, charged, pending, halted, cancelled). Its secret goes in
  `AUTOPAY_RAZORPAY_WEBHOOK_SECRET` (falls back to the platform one).

### Monthly billing calendar

Monthly vendors pay ₹999 installation + ₹294 for the first month in one
payment at signup. After that every month is **due on the 1st**:

| When | What happens |
|---|---|
| 27th | Autopay debits ₹294 for next month. |
| 27th → 1st, daily at 19:00 IST | If next month is still unpaid, the vendor gets a reminder email (none on the debit day itself while the debit is still in flight). |
| End of the 1st | Still unpaid → shop disabled (status `suspended`). They get one "shop offline" email. |
| Any time | Paying (autopay retry or **Pay now** on the dashboard) brings the shop straight back. |

The reminders are sent by `server/jobs.py`, run by the `printhub-daily`
systemd timer (installed by `setup.sh` / `redeploy.sh`). Email needs SMTP:

```bash
sudo nano /etc/printhub.env
# SMTP_HOST=smtp.gmail.com   SMTP_PORT=587   SMTP_SECURITY=starttls
# SMTP_USER=billing@yourdomain.com   SMTP_PASSWORD=<app password>
# SMTP_FROM=billing@yourdomain.com
cd /home/ubuntu/printhub/server && set -a && . /etc/printhub.env && set +a   && .venv/bin/python jobs.py daily --dry-run   # who would be emailed today
```

Customers' Word/Excel/PowerPoint uploads are converted to PDF by headless
LibreOffice. `deploy/install_converter.sh` installs it (with fonts for
Calibri/Arial-style layouts and Indian scripts); `setup.sh` and
`redeploy.sh` both run it, and it does nothing once installed. Check it with
`soffice --version`.

The same daily run deletes order PDFs older than 30 days
(`FILE_RETENTION_DAYS` in `config.py`) from `/var/lib/printhub/uploads`.
Order records are never deleted — only the file on disk. Printed files are
already removed the moment the shop prints them, so this mainly clears
uploads from checkouts that were never paid.

Without SMTP the job still disables unpaid shops; it logs each unsent
email and retries it on the next run.

## 5. First-run walkthrough

1. `http://<STATIC_IP>/admin/login` — user `admin`, the generated password.
2. Register a vendor (choose plan + shop code; the code becomes their URL:
   `http://<STATIC_IP>/<code>`).
3. Activate: either send them the `/pay/onboard/<code>` link (online first
   payment via the platform Razorpay account) or click **Record first
   payment + activate**. Credentials appear once — deliver them.
4. Open the **QR poster** for the vendor and get it to their counter.
5. Shop PC: run the worker (`worker/printhub_worker.py` or the built .exe),
   server URL `http://<STATIC_IP>`, log in with the vendor credentials,
   pick printers/trays, Start.

## 5b. Distribute the worker app (one build for ALL vendors)

You never build per-vendor apps — the worker is generic and takes its
identity from the vendor's login (it then remembers the session and
auto-reconnects after reboots). Build once on any Windows PC:

```powershell
cd C:\FRL\myprojs\PrintHub\worker
.\build_exe.bat            # produces dist\PrintHubWorker.exe
```

Upload it to the platform so vendors can download it themselves:

```powershell
scp -i key.pem worker\dist\PrintHubWorker.exe ubuntu@<STATIC_IP>:/home/ubuntu/printhub/server/downloads/
```

(`mkdir -p /home/ubuntu/printhub/server/downloads` first if needed.) A
"Download PrintHubWorker.exe" button then appears on every vendor's
dashboard (`/download/worker`, login required). Re-upload the file to ship
worker updates — vendors just re-download.

## 6. Later: connect the domain

1. In your DNS, create an **A record** → the static IP
   (e.g. `print.yourdomain.com` or the bare domain).
2. Wait for `dig +short yourdomain.com` (or an online DNS checker) to show
   the IP, then:

```bash
DOMAIN=yourdomain.com EMAIL=you@example.com \
  bash /home/ubuntu/printhub/deploy/setup_https.sh
```

That issues the HTTPS cert, redirects HTTP→HTTPS, and rewrites
`PRINTHUB_BASE_URL` so QR codes and worker file links use
`https://yourdomain.com/<shop_code>`. Afterwards: **reprint any QR posters**
issued while on the IP, update both Razorpay webhook URLs to the domain,
and point shop-PC workers at `https://yourdomain.com`.

## Day-2 operations

| Task | Command |
|---|---|
| Redeploy (git push, then on server) | `bash deploy/redeploy.sh` |
| App logs (live) | `sudo journalctl -u printhub -f` |
| Billing job: next run / logs | `systemctl list-timers printhub-daily.timer` / `sudo journalctl -u printhub-daily -n 50` |
| Billing job: run now | `sudo systemctl start printhub-daily.service` |
| Restart app | `sudo systemctl restart printhub` |
| MySQL shell | `sudo mysql printhub` |
| Change admin password | edit `/etc/printhub.env`, restart |
| DB backup | `sudo mysqldump printhub > backup.sql` |

Uploaded job PDFs live in `/var/lib/printhub/uploads` (auto-deleted after
printing).
