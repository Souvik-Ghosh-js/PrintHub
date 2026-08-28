"""Cashfree Subscriptions — autopay for vendor renewals.

Vendors on a monthly or yearly plan authorise a UPI Autopay / e-NACH / card
mandate once; Cashfree then debits each cycle on its own (plan_type PERIODIC)
and tells us over webhooks. We never schedule or raise the charge ourselves,
and we never store a mandate — Cashfree holds it.

Verified against the official OpenAPI spec (v2026-01-01):
  POST /plans                          create a plan
  POST /subscriptions                  create a subscription -> session id
  GET  /subscriptions/{id}             status
  POST /subscriptions/{id}/manage      CANCEL / PAUSE / ACTIVATE
Same host, credentials and webhook-signature scheme as the Orders API, but a
DIFFERENT x-api-version, so it is pinned separately here.
"""
import json

import requests

import cashfree

# Subscriptions live on the same PG host but require this API version.
SUBS_API_VERSION = "2026-01-01"

# UPI Autopay debits above this need additional factor authentication each
# cycle, which defeats the point of autopay. Our plans are far below it, but
# the guard keeps a future price rise from silently breaking renewals.
UPI_AUTOPAY_NO_AFA_LIMIT = 15000


def _headers(app_id, secret_key):
    return {
        "Content-Type": "application/json",
        "x-api-version": SUBS_API_VERSION,
        "x-client-id": app_id,
        "x-client-secret": secret_key,
    }


def _post(app_id, secret_key, env, path, payload):
    url = f"{cashfree.base_url(env)}{path}"
    try:
        r = requests.post(url, headers=_headers(app_id, secret_key),
                          json=payload, timeout=30)
        data = r.json() if r.content else {}
        if r.status_code in (200, 201):
            return {"success": True, "data": data}
        print(f"[cashfree-subs] POST {path} -> {r.status_code}: {data}")
        return {"success": False,
                "error": data.get("message") or f"HTTP {r.status_code}"}
    except Exception as e:
        print(f"[cashfree-subs] POST {path} exception: {e}")
        return {"success": False, "error": str(e)}


def _get(app_id, secret_key, env, path):
    url = f"{cashfree.base_url(env)}{path}"
    try:
        r = requests.get(url, headers=_headers(app_id, secret_key), timeout=30)
        data = r.json() if r.content else {}
        if r.status_code == 200:
            return {"success": True, "data": data}
        print(f"[cashfree-subs] GET {path} -> {r.status_code}: {data}")
        return {"success": False,
                "error": data.get("message") or f"HTTP {r.status_code}"}
    except Exception as e:
        print(f"[cashfree-subs] GET {path} exception: {e}")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Creating the mandate
# ---------------------------------------------------------------------------
def create_subscription(app_id, secret_key, env, *, subscription_id, amount,
                        interval_type, intervals, customer_email,
                        customer_phone, return_url, first_charge_time=None,
                        note=""):
    """Create a PERIODIC subscription and return its session id.

    The session id is handed to the Cashfree checkout SDK, where the vendor
    approves the mandate in their UPI app. After that Cashfree debits every
    cycle by itself.

    interval_type: "MONTH" or "YEAR"; intervals: how many of them per cycle.
    """
    if not cashfree.configured(app_id, secret_key):
        return {"success": False, "error": "Cashfree is not configured."}
    amount = round(float(amount), 2)

    payload = {
        "subscription_id": subscription_id,
        "customer_details": {
            "customer_email": customer_email or "vendor@example.com",
            "customer_phone": customer_phone or "9999999999",
        },
        "plan_details": {
            "plan_type": "PERIODIC",          # Cashfree charges automatically
            "plan_currency": "INR",
            "plan_amount": amount,
            # Headroom so a later price rise does not need a new mandate.
            "plan_max_amount": max(amount * 3, amount + 1000),
            "plan_interval_type": interval_type,
            "plan_intervals": int(intervals),
        },
        "authorization_details": {
            # A token debit to prove the mandate works; refunded automatically.
            "authorization_amount": 1,
            "authorization_amount_refund": True,
            "payment_methods": ["upi", "enach", "card"],
        },
        "subscription_meta": {"return_url": return_url},
        "subscription_note": (note or "PrintHub subscription")[:200],
    }
    if first_charge_time:
        payload["subscription_first_charge_time"] = first_charge_time

    res = _post(app_id, secret_key, env, "/subscriptions", payload)
    if not res["success"]:
        return res
    d = res["data"]
    return {
        "success": True,
        "subscription_id": d.get("subscription_id", subscription_id),
        "cf_subscription_id": d.get("cf_subscription_id"),
        "session_id": d.get("subscription_session_id"),
        "status": d.get("subscription_status"),
    }


def fetch_subscription(app_id, secret_key, env, subscription_id):
    res = _get(app_id, secret_key, env, f"/subscriptions/{subscription_id}")
    return res["data"] if res["success"] else None


def cancel_subscription(app_id, secret_key, env, subscription_id):
    """Stop future debits. There is no DELETE — cancellation is an action on
    /manage."""
    return _post(app_id, secret_key, env,
                 f"/subscriptions/{subscription_id}/manage",
                 {"action": "CANCEL"})


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
# Events we act on. Cashfree sends several others (reminders, notifications)
# which we acknowledge and ignore.
EVENT_AUTH = "SUBSCRIPTION_AUTH_STATUS"
EVENT_PAID = "SUBSCRIPTION_PAYMENT_SUCCESS"
EVENT_FAILED = "SUBSCRIPTION_PAYMENT_FAILED"
EVENT_STATUS = "SUBSCRIPTION_STATUS_CHANGED"


def parse_webhook(body):
    """Pull the fields we care about out of a subscription webhook."""
    if isinstance(body, (bytes, str)):
        try:
            body = json.loads(body)
        except ValueError:
            return {}
    data = (body or {}).get("data") or {}
    subs = data.get("subscription_details") or {}
    auth = data.get("authorization_details") or {}
    return {
        "type": (body or {}).get("type"),
        "subscription_id": data.get("subscription_id") or subs.get("subscription_id"),
        "payment_id": data.get("payment_id") or data.get("cf_payment_id"),
        "payment_status": (data.get("payment_status") or "").upper(),
        "payment_amount": data.get("payment_amount"),
        "authorization_status": (auth.get("authorization_status") or "").upper(),
        "subscription_status": (subs.get("subscription_status") or "").upper(),
        "next_schedule_date": subs.get("next_schedule_date"),
        "failure_reason": ((data.get("failure_details") or {})
                           .get("failure_reason")),
    }
