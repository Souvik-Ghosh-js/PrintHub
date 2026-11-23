"""Outgoing email — billing reminders and service-disabled notices.

Plain SMTP (config.SMTP_*), so any provider works. With SMTP_HOST unset,
send() logs the message and reports failure, and the daily job leaves the
vendor unmarked so the email goes out once SMTP is configured.
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import config
import db


def configured() -> bool:
    # A login user without its password (not yet set on this server) would
    # fail every send, so treat it as "email off" and say so in the log.
    return bool(config.SMTP_HOST and config.SMTP_FROM
                and (config.SMTP_PASSWORD or not config.SMTP_USER))


def send(to, subject, body) -> bool:
    if not configured():
        print(f"[mail] SMTP not configured — NOT sent to {to}: {subject}")
        return False
    msg = EmailMessage()
    msg["From"] = formataddr((config.SMTP_FROM_NAME, config.SMTP_FROM))
    msg["Reply-To"] = config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=config.SMTP_FROM.split("@")[-1])
    msg.set_content(body)
    try:
        if config.SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                      context=ssl.create_default_context(),
                                      timeout=30)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        with server:
            if config.SMTP_SECURITY == "starttls":
                server.starttls(context=ssl.create_default_context())
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[mail] send to {to} failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Billing emails
# ---------------------------------------------------------------------------
def _dashboard_url():
    return f"{db.PUBLIC_BASE_URL.rstrip('/')}/vendor/login"


def welcome_email(vendor, creds):
    """(subject, body) sent once, when a shop's first payment activates it:
    the generated Login ID + password and everything needed to go live.

    vendor: the row after activation (plan, renews_at, shop_code, ...).
    creds:  what billing.record_first_payment returned (login_id, password,
            total, subscription_fee, installation_fee).
    """
    brand = config.SMTP_FROM_NAME
    shop = vendor.get("shop_name") or "your shop"
    base = db.PUBLIC_BASE_URL.rstrip("/")
    plan = vendor.get("plan") or "monthly"
    plan_label = config.PLANS.get(plan, {}).get("label", plan.title())
    renews = vendor.get("renews_at")
    if plan == "lifetime" or not renews:
        renewal = "Lifetime — no renewals."
    else:
        import billing                      # MySQL gives a datetime, SQLite text
        try:
            due = billing._as_dt(renews)
        except Exception:
            due = None
        when = f"{due.day} {due:%B %Y}" if due else str(renews)[:10]
        fee = config.PLANS[plan]["fee"]
        renewal = f"Next payment of ₹{fee:,} is due by {when}."
        if plan == "monthly":
            renewal += (f" Turn on autopay in your dashboard and it is paid "
                        f"automatically on the {config.AUTOPAY_DEBIT_DAY}th.")
    subject = f"{brand}: welcome — your PrintHub login for {shop}"
    body = (
        f"Hello {vendor.get('name') or shop},\n\n"
        f"Thank you — your payment was received and {shop} is now active on "
        f"PrintHub.\n\n"
        f"YOUR LOGIN\n"
        f"  Login ID : {creds['login_id']}\n"
        f"  Password : {creds['password']}\n"
        f"  Sign in  : {_dashboard_url()}\n"
        f"Use the same Login ID and password in the PrintHub app on your shop "
        f"PC. Keep them safe and don't share them.\n\n"
        f"YOUR SUBSCRIPTION\n"
        f"  Plan     : {plan_label}\n"
        f"  Paid     : ₹{creds.get('total', 0):,} (₹{creds.get('subscription_fee', 0):,} "
        f"plan + ₹{creds.get('installation_fee', 0):,} one-time setup)\n"
        f"  Renewal  : {renewal}\n\n"
        f"YOUR SHOP PAGE\n"
        f"  {base}/{vendor.get('shop_code')}\n"
        f"  Customers open this by scanning your QR code.\n\n"
        f"GET READY TO TAKE ORDERS\n"
        f"  1. Connect your own Razorpay account (dashboard → Payments).\n"
        f"     Your customers pay straight into it; PrintHub takes nothing.\n"
        f"     No account yet? Create one free at https://razorpay.com,\n"
        f"     then copy your API Key ID and Key Secret into the dashboard.\n"
        f"  2. Set your per-page prices for Black & White and Colour.\n"
        f"  3. Download the PrintHub app from your dashboard onto the PC\n"
        f"     connected to your printers, sign in, and choose your printers.\n"
        f"  4. Print your counter QR poster from the dashboard and put it up.\n"
        f"Your shop starts taking orders once steps 1 and 2 are done.\n"
    )
    return subject, body + _signature()


def _signature():
    lines = ["", "Regards,", config.SMTP_FROM_NAME]
    phones = ", ".join(p["label"] for p in config.CONTACT_PHONES)
    if phones:
        lines.append(f"Questions? Call or WhatsApp us: {phones}")
    return "\n".join(lines)


def billing_email(vendor, kind, due, today):
    """(subject, body) for a reminder or a service-disabled notice."""
    fee = config.PLANS["monthly"]["fee"]
    brand = config.SMTP_FROM_NAME
    shop = vendor.get("shop_name") or "your shop"
    month = due.strftime("%B %Y")
    deadline = f"{due.day} {due:%B}"
    autopay = (vendor.get("autopay_status") or "").lower()
    link = _dashboard_url()

    if kind == "suspended":
        subject = f"{brand}: {shop} is offline — payment not received"
        body = (
            f"Hello {vendor.get('name') or shop},\n\n"
            f"We did not receive your {brand} monthly fee of ₹{fee}, which "
            f"was due by {deadline}. {shop} has been disabled: customers "
            f"cannot place orders and the shop app will not print.\n\n"
            f"Pay ₹{fee} from your dashboard and the shop comes back online "
            f"immediately:\n{link}\n"
        )
        return subject, body + _signature()

    last_day = today == due.date()
    days_left = (due.date() - today).days
    if last_day:
        subject = f"{brand}: last day to pay ₹{fee} and keep {shop} online"
        when = "today is the last day to pay"
    else:
        subject = (f"{brand}: ₹{fee} due by {deadline} for {shop} "
                   f"({days_left} day{'s' if days_left != 1 else ''} left)")
        when = f"it is due by {deadline}"

    if autopay == "pending":
        status_line = (
            f"Your autopay debit of ₹{fee} did not go through. Razorpay will "
            f"try again, but to be safe please check your UPI/bank account "
            f"or pay now from your dashboard.")
    elif autopay == "halted":
        status_line = (
            f"Your autopay stopped after several failed debits, so it will "
            f"not pay this month. Please pay ₹{fee} now and turn autopay on "
            f"again from your dashboard.")
    elif autopay in ("active", "authenticated"):
        status_line = (
            f"Your autopay has not completed this month's debit yet. If it "
            f"does not go through, please pay from your dashboard.")
    else:
        status_line = (
            f"You don't have autopay on, so this month has to be paid by hand. "
            f"Turn on autopay from your dashboard and ₹{fee} is debited "
            f"automatically on the {config.AUTOPAY_DEBIT_DAY}th of each month.")

    body = (
        f"Hello {vendor.get('name') or shop},\n\n"
        f"Your {brand} monthly fee of ₹{fee} for {month} has not been paid "
        f"yet — {when}.\n\n"
        f"{status_line}\n\n"
        f"If it is not paid by the end of {deadline}, {shop} will be "
        f"disabled until payment is made.\n\n"
        f"Pay now: {link}\n"
    )
    return subject, body + _signature()
