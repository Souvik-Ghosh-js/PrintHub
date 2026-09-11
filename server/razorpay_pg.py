"""Razorpay client — orders, subscriptions (autopay) and webhook verification.

Used with TWO kinds of accounts:
  * the PLATFORM account (config.PLATFORM_RAZORPAY_*) for vendor
    installation/renewal money, and
  * each VENDOR's OWN account (vendors.razorpay_* columns) for that shop's
    customer print-job payments — every shop has a DIFFERENT account.
Every function takes credentials explicitly so the same code serves both.

Written against requests rather than the `razorpay` SDK: the SDK imports
pkg_resources, which current setuptools no longer ships, and pinning an old
setuptools underneath the payment path is a worse trade than calling four
documented endpoints ourselves.

RUPEES vs PAISE — the one thing to get right:
Razorpay speaks integer paise everywhere (orders, plans, webhook payloads);
PrintHub speaks rupees. The boundary is to_paise()/to_rupees() and NOTHING
else in this file or its callers should multiply by 100. A missed conversion
charges 1/100th or 100x the real amount.
"""
import hashlib
import hmac
import json

import requests

API_BASE = "https://api.razorpay.com/v1"
TIMEOUT = 30

# Above this, UPI Autopay asks the payer for their PIN on every debit, which
# defeats autopay. Our plans are far below it; the guard keeps a future price
# rise from silently breaking renewals.
UPI_AUTOPAY_NO_AFA_LIMIT = 15000


def to_paise(rupees) -> int:
    """Rupees -> integer paise. Rounded, never truncated: float arithmetic
    makes 340.0 * 100 = 33999.999... and int() would bill a paise short."""
    try:
        return int(round(float(rupees) * 100))
    except (TypeError, ValueError):
        return 0


def to_rupees(paise) -> float:
    try:
        return round(int(paise) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def configured(key_id, key_secret) -> bool:
    return (bool(key_id) and bool(key_secret)
            and not str(key_id).startswith("CHANGE_ME"))


def is_test_key(key_id) -> bool:
    """Razorpay test and live accounts are separate key pairs with no mode
    flag, so the key itself is the only signal of which one is in use."""
    return str(key_id or "").startswith("rzp_test_")


def _auth(key_id, key_secret):
    return (str(key_id), str(key_secret))


def _request(method, key_id, key_secret, path, payload=None):
    url = f"{API_BASE}{path}"
    try:
        r = requests.request(method, url, auth=_auth(key_id, key_secret),
                             json=payload, timeout=TIMEOUT)
        data = r.json() if r.content else {}
        if r.status_code in (200, 201):
            return {"success": True, "data": data}
        err = (data.get("error") or {}).get("description") or f"HTTP {r.status_code}"
        print(f"[razorpay] {method} {path} -> {r.status_code}: {err}")
        return {"success": False, "error": err}
    except Exception as e:
        print(f"[razorpay] {method} {path} exception: {e}")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# One-time payments
# ---------------------------------------------------------------------------
def create_order(key_id, key_secret, amount, receipt, notes=None):
    """Create an order for a one-time payment. `amount` is in RUPEES.

    Returns {success, order_id, amount_paise, key_id} — key_id is handed to
    the browser checkout, which never sees the secret.
    """
    if not configured(key_id, key_secret):
        return {"success": False, "error": "Razorpay account is not configured."}
    payload = {
        "amount": to_paise(amount),
        "currency": "INR",
        # Razorpay caps receipt at 40 characters and wants it unique.
        "receipt": str(receipt)[:40],
        "notes": {k: str(v)[:256] for k, v in (notes or {}).items()},
    }
    res = _request("POST", key_id, key_secret, "/orders", payload)
    if not res["success"]:
        return res
    d = res["data"]
    return {"success": True, "order_id": d.get("id"),
            "amount_paise": d.get("amount"), "key_id": key_id}


def fetch_order(key_id, key_secret, order_id):
    """Order status: 'created' | 'attempted' | 'paid' | None on error.
    Only 'paid' means the money is captured."""
    if not configured(key_id, key_secret):
        return None
    res = _request("GET", key_id, key_secret, f"/orders/{order_id}")
    return res["data"].get("status") if res["success"] else None


def fetch_payment(key_id, key_secret, payment_id):
    res = _request("GET", key_id, key_secret, f"/payments/{payment_id}")
    return res["data"] if res["success"] else None


# ---------------------------------------------------------------------------
# Subscriptions (autopay)
# ---------------------------------------------------------------------------
def create_plan(key_id, key_secret, *, amount, period, interval, name):
    """A plan is the recurring price. `amount` in RUPEES."""
    payload = {
        "period": period,          # daily|weekly|monthly|quarterly|yearly
        "interval": int(interval),
        "item": {"name": name[:255], "amount": to_paise(amount),
                 "currency": "INR"},
    }
    res = _request("POST", key_id, key_secret, "/plans", payload)
    if not res["success"]:
        return res
    return {"success": True, "plan_id": res["data"].get("id")}


def create_subscription(key_id, key_secret, *, plan_id, total_count,
                        notes=None, notify=True):
    """Create a subscription the customer then authorises.

    start_at is deliberately OMITTED. Razorpay charges the full plan amount
    at authorisation and keeps it ("In case of immediate start dates, the
    authentication transaction amount is not refunded"), then recurs on its
    own schedule. Setting a future start_at instead would take a ₹5
    auto-refunded validation charge and collect nothing for the first cycle.
    """
    if not configured(key_id, key_secret):
        return {"success": False, "error": "Razorpay account is not configured."}
    payload = {
        "plan_id": plan_id,
        "total_count": int(total_count),
        "quantity": 1,
        "customer_notify": 1 if notify else 0,
        "notes": {k: str(v)[:256] for k, v in (notes or {}).items()},
    }
    res = _request("POST", key_id, key_secret, "/subscriptions", payload)
    if not res["success"]:
        return res
    d = res["data"]
    return {"success": True,
            "subscription_id": d.get("id"),
            "status": d.get("status"),
            "short_url": d.get("short_url"),
            "key_id": key_id}


def fetch_subscription(key_id, key_secret, subscription_id):
    res = _request("GET", key_id, key_secret,
                   f"/subscriptions/{subscription_id}")
    return res["data"] if res["success"] else None


def cancel_subscription(key_id, key_secret, subscription_id,
                        at_cycle_end=False):
    """Stop future debits. Cancellation is final — Razorpay cannot restart a
    cancelled subscription, only create a new one."""
    return _request("POST", key_id, key_secret,
                    f"/subscriptions/{subscription_id}/cancel",
                    {"cancel_at_cycle_end": bool(at_cycle_end)})


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------
# Three different signed strings, and they are easy to mix up:
#   one-time payment : order_id|payment_id          key = API secret
#   subscription auth: payment_id|subscription_id   key = API secret  (reversed!)
#   webhook          : the raw request body         key = WEBHOOK secret
# All HMAC-SHA256, hex digest.
def _hmac_hex(secret, message: str) -> str:
    return hmac.new(str(secret).encode(), message.encode(),
                    hashlib.sha256).hexdigest()


def verify_payment_signature(key_secret, order_id, payment_id, signature):
    if not (key_secret and order_id and payment_id and signature):
        return False
    return hmac.compare_digest(
        _hmac_hex(key_secret, f"{order_id}|{payment_id}"), str(signature))


def verify_subscription_signature(key_secret, subscription_id, payment_id,
                                  signature):
    """NOTE the reversed order versus a one-time payment."""
    if not (key_secret and subscription_id and payment_id and signature):
        return False
    return hmac.compare_digest(
        _hmac_hex(key_secret, f"{payment_id}|{subscription_id}"), str(signature))


def verify_webhook(webhook_secret, headers, raw_body) -> bool:
    """Verify X-Razorpay-Signature over the RAW body.

    The body must be the bytes as received — re-serialising parsed JSON
    changes the whitespace and the signature will never match.

    An account with no webhook secret configured is accepted with a loud
    warning, so a half-configured shop keeps trading rather than silently
    losing every payment; the log makes the insecurity visible.
    """
    if not webhook_secret:
        print("[razorpay] WARNING: webhook NOT verified — no webhook secret set")
        return True
    signature = headers.get("X-Razorpay-Signature", "")
    if not signature:
        return False
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", "replace")
    return hmac.compare_digest(_hmac_hex(webhook_secret, raw_body),
                               str(signature))


# ---------------------------------------------------------------------------
# Webhook events
# ---------------------------------------------------------------------------
EVENT_PAYMENT_CAPTURED = "payment.captured"
EVENT_PAYMENT_FAILED = "payment.failed"
EVENT_ORDER_PAID = "order.paid"
EVENT_SUB_AUTHENTICATED = "subscription.authenticated"
EVENT_SUB_ACTIVATED = "subscription.activated"
EVENT_SUB_CHARGED = "subscription.charged"
# A failed auto-debit arrives as 'pending' (retries still to come), then
# 'halted' once retries are exhausted. There is no subscription.*.failed.
EVENT_SUB_PENDING = "subscription.pending"
EVENT_SUB_HALTED = "subscription.halted"
EVENT_SUB_CANCELLED = "subscription.cancelled"
EVENT_SUB_COMPLETED = "subscription.completed"


def parse_webhook(raw_body):
    """Pull the fields we act on out of any Razorpay webhook."""
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", "replace")
    try:
        body = json.loads(raw_body) if isinstance(raw_body, str) else (raw_body or {})
    except ValueError:
        return {}
    payload = (body or {}).get("payload") or {}
    payment = ((payload.get("payment") or {}).get("entity")) or {}
    sub = ((payload.get("subscription") or {}).get("entity")) or {}
    order = ((payload.get("order") or {}).get("entity")) or {}
    return {
        "event": (body or {}).get("event"),
        "payment_id": payment.get("id"),
        "payment_status": (payment.get("status") or "").lower(),
        "payment_amount": to_rupees(payment.get("amount")) if payment.get("amount") else None,
        # Payments made against an order carry it; subscription charges carry
        # their own internally-generated order id, which we ignore.
        "order_id": payment.get("order_id") or order.get("id"),
        "error_description": payment.get("error_description"),
        "subscription_id": sub.get("id"),
        "subscription_status": (sub.get("status") or "").lower(),
        "charge_at": sub.get("charge_at"),
        "paid_count": sub.get("paid_count"),
        "remaining_count": sub.get("remaining_count"),
    }
