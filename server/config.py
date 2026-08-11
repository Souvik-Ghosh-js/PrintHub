"""PrintHub platform configuration — plans, fees, formats, admin account.

Spec references are to the PrintTech Project Planning Document (GOBT).
"""

SECRET_KEY = "CHANGE_ME_printhub_flask_secret"

# --- Subscription & pricing model (spec §5, §6) ---
INSTALLATION_FEE = 2000          # one-time, every vendor, non-recurring
PLANS = {
    "monthly":  {"label": "Monthly",  "fee": 340,   "period_days": 30},
    "yearly":   {"label": "Yearly",   "fee": 3600,  "period_days": 365},
    "lifetime": {"label": "Lifetime", "fee": 20000, "period_days": None},  # no recurring
}
GRACE_DAYS = 15                  # renewal grace period (spec §8.3)

# --- Super Admin panel login (spec §8) ---
ADMIN_USER = "admin"
ADMIN_PASSWORD = "CHANGE_ME_admin_password"

# --- Supported document formats & their page layouts (spec §1) ---
# layout: how front/back are placed on the printed A4 page AND shown in the UI.
DOC_FORMATS = {
    "aadhaar": {"label": "Aadhaar Card", "layout": "side_by_side"},
    "pan":     {"label": "PAN Card",     "layout": "stacked"},
    "voter":   {"label": "Voter ID",     "layout": "side_by_side"},
}
