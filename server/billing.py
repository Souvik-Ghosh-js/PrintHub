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
# Monthly vendors pay the plan fee through an autopay mandate, whose first
# debit lands immediately on approval. Their signup order is therefore the
# installation fee ALONE — charging the plan fee here too would bill them
# twice for month one. Yearly and lifetime have no mandate, so they pay the
# plan fee and installation together as one order.
def awaiting_mandate(vendor) -> bool:
    """Paid installation, but the plan fee has never been collected because
    the mandate was never approved. Distinguishes "new vendor still setting
    up" from "existing vendor who let their subscription lapse" — both sit in
    grace, but they need opposite messages."""
    if not vendor or not uses_autopay(vendor.get("plan", "")):
        return False
    if (vendor.get("autopay_status") or "").upper() == "ACTIVE":
        return False
    try:
        return not any(p["kind"] in ("subscription", "renewal")
                       for p in db.list_payments(vendor_id=vendor["id"]))
    except Exception:
        return False


def uses_autopay(plan: str) -> bool:
    """Only the monthly plan recurs via a mandate. Yearly is a one-time
    charge that the vendor renews themselves; lifetime never renews."""
    return plan == "monthly"


def first_payment(plan: str) -> dict:
    """What the vendor's signup order must collect."""
    p = PLANS[plan]
    if uses_autopay(plan):
        # Plan fee is collected by the mandate, not by this order.
        return {"plan": plan, "subscription_fee": 0,
                "installation_fee": INSTALLATION_FEE,
                "mandate_fee": p["fee"],
                "total": INSTALLATION_FEE}
    return {"plan": plan, "subscription_fee": p["fee"],
            "installation_fee": INSTALLATION_FEE,
            "mandate_fee": 0,
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
    if amounts["subscription_fee"]:
        db.insert_payment({"vendor_id": vendor["id"], "kind": "subscription",
                           "plan": vendor["plan"], "amount": amounts["subscription_fee"],
                           "status": "paid", "method": method, "reference": reference})

    login_id, password, worker_token = generate_credentials()
    now = datetime.now()
    fields = {
        "status": "active",
        "subscribed_at": now,
        "grace_until": None,
        "login_id": login_id,
        "password_hash": generate_password_hash(password),
        "worker_token": worker_token,
    }
    if amounts["mandate_fee"]:
        # The paid period starts when the mandate's first debit clears, not
        # now — installation alone buys no subscription time. Until then the
        # vendor is active so they can reach the dashboard and set autopay up.
        fields["renews_at"] = now
    else:
        fields["renews_at"] = _next_renewal(vendor["plan"], now)
    db.update_vendor(vendor["id"], fields)
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


def days_until_renewal(vendor):
    """Whole days until the subscription renews. None for lifetime/unknown,
    negative once it is overdue."""
    if not vendor or vendor.get("plan") == "lifetime":
        return None
    r = vendor.get("renews_at")
    if not r:
        return None
    if isinstance(r, str):
        try:
            r = datetime.fromisoformat(r)
        except ValueError:
            return None
    return (r.date() - datetime.now().date()).days


def sweep_subscriptions():
    """Apply the state machine to EVERY vendor, not just the one being viewed.

    refresh_status() only runs when a vendor's own page or worker touches
    their row, so a shop that stops logging in would never move to grace or
    suspended. This sweep is called from the worker poll and the admin panel
    so expiry is enforced platform-wide. Returns how many rows changed.
    """
    changed = 0
    try:
        vendors = db.list_vendors()
    except Exception as e:
        print(f"[sweep] could not list vendors: {e}")
        return 0
    for v in vendors:
        before = v.get("status")
        try:
            after = refresh_status(v)
        except Exception as e:
            print(f"[sweep] vendor {v.get('id')}: {e}")
            continue
        if after and after.get("status") != before:
            changed += 1
    return changed


def has_access(vendor) -> bool:
    """Vendors keep platform access while active or in grace (spec §8.3)."""
    return bool(vendor) and vendor["status"] in ("active", "grace")
