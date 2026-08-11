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
| §5 Subscription & pricing (₹340/mo, ₹3600/yr, ₹20000 lifetime + ₹2000 install) | `config.PLANS`, `billing.first_payment` |
| §6 Billing logic (first combined payment, autopay renewals, credential generation, vendor pricing control) | `billing.py`; vendor B/W + colour prices in the vendor dashboard |
| §7 Printer configuration (single/multi max 2, B/W+colour routing, trays, Change Printer) | `worker/printhub_worker.py` (config UI + routing) synced to the server via `/worker/api/printer-config`; visible on the vendor dashboard |
| §8 Super Admin (activity log, payment log, 15-day grace, approve/reject) | `/admin` panel; state machine in `billing.refresh_status` |

## Running the server

### Local testing — zero setup
No MySQL needed: if MySQL is unreachable the data layer automatically falls
back to a local SQLite file (`server/printhub.sqlite`, created on first run).

```bash
cd server
pip install -r requirements.txt
python app.py                        # http://127.0.0.1:5000
```

### Production (MySQL)
```bash
cd server
mysql -u root -p < schema.sql        # creates the `printhub` database
# create the app DB user, set PRINTHUB_DB_* env vars (or edit db.py)
PRINTHUB_DB=mysql python app.py      # require MySQL (no SQLite fallback)
```

Change before deploying: `config.SECRET_KEY`, `config.ADMIN_PASSWORD`,
DB credentials in `db.py` (env vars `PRINTHUB_DB_*`), `PRINTHUB_BASE_URL`.
Production: `gunicorn -w 2 -b 0.0.0.0:8000 app:app` behind HTTPS (the
prototype's `deploy_setup.sh` / `setup_https.sh` apply nearly unchanged).

### First-run walkthrough
1. `/admin/login` (credentials in `config.py`) → register a vendor with a plan.
2. Click **Record first payment + activate** — the auto-generated Login ID +
   Password appear ONCE; deliver them to the vendor.
3. Vendor logs in at `/vendor/login` → sets B/W + colour per-page prices.
4. On the shop PC: run the worker app, log in with the same credentials,
   choose Single or Multiple (B/W + Colour) printers + trays, **Save / Change
   Printer**, then **Start**.
5. Customers open `/shop/<code>` → pick Aadhaar / PAN / Voter / Document scan
   → photos are auto-cropped by the ML model (Cropper.js manual fine-tune,
   with console logging of model vs fallback) → optional Light/Burn filter →
   place order → the worker prints it on the right printer automatically.

## Worker desktop app

```bash
cd worker
pip install -r requirements.txt
python printhub_worker.py            # or build_exe.bat for a standalone .exe
```

## Deliberate v1 simplifications
- **Payments are recorded, not collected**: subscription amounts follow the
  spec exactly, but the admin records them manually. The prototype's working
  Cashfree integration can be wired into `billing.record_first_payment` /
  `record_renewal_payment` via webhook without touching the lifecycle logic.
  Customer print jobs are "pay at the counter".
- **Autopay** is modeled (renewal amounts, due dates, grace, suspension) but
  charges are not initiated automatically — a gateway mandate/e-NACH
  integration slots into the same two functions.
- Customer page accepts **images** (camera photos); the prototype's PDF-upload
  path (pdf.js) can be ported into `customer.html` when needed.
- The spec's "mobile application" ships here as a mobile-friendly web app; the
  scanning endpoints (`/api/detect_document`, `/api/enhance_page`) are already
  API-shaped for a future native app, and `docs/HANDOFF_FOR_APP_DEVELOPER.md`
  in GOBT_ML describes on-device ONNX inference for full offline scanning.
