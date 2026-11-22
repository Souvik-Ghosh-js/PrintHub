"""PrintHub platform configuration — plans, fees, formats, admin account.

Spec references are to the PrintTech Project Planning Document (GOBT).
"""
import os


def _load_dotenv():
    """Local development: read KEY=VALUE lines from the repo's .env (or
    server/.env). Real environment variables always win, so on the server
    /etc/printhub.env (loaded by systemd) stays the source of truth."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(here, "..", ".env")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    value = value.strip().strip('"').strip("'")
                    os.environ.setdefault(key.strip(), value)
        except OSError:
            continue


_load_dotenv()

SECRET_KEY = os.environ.get("PRINTHUB_SECRET_KEY", "CHANGE_ME_printhub_flask_secret")

# --- PLATFORM Razorpay account (GOBT's own) ---
# Receives vendor money: the installation fee, yearly/lifetime plan fees and
# the monthly autopay mandate. Separate from each vendor's OWN Razorpay
# account (stored per vendor row), which receives that shop's customer
# print-job payments — every shop has a DIFFERENT account.
#
# Razorpay has no environment flag: test and live are different key pairs.
# The key id itself says which one is in use (rzp_test_* vs rzp_live_*).
PLATFORM_RAZORPAY_KEY_ID = os.environ.get("PLATFORM_RAZORPAY_KEY_ID", "")
PLATFORM_RAZORPAY_KEY_SECRET = os.environ.get("PLATFORM_RAZORPAY_KEY_SECRET", "")
# Set per-webhook in the Razorpay dashboard — NOT the API key secret, and
# each webhook carries its OWN secret, so they are configured separately.
# The autopay one falls back to the platform value when unset, which covers
# a dashboard where both webhooks were given the same secret.
PLATFORM_RAZORPAY_WEBHOOK_SECRET = os.environ.get("PLATFORM_RAZORPAY_WEBHOOK_SECRET", "")
AUTOPAY_RAZORPAY_WEBHOOK_SECRET = (
    os.environ.get("AUTOPAY_RAZORPAY_WEBHOOK_SECRET", "")
    or PLATFORM_RAZORPAY_WEBHOOK_SECRET)

# --- Subscription & pricing model (spec §5, §6) ---
INSTALLATION_FEE = 999           # one-time onboarding, every vendor, non-recurring
PLANS = {
    # Monthly runs on the CALENDAR month, not 30 days from signup — see the
    # billing calendar below. period_days is unused for it.
    "monthly":  {"label": "Monthly",  "fee": 294,   "period_days": None},
    "yearly":   {"label": "Yearly",   "fee": 1999,  "period_days": 365},
    "lifetime": {"label": "Lifetime", "fee": 20000, "period_days": None},  # no recurring
}
GRACE_DAYS = 15                  # yearly renewal grace period (spec §8.3)

# --- Monthly billing calendar ---
# Every monthly vendor's month is due on the 1st. Autopay debits on the 27th
# of the month before, so a failed debit has days of Razorpay retries before
# the deadline. Reminder emails go out daily from the 27th to the 1st while
# the coming month is unpaid; still unpaid at the END of the 1st, the shop is
# disabled (suspended) until it pays. The 27th exists in every month, which
# is why it is the debit day and not the 28th-31st.
AUTOPAY_DEBIT_DAY = 27
AUTOPAY_DEBIT_HOUR = 6           # early, so the day's reminder run sees the result
REMINDER_FROM_DAY = 27
# The calendar is Indian, whatever timezone the server clock runs in.
BILLING_TZ_OFFSET_MINUTES = 330  # IST, UTC+05:30 (no DST)

# --- Stored order files ---
# The daily job deletes order PDFs older than this many days (mostly
# abandoned checkouts; printed files are deleted as soon as they print).
# Order records are always kept — only the file on disk goes.
FILE_RETENTION_DAYS = 30

# --- Outgoing email (billing reminders) over SMTP ---
# Defaults are the Mohini Print Shop Gmail account (app password), so
# reminders work after a redeploy with no server-side setup. A non-empty
# SMTP_* in /etc/printhub.env or .env overrides them; a blank one (like the
# lines setup.sh writes) keeps the default. SMTP_SECURITY: starttls (587) |
# ssl (465) | none. NOTE: this repository is public — if the app password is
# ever misused, revoke it in the Google account and set a new one only in
# /etc/printhub.env.
SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 587)
SMTP_USER = os.environ.get("SMTP_USER") or "souvikghoshkalna@gmail.com"
SMTP_PASSWORD = (os.environ.get("SMTP_PASSWORD") or "ljsltugrojjoozwv").replace(" ", "")
SMTP_SECURITY = (os.environ.get("SMTP_SECURITY") or "starttls").lower()
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
# Name vendors see as the sender, in the sign-off and in subject lines.
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "") or "Mohini Print Shop"

# --- Super Admin panel login (spec §8) ---
ADMIN_USER = os.environ.get("PRINTHUB_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("PRINTHUB_ADMIN_PASSWORD", "CHANGE_ME_admin_password")

# --- How shop owners reach us (shown on the site) ---
# Phone numbers in international form so tel:/wa.me links work from a phone.
CONTACT_PHONES = [
    {"label": "+91 99033 47290", "tel": "+919903347290", "wa": "919903347290"},
    {"label": "+91 62955 66948", "tel": "+916295566948", "wa": "916295566948"},
]

# --- Supported document formats & their page layouts (spec §1) ---
# layout: how front/back are placed on the printed A4 page AND shown in the UI.
DOC_FORMATS = {
    "aadhaar": {"label": "Aadhaar Card", "layout": "side_by_side"},
    "pan":     {"label": "PAN Card",     "layout": "stacked"},
    "voter":   {"label": "Voter ID",     "layout": "side_by_side"},
}
