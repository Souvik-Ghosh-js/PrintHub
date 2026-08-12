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

## 2. Upload the code (from your Windows PC)

```powershell
cd C:\FRL\myprojs
scp -i C:\path\to\key.pem -r PrintHub ubuntu@<STATIC_IP>:/home/ubuntu/printhub
```

(`scp` ships with Windows 10/11. Exclude nothing — the ~8 MB ONNX model in
`server/weights/` must go along. If you prefer, delete
`server/printhub.sqlite` and `server/__pycache__` first; they're unused in
production.)

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
sudo nano /etc/printhub.env      # fill PLATFORM_CASHFREE_* (GOBT's account)
sudo systemctl restart printhub
```

- **Platform account** (vendor subscription money): set the three
  `PLATFORM_CASHFREE_*` values. In that Cashfree dashboard set the webhook
  to `http://<STATIC_IP>/payment/webhook/platform` (switch to the https://
  domain URL later). Until these are set, activate vendors manually from
  the admin panel ("Record first payment").
- **Vendor accounts** (print-job money): nothing to do server-side — each
  vendor enters their own keys on their dashboard, webhook
  `…/payment/webhook/vendor`. Every site has a different Cashfree account.

## 5. First-run walkthrough

1. `http://<STATIC_IP>/admin/login` — user `admin`, the generated password.
2. Register a vendor (choose plan + shop code; the code becomes their URL:
   `http://<STATIC_IP>/<code>`).
3. Activate: either send them the `/pay/onboard/<code>` link (online first
   payment via the platform Cashfree account) or click **Record first
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
issued while on the IP, update both Cashfree webhook URLs to the domain,
and point shop-PC workers at `https://yourdomain.com`.

## Day-2 operations

| Task | Command |
|---|---|
| Redeploy after code upload | `bash deploy/redeploy.sh` |
| App logs (live) | `sudo journalctl -u printhub -f` |
| Restart app | `sudo systemctl restart printhub` |
| MySQL shell | `sudo mysql printhub` |
| Change admin password | edit `/etc/printhub.env`, restart |
| DB backup | `sudo mysqldump printhub > backup.sql` |

Uploaded job PDFs live in `/var/lib/printhub/uploads` (auto-deleted after
printing).
