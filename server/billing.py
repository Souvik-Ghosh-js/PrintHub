"""Billing + subscription lifecycle (spec §5, §6, §8.3).

Payment model: ONLINE ONLY. Money is collected through Cashfree — vendor
subscriptions/renewals on the platform account, customer print jobs on each
vendor's own account. The functions here are called by app.py only after a
gateway payment is confirmed (webhook or verified order status); they never
mark anything paid on their own, and there is no manual/counter path.
"""
import secrets
import string
from datetime import datetime, timedelta

import db
from config import PLANS, INSTALLATION_FEE, GRACE_DAYS


# ---------------------------------------------------------------------------
# Amounts (spec §5)
# ---------------------------------------------------------------------------
def first_payment(plan: str) -> dict:
    """Subscription fee + one-time installation fee = first combined payment."""
    p = PLANS[plan]
    return {"plan": plan, "subscription_fee": p["fee"],
            "installation_fee": INSTALLATION_FEE,
            "total": p["fee"] + INSTALLATION_FEE}


def renewal_amount(plan: str) -> int:
    """Autopay amount for subsequent periods (no installation fee repeated)."""
    return PLANS[plan]["fee"]


# ---------------------------------------------------------------------------
# Credential generation (spec §4, §6.4)
# ---------------------------------------------------------------------------
def generate_credentials():
    """Auto-generated unique Login ID + Password, delivered after payment."""
    login_id = "PH" + "".join(secrets.choice(string.digits) for _ in range(6))
    # Ensure uniqueness against existing vendors.
    while db.get_vendor_by_login(login_id):
        login_id = "PH" + "".join(secrets.choice(string.digits) for _ in range(6))
    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(10))
    worker_token = secrets.token_hex(24)
    return login_id, password, worker_token


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def _next_renewal(plan: str, start: datetime):
    period = PLANS[plan]["period_days"]
    return (start + timedelta(days=period)) if period else None  # lifetime: never


def record_first_payment(vendor, method="cashfree", reference=None):
    """First combined payment (subscription + installation), CONFIRMED by the
    gateway: activates the vendor and generates their credentials. Returns the
    plaintext credentials (shown ONCE for delivery; only the hash is stored)."""
    from werkzeug.security import generate_password_hash

    amounts = first_payment(vendor["plan"])
    db.insert_payment({"vendor_id": vendor["id"], "kind": "installation",
                       "plan": vendor["plan"], "amount": amounts["installation_fee"],
                       "status": "paid", "method": method, "reference": reference})
    db.insert_payment({"vendor_id": vendor["id"], "kind": "subscription",
                       "plan": vendor["plan"], "amount": amounts["subscription_fee"],
                       "status": "paid", "method": method, "reference": reference})

    login_id, password, worker_token = generate_credentials()
    now = datetime.now()
    db.update_vendor(vendor["id"], {
        "status": "active",
        "subscribed_at": now,
        "renews_at": _next_renewal(vendor["plan"], now),
        "grace_until": None,
        "login_id": login_id,
        "password_hash": generate_password_hash(password),
        "worker_token": worker_token,
    })
    db.log_activity("admin", "vendor_activated",
                    f"first payment recorded (total {amounts['total']}), "
                    f"credentials generated", vendor_id=vendor["id"])
    return {"login_id": login_id, "password": password, **amounts}


def record_renewal_payment(vendor, method="cashfree", reference=None):
    """Renewal payment CONFIRMED by the gateway: extends the subscription one
    period and reactivates the vendor whatever state they were in
    (grace/suspended/rejected)."""
    amount = renewal_amount(vendor["plan"])
    db.insert_payment({"vendor_id": vendor["id"], "kind": "renewal",
                       "plan": vendor["plan"], "amount": amount,
                       "status": "paid", "method": method, "reference": reference})
    # Extend from the old renewal date if it's in the future, else from now.
    base = vendor.get("renews_at") or datetime.now()
    if isinstance(base, str):
        base = datetime.fromisoformat(base)
    if base < datetime.now():
        base = datetime.now()
    db.update_vendor(vendor["id"], {
        "status": "active",
        "renews_at": _next_renewal(vendor["plan"], base),
        "grace_until": None,
    })
    db.log_activity("system", "renewal_paid", f"amount {amount} ({method})",
                    vendor_id=vendor["id"])
    return amount


def refresh_status(vendor):
    """Enforce the subscription state machine (spec §8.3) on a vendor row.

    active --renewal date passed--> grace (15 days)
    grace  --grace expired-------> suspended (awaiting admin approve/reject)

    Returns the vendor row with any status change applied.
    """
    if not vendor or vendor["status"] in ("pending_payment", "rejected"):
        return vendor
    if vendor["plan"] == "lifetime" or not vendor.get("renews_at"):
        return vendor  # lifetime never expires

    now = datetime.now()
    renews_at = vendor["renews_at"]
    if isinstance(renews_at, str):
        renews_at = datetime.fromisoformat(renews_at)

    if vendor["status"] == "active" and now > renews_at:
        grace_until = renews_at + timedelta(days=GRACE_DAYS)
        new_status = "grace" if now <= grace_until else "suspended"
        db.update_vendor(vendor["id"], {"status": new_status,
                                        "grace_until": grace_until})
        db.log_activity("system", "subscription_" + new_status,
                        f"renewal was due {renews_at:%Y-%m-%d}",
                        vendor_id=vendor["id"])
        vendor = db.get_vendor(vendor["id"])
    elif vendor["status"] == "grace":
        grace_until = vendor.get("grace_until")
        if isinstance(grace_until, str):
            grace_until = datetime.fromisoformat(grace_until)
        if grace_until and now > grace_until:
            db.update_vendor(vendor["id"], {"status": "suspended"})
            db.log_activity("system", "subscription_suspended",
                            "grace period expired", vendor_id=vendor["id"])
            vendor = db.get_vendor(vendor["id"])
    return vendor


def has_access(vendor) -> bool:
    """Vendors keep platform access while active or in grace (spec §8.3)."""
    return bool(vendor) and vendor["status"] in ("active", "grace")
