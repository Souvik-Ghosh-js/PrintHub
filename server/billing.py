"""Billing + subscription lifecycle (spec §5, §6, §8.3).

Payment model: ONLINE ONLY. Money is collected through Razorpay — vendor
subscriptions/renewals on the platform account, customer print-job payments
on each vendor's own account. The functions here are called by app.py only
after a gateway payment is confirmed (webhook or verified order status); they
never mark anything paid on their own, and there is no manual/counter path.

Monthly billing calendar (config.AUTOPAY_DEBIT_DAY etc.):
  * every month is due on the 1st; `renews_at` is that 1st at 00:00
  * autopay debits on the 27th of the month before
  * reminder emails go out daily from the 27th to the 1st while unpaid
  * still unpaid at the END of the 1st -> suspended (shop offline) until paid
Yearly keeps its 365-day period and 15-day grace; lifetime never renews.
"""
import secrets
import string
from datetime import datetime, timedelta, timezone

import db
from config import (PLANS, INSTALLATION_FEE, GRACE_DAYS, AUTOPAY_DEBIT_DAY,
                    AUTOPAY_DEBIT_HOUR, REMINDER_FROM_DAY,
                    BILLING_TZ_OFFSET_MINUTES)

_BILLING_TZ = timezone(timedelta(minutes=BILLING_TZ_OFFSET_MINUTES))
# Mandate statuses in which Razorpay will debit on schedule. 'pending' means
# a debit failed and is being retried; 'halted' means retries ran out.
AUTOPAY_HEALTHY = ("active", "authenticated")


def now() -> datetime:
    """Current time on the billing calendar (IST), as a naive datetime.

    "Due on the 1st" means the 1st in India, whatever timezone the server
    clock runs in — a UTC server would otherwise cut shops off at 05:30."""
    return datetime.now(_BILLING_TZ).replace(tzinfo=None)


def to_unix(d: datetime) -> int:
    """Billing-calendar (IST) naive datetime -> unix seconds."""
    return int(d.replace(tzinfo=_BILLING_TZ).timestamp())


def from_unix(ts) -> datetime:
    """Unix seconds -> billing-calendar (IST) naive datetime."""
    return datetime.fromtimestamp(int(ts), _BILLING_TZ).replace(tzinfo=None)


def _as_dt(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def next_month_start(d: datetime) -> datetime:
    """00:00 on the 1st of the month after `d`."""
    return (datetime(d.year + 1, 1, 1) if d.month == 12
            else datetime(d.year, d.month + 1, 1))


def _is_month_start(d: datetime) -> bool:
    return (d.day == 1 and d.hour == 0 and d.minute == 0
            and d.second == 0 and d.microsecond == 0)


# ---------------------------------------------------------------------------
# Amounts (spec §5)
# ---------------------------------------------------------------------------
def uses_autopay(plan: str) -> bool:
    """Only the monthly plan recurs via a mandate. Yearly is a one-time
    charge that the vendor renews themselves; lifetime never renews."""
    return plan == "monthly"


def first_payment(plan: str) -> dict:
    """What the vendor's signup order collects: installation + the first
    period of the plan, as ONE order.

    For monthly this works because the autopay mandate does not charge at
    approval — its first debit is scheduled for the next 27th (see
    next_autopay_start) — so nobody pays month one twice."""
    p = PLANS[plan]
    return {"plan": plan, "subscription_fee": p["fee"],
            "installation_fee": INSTALLATION_FEE,
            "total": p["fee"] + INSTALLATION_FEE}


# Statuses an administrator set by hand. Billing never moves a vendor out
# of them — a payment still extends the paid period, but only the admin's
# Enable brings the shop back. ('rejected' is the older name for this.)
ADMIN_DISABLED = ("disabled", "rejected")


def renewal_amount(plan: str) -> int:
    """Amount for subsequent periods (no installation fee repeated)."""
    return PLANS[plan]["fee"]


def _paid_plan_fee(vendor) -> bool:
    try:
        return any(p["kind"] in ("subscription", "renewal")
                   for p in db.list_payments(vendor_id=vendor["id"]))
    except Exception:
        return True   # unknown: assume paid rather than nag a paying shop


def owes_first_month(vendor) -> bool:
    """Monthly vendor who paid installation but never a plan fee.

    Only vendors who signed up under the old split flow (installation
    first, mandate later) can be in this state; new signups pay both in one
    order. They need a "pay your first month" message, not "overdue"."""
    if not vendor or not uses_autopay(vendor.get("plan", "")):
        return False
    if vendor.get("status") in ("pending_payment",):
        return False
    return not _paid_plan_fee(vendor)


# ---------------------------------------------------------------------------
# Monthly calendar
# ---------------------------------------------------------------------------
def monthly_paid_until(renews_at, paid_at: datetime) -> datetime:
    """New `renews_at` after one monthly fee is paid at `paid_at`.

    Paid ahead (renews_at still in the future — the normal autopay debit on
    the 27th): one more calendar month on top.
    Paid late, or a first payment: the current month, up to the next 1st.
    Paid on/after the 27th, the next debit would already be due within days,
    so the payment covers the rest of this month AND all of next month —
    a vendor joining on the 28th is not charged twice in one week.
    """
    renews_at = _as_dt(renews_at)
    if renews_at and renews_at > paid_at:
        return next_month_start(renews_at)
    end = next_month_start(paid_at)
    if paid_at.day >= AUTOPAY_DEBIT_DAY:
        end = next_month_start(end)
    return end


def debit_date_for(due: datetime) -> datetime:
    """When autopay collects the month that starts on `due` (a 1st): the
    27th of the month before, early morning."""
    prev = due - timedelta(days=1)
    return datetime(prev.year, prev.month, AUTOPAY_DEBIT_DAY,
                    AUTOPAY_DEBIT_HOUR, 0)


def next_autopay_start(vendor, at=None, lead=timedelta(hours=2)) -> datetime:
    """First debit for a new mandate: the 27th that pays the vendor's next
    unpaid month. If that 27th has already passed (we are in the reminder
    window, or the vendor is overdue) it moves to the next 27th — the
    current month then has to be paid by hand, which the dashboard says."""
    at = at or now()
    due = _as_dt(vendor.get("renews_at")) or next_month_start(at)
    start = debit_date_for(due if _is_month_start(due) else next_month_start(due))
    while start < at + lead:
        start = debit_date_for(next_month_start(start + timedelta(days=5)))
    return start


def _grace_until(plan, renews_at):
    if plan == "monthly":
        # The 1st itself is the last day to pay: access ends at midnight.
        return renews_at + timedelta(days=1)
    return renews_at + timedelta(days=GRACE_DAYS)


def _normalise_monthly(vendor):
    """One-time move of a monthly vendor from the old 30-day cycle onto the
    calendar month: an odd `renews_at` is moved FORWARD to the next 1st, so
    nobody loses days they paid for. Vendors who never paid a plan fee keep
    their date — they owe their first month (owes_first_month)."""
    r = _as_dt(vendor.get("renews_at"))
    if not r or _is_month_start(r) or not _paid_plan_fee(vendor):
        return vendor
    new_r = next_month_start(r)
    fields = {"renews_at": new_r}
    if vendor["status"] == "grace" and now() <= new_r:
        fields.update(status="active", grace_until=None)
    db.update_vendor(vendor["id"], fields)
    db.log_activity("system", "billing_calendar",
                    f"renewal moved from {r:%Y-%m-%d} to the 1st "
                    f"({new_r:%Y-%m-%d})", vendor_id=vendor["id"])
    return db.get_vendor(vendor["id"])


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
def _period_end(plan: str, renews_at, paid_at: datetime):
    if plan == "monthly":
        return monthly_paid_until(renews_at, paid_at)
    period = PLANS[plan]["period_days"]
    if not period:
        return None   # lifetime: never
    base = _as_dt(renews_at)
    if not base or base < paid_at:
        base = paid_at
    return base + timedelta(days=period)


def record_first_payment(vendor, method="razorpay", reference=None):
    """First combined payment (plan fee + installation), CONFIRMED by the
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
    paid_at = now()
    db.update_vendor(vendor["id"], {
        "status": "active",
        "subscribed_at": paid_at,
        "renews_at": _period_end(vendor["plan"], None, paid_at),
        "grace_until": None,
        "login_id": login_id,
        "password_hash": generate_password_hash(password),
        "worker_token": worker_token,
    })
    db.log_activity("admin", "vendor_activated",
                    f"first payment recorded (total {amounts['total']}), "
                    f"credentials generated", vendor_id=vendor["id"])
    return {"login_id": login_id, "password": password, **amounts}


def record_renewal_payment(vendor, method="razorpay", reference=None,
                           amount=None):
    """Renewal payment CONFIRMED by the gateway: extends the subscription one
    period and reactivates the vendor from grace/suspended. A vendor the
    admin disabled stays disabled (the paid period is still extended).

    Idempotent on `reference`: Razorpay redelivers webhooks, and the same
    autopay debit must never extend the subscription twice. Returns the
    amount recorded, or None if this payment was already applied."""
    if reference and db.payment_exists(vendor["id"], "renewal", reference):
        return None
    if amount is None:
        amount = renewal_amount(vendor["plan"])
    db.insert_payment({"vendor_id": vendor["id"], "kind": "renewal",
                       "plan": vendor["plan"], "amount": amount,
                       "status": "paid", "method": method, "reference": reference})
    new_renews = _period_end(vendor["plan"], vendor.get("renews_at"), now())
    fields = {"renews_at": new_renews, "grace_until": None}
    if vendor.get("status") not in ADMIN_DISABLED:
        fields["status"] = "active"
    db.update_vendor(vendor["id"], fields)
    db.log_activity("system", "renewal_paid",
                    f"amount {amount} ({method}); paid until "
                    f"{new_renews:%Y-%m-%d}" if new_renews else
                    f"amount {amount} ({method})",
                    vendor_id=vendor["id"])
    return amount


def refresh_status(vendor):
    """Enforce the subscription state machine (spec §8.3) on a vendor row.

    monthly: active --1st reached--> grace (the 1st only) --> suspended
    yearly : active --renewal date--> grace (15 days)     --> suspended
    A payment moves any of these straight back to active.

    Returns the vendor row with any status change applied.
    """
    if not vendor or vendor["status"] in ("pending_payment",) + ADMIN_DISABLED:
        return vendor
    if vendor["plan"] == "lifetime" or not vendor.get("renews_at"):
        return vendor  # lifetime never expires

    if vendor["plan"] == "monthly":
        vendor = _normalise_monthly(vendor)

    current = now()
    renews_at = _as_dt(vendor["renews_at"])
    if not renews_at:
        return vendor
    grace_until = _grace_until(vendor["plan"], renews_at)

    if vendor["status"] == "active" and current > renews_at:
        new_status = "grace" if current <= grace_until else "suspended"
        db.update_vendor(vendor["id"], {"status": new_status,
                                        "grace_until": grace_until})
        db.log_activity("system", "subscription_" + new_status,
                        f"renewal was due {renews_at:%Y-%m-%d}",
                        vendor_id=vendor["id"])
        vendor = db.get_vendor(vendor["id"])
    elif vendor["status"] == "grace" and current > grace_until:
        db.update_vendor(vendor["id"], {"status": "suspended",
                                        "grace_until": grace_until})
        db.log_activity("system", "subscription_suspended",
                        "not paid by the deadline — service disabled",
                        vendor_id=vendor["id"])
        vendor = db.get_vendor(vendor["id"])
    return vendor


def days_until_renewal(vendor):
    """Whole days until the subscription renews. None for lifetime/unknown,
    negative once it is overdue."""
    if not vendor or vendor.get("plan") == "lifetime":
        return None
    r = _as_dt(vendor.get("renews_at"))
    if not r:
        return None
    return (r.date() - now().date()).days


def sweep_subscriptions():
    """Apply the state machine to EVERY vendor, not just the one being viewed.

    refresh_status() only runs when a vendor's own page or worker touches
    their row, so a shop that stops logging in would never move to grace or
    suspended. This sweep is called from the worker poll, the admin panel
    and the daily billing job so expiry is enforced platform-wide. Returns
    how many rows changed.
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


# ---------------------------------------------------------------------------
# Reminder emails
# ---------------------------------------------------------------------------
def billing_email_due(vendor, at=None):
    """Which billing email (if any) this vendor should get today.

    Returns (kind, due, dedupe_key) or None:
      "reminder"  — 27th..1st, the coming month is unpaid. Daily.
      "suspended" — the deadline passed; sent once per missed due date.
    A healthy mandate is left alone on its own debit day: the debit is in
    flight, and "you haven't paid" would be wrong. If it fails, Razorpay
    marks the mandate pending and the next day's run picks the vendor up.
    """
    if not vendor or vendor.get("plan") != "monthly":
        return None
    if not (vendor.get("email") or "").strip():
        return None
    status = vendor.get("status")
    renews_at = _as_dt(vendor.get("renews_at"))
    if not renews_at:
        return None
    at = at or now()
    today = at.date()

    if status == "suspended":
        return ("suspended", renews_at,
                f"suspended:{renews_at:%Y-%m-%d}")
    if status not in ("active", "grace"):
        return None

    if today.day >= REMINDER_FROM_DAY:
        due = next_month_start(at)
    elif today.day == 1:
        due = datetime(at.year, at.month, 1)
    else:
        return None
    if renews_at > due:
        return None   # the coming month is already paid

    autopay = (vendor.get("autopay_status") or "").lower()
    next_charge = _as_dt(vendor.get("autopay_next_charge"))
    if (autopay in AUTOPAY_HEALTHY and next_charge
            and next_charge.date() == today):
        return None
    return ("reminder", due, f"reminder:{today:%Y-%m-%d}")
