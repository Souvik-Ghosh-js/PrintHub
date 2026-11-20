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
