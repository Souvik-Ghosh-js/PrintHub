"""PrintHub platform configuration — plans, fees, formats, admin account.

Spec references are to the PrintTech Project Planning Document (GOBT).
"""
import os

SECRET_KEY = os.environ.get("PRINTHUB_SECRET_KEY", "CHANGE_ME_printhub_flask_secret")

# --- PLATFORM Cashfree account (GOBT's own) ---
# Receives vendor SUBSCRIPTION money: first combined payment (plan fee +
# installation) and renewals. This is separate from each vendor's OWN
# Cashfree account (stored per vendor row), which receives that shop's
# customer print-job payments — every site has a DIFFERENT Cashfree account.
PLATFORM_CASHFREE_APP_ID = os.environ.get("PLATFORM_CASHFREE_APP_ID", "")
PLATFORM_CASHFREE_SECRET_KEY = os.environ.get("PLATFORM_CASHFREE_SECRET_KEY", "")
PLATFORM_CASHFREE_WEBHOOK_SECRET = os.environ.get("PLATFORM_CASHFREE_WEBHOOK_SECRET", "")
PLATFORM_CASHFREE_ENV = os.environ.get("PLATFORM_CASHFREE_ENV", "production")  # production|sandbox

# --- Subscription & pricing model (spec §5, §6) ---
INSTALLATION_FEE = 2000          # one-time, every vendor, non-recurring
PLANS = {
    "monthly":  {"label": "Monthly",  "fee": 340,   "period_days": 30},
    "yearly":   {"label": "Yearly",   "fee": 3600,  "period_days": 365},
    "lifetime": {"label": "Lifetime", "fee": 20000, "period_days": None},  # no recurring
}
GRACE_DAYS = 15                  # renewal grace period (spec §8.3)

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
