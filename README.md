# PrintHub

Multi-vendor document scanning & printing platform — implementation of the
**PrintTech Project Planning Document** (GOBT), built on the proven codebase of
the working prototype at `printer/printer` (website + desktop worker).

```
PrintHub/
  server/       Flask platform: customer scan/print, vendor portal,
                worker API, Super Admin panel, DocumentAI scanning
  worker/       Vendor desktop app (Windows): login, printer config, printing
  docs/         (reserved)
```

## Spec → implementation map

| Spec section | Where |
|---|---|
| §1 Document format layouts (Aadhaar side-by-side, PAN stacked, Voter side-by-side) | `server/config.py` `DOC_FORMATS`; UI in `templates/customer.html`; print layout in `app.py compose_id_pdf` |
| §2 Per-page delete + auto-centre single page | `customer.html` slots UI; single side prints centred (`compose_id_pdf`) |
| §3 Page Cutting & AI Processing | `server/docscan.py` + `server/docenh/` + `weights/docunet_mobile.onnx`. Auto Precise Cut = ML corner detection with classical-CV fallback (`/api/detect_document`, source logged in browser + server console). Light/Burn filter = `/api/enhance_page` (shadow removal, white balance, contrast, gray/B&W). Manual adjustment = Cropper.js corner handles |
| §4 Vendor onboarding, auto-generated credentials | `billing.generate_credentials`; delivered once in the admin panel after first payment |
| §5 Subscription & pricing (₹294/mo, ₹1999/yr, ₹20000 lifetime + ₹999 install) | `config.PLANS`, `billing.first_payment` |
| Monthly calendar: due on the 1st, autopay on the 27th, reminder emails 27th→1st, disabled after the 1st | `billing.py` (`monthly_paid_until`, `refresh_status`, `billing_email_due`), `jobs.py` + `notify.py`, `deploy/install_timer.sh` |
| §6 Billing logic (first combined payment, autopay renewals, credential generation, vendor pricing control) | `billing.py`; vendor B/W + colour prices in the vendor dashboard |
| §7 Printer configuration (single/multi max 2, B/W+colour routing, trays, Change Printer) | `worker/printhub_worker.py` (config UI + routing) synced to the server via `/worker/api/printer-config`; visible on the vendor dashboard |
| §8 Super Admin (activity log, payment log, grace, disable/enable/delete vendor) | `/admin` panel; state machine in `billing.refresh_status` |

## Running the server

### Local testing — zero setup
No MySQL needed: if MySQL is unreachable the data layer automatically falls
back to a local SQLite file (`server/printhub.sqlite`, created on first run).

```bash
cd server
pip install -r requirements.txt
python app.py                        # http://127.0.0.1:5000
```

### Production (Lightsail — MySQL + nginx + systemd)
See **[docs/DEPLOY.md](docs/DEPLOY.md)**. Short version: upload the repo to
the instance, then `bash deploy/setup.sh` — it installs MySQL, creates the
DB + app user, generates secrets into `/etc/printhub.env`, and runs the app
behind nginx on port 80 (no domain needed to start). When the domain is
connected: `DOMAIN=… EMAIL=… bash deploy/setup_https.sh`. Redeploys:
`bash deploy/redeploy.sh`.

### First-run walkthrough
1. `/admin/login` (credentials in `config.py`) → register a vendor with a plan.
2. Click **Record first payment + activate** — the auto-generated Login ID +
   Password appear ONCE; deliver them to the vendor.
3. Vendor logs in at `/vendor/login` → sets B/W + colour per-page prices.
4. On the shop PC: run the worker app, log in with the same credentials,
   choose Single or Multiple (B/W + Colour) printers + trays, **Save / Change
   Printer**, then **Start**.
5. Hand the vendor their **counter QR poster** (admin panel → "QR poster",
   also on the vendor dashboard). The QR encodes the vendor's short branded
   URL on our domain — `https://<PRINTHUB_BASE_URL>/<shop_code>`, e.g.
   `mohiniprintshop.org/vendor2` — customers scan it, no login needed.
6. Customers land on that page → pick Aadhaar / PAN / Voter / Document scan
   → photos are auto-cropped by the ML model (Cropper.js manual fine-tune,
   with console logging of model vs fallback) → optional Light/Burn filter →
   place order → the worker prints it on the right printer automatically.

## Worker desktop app

```bash
cd worker
pip install -r requirements.txt
python printhub_worker.py            # or build_exe.bat for a standalone .exe
```

**One build serves every vendor** — the app is generic; identity comes from
the vendor's login (Login ID + Password → the server returns that vendor's
token and printer config). It saves the session in
`%APPDATA%\PrintHub\worker.json`, auto-reconnects on the next launch, and
has a Log out button to switch shops. Distribute it by uploading the built
`PrintHubWorker.exe` to `server/downloads/` on the server — vendors then get
a download button on their dashboard (`/download/worker`).

## Payments (Razorpay — every shop has a DIFFERENT account)

Two kinds of Razorpay accounts, handled by `server/razorpay_pg.py`:

1. **Each vendor's own account** (`vendors.razorpay_*` columns) receives that
   shop's customer print-job payments. A shop with no keys cannot take orders
   at all (the gateway declines to create the order). Vendors point their
   Razorpay webhook at `…/payment/webhook/vendor`, subscribed to
   `payment.captured` and `payment.failed`; a reconciler also re-checks
   pending orders with Razorpay directly, so payments confirm even without a
   reachable webhook.

2. **The platform account** (GOBT's own; `PLATFORM_RAZORPAY_*` env vars)
   receives the installation fee, yearly/lifetime plan fees, and the monthly
   autopay mandate. Webhooks go to `…/payment/webhook/platform` and
   `…/payment/webhook/autopay`.

**Amounts are integer paise at the Razorpay boundary** and rupees everywhere
else. `razorpay_pg.to_paise()` / `to_rupees()` are the only places that
convert — nothing else should multiply by 100.

**A payment counts only when CAPTURED.** An `authorized` payment has taken
the customer's money but is auto-refunded within 3 days if never captured, so
it must never mark a job printable or a subscription active.

Three different signatures, easy to confuse:

| What | Signed string | Key |
|---|---|---|
| One-time payment | `order_id\|payment_id` | API key secret |
| Subscription auth | `payment_id\|subscription_id` | API key secret |
| Webhook | the raw request body | **webhook** secret |

Autopay uses Razorpay Subscriptions with **no `start_at`**, so authorising the
mandate charges the first month immediately and Razorpay keeps it; every later
cycle is debited by Razorpay itself (no scheduler here). A failed debit arrives
as `subscription.pending`, then `subscription.halted` once retries are spent.

## Deliberate v1 simplifications
- **Autopay** is modeled (renewal amounts, due dates, grace, suspension) and
  renewals are one-click online payments, but charges are not initiated
  automatically — a gateway mandate/e-NACH integration slots into
  `billing.record_renewal_payment` unchanged.
- Customer page accepts **images** (camera photos); the prototype's PDF-upload
  path (pdf.js) can be ported into `customer.html` when needed.
- The spec's "mobile application" ships here as a mobile-friendly web app; the
  scanning endpoints (`/api/detect_document`, `/api/enhance_page`) are already
  API-shaped for a future native app, and `docs/HANDOFF_FOR_APP_DEVELOPER.md`
  in GOBT_ML describes on-device ONNX inference for full offline scanning.
