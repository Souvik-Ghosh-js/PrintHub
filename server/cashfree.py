"""Cashfree Payments client (ported from the working prototype's integration).

Used with TWO kinds of accounts:
  * the PLATFORM account (config.PLATFORM_CASHFREE_*) for vendor
    subscription/renewal payments, and
  * each VENDOR's OWN account (vendors.cashfree_* columns) for that shop's
    customer print-job payments — every site has a different Cashfree account.

All functions take the account's credentials explicitly so the same code
serves both.
"""
import base64
import hashlib
import hmac

import requests

API_VERSION = "2022-09-01"


def base_url(env: str) -> str:
    return ("https://api.cashfree.com/pg" if env == "production"
            else "https://sandbox.cashfree.com/pg")


def configured(app_id, secret_key) -> bool:
    return bool(app_id) and bool(secret_key) and not str(app_id).startswith("CHANGE_ME")


def create_order(app_id, secret_key, env, order_id, amount,
                 customer_id, customer_email, customer_phone,
                 return_url, note=""):
    """Create a Cashfree order. Returns
    {success, payment_session_id, order_id} or {success: False, error}."""
    if not configured(app_id, secret_key):
        return {"success": False,
                "error": "Cashfree account is not configured."}
    headers = {
        "Content-Type": "application/json",
        "x-api-version": API_VERSION,
        "x-client-id": app_id,
        "x-client-secret": secret_key,
    }
    try:
        amount_f = round(float(amount), 2)
    except (TypeError, ValueError):
        amount_f = 0.0
    payload = {
        "order_id": order_id,
        "order_amount": amount_f,
        "order_currency": "INR",
        "order_note": note or "PrintHub payment",
        "customer_details": {
            "customer_id": str(customer_id)[:50] or "customer",
            "customer_email": customer_email or "customer@example.com",
            "customer_phone": customer_phone or "9999999999",
        },
        "order_meta": {"return_url": return_url, "notify_url": return_url},
    }
    try:
        resp = requests.post(f"{base_url(env)}/orders", headers=headers,
                             json=payload, timeout=30)
        data = resp.json()
        if resp.status_code == 200:
            return {"success": True,
                    "payment_session_id": data.get("payment_session_id"),
                    "order_id": order_id}
        print(f"[cashfree] create_order error {resp.status_code}: {data}")
        return {"success": False,
                "error": data.get("message", "Unknown error from Cashfree")}
    except Exception as e:
        print(f"[cashfree] create_order exception: {e}")
        return {"success": False, "error": str(e)}


def order_status(app_id, secret_key, env, order_id):
    """Ask Cashfree for an order's authoritative status.
    Returns 'PAID', 'ACTIVE', 'EXPIRED', ... or None on error."""
    if not configured(app_id, secret_key):
        return None
    headers = {
        "x-api-version": API_VERSION,
        "x-client-id": app_id,
        "x-client-secret": secret_key,
    }
    try:
        resp = requests.get(f"{base_url(env)}/orders/{order_id}",
                            headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("order_status")
        print(f"[cashfree] order_status {order_id}: {resp.status_code}")
        return None
    except Exception as e:
        print(f"[cashfree] order_status exception: {e}")
        return None


def verify_webhook(secret, headers, raw_body: str) -> bool:
    """Verify a Cashfree webhook signature against ONE account's secret.

    Accepts both schemes Cashfree has used:
      * new: base64(HMAC-SHA256(timestamp + rawBody))
        with x-webhook-signature + x-webhook-timestamp headers
      * old: hex(HMAC-SHA256(rawBody))
    If the account has no webhook secret configured, accept but warn loudly
    (misconfigured shop keeps working, visibly insecurely).
    """
    if not secret:
        print("[cashfree] WARNING: webhook NOT verified — no webhook secret configured")
        return True
    signature = headers.get("x-webhook-signature", "")
    if not signature:
        return False
    ts = headers.get("x-webhook-timestamp", "")
    new_style = base64.b64encode(hmac.new(
        secret.encode(), (ts + raw_body).encode(),
        hashlib.sha256).digest()).decode()
    old_style = hmac.new(secret.encode(), raw_body.encode(),
                         hashlib.sha256).hexdigest()
    return (hmac.compare_digest(new_style, signature)
            or hmac.compare_digest(old_style, signature))
