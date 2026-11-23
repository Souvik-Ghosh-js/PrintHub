"""Scheduled billing job — run once a day by the printhub-daily systemd timer.

    python jobs.py daily            # sweep statuses, send due emails
    python jobs.py daily --dry-run  # show who would be emailed, send nothing

Each run:
  1. sweep_subscriptions() — moves unpaid shops to grace/suspended on time
     even if nobody opens a page;
  2. emails every monthly vendor whose coming month is unpaid (27th..1st),
     and a one-off notice to any vendor just disabled for not paying;
  3. deletes order files older than config.FILE_RETENTION_DAYS (order
     records stay, only the PDFs on disk go);
  4. removes self sign-ups never paid for config.ABANDONED_SIGNUP_DAYS,
     freeing their shop links.
Safe to run more than once a day: each email is claimed atomically per
vendor per day (db.claim_billing_email), so nobody is mailed twice.
"""
import sys

import billing
import config
import db
import notify


def run_daily(dry_run=False, at=None):
    at = at or billing.now()
    if not dry_run:
        changed = billing.sweep_subscriptions()
        print(f"[daily] sweep: {changed} status change(s)")
    sent = failed = 0
    for v in db.list_vendors():
        if not dry_run:
            v = billing.refresh_status(v)
        due = billing.billing_email_due(v, at)
        if not due:
            continue
        kind, due_date, key = due
        if v.get("last_billing_email") == key:
            continue
        subject, body = notify.billing_email(v, kind, due_date, at.date())
        if dry_run:
            print(f"[daily] would send {kind} to {v['email']} "
                  f"({v['shop_name']}): {subject}")
            continue
        previous = v.get("last_billing_email")
        if not db.claim_billing_email(v["id"], key):
            continue   # another run got there first
        if notify.send(v["email"], subject, body):
            sent += 1
            db.log_activity("system", f"email_{kind}",
                            f"{subject} -> {v['email']}", vendor_id=v["id"])
        else:
            failed += 1
            db.release_billing_email(v["id"], key, previous)
    print(f"[daily] emails sent: {sent}, failed: {failed}")

    if not dry_run:
        try:
            gone = db.purge_abandoned_signups(config.ABANDONED_SIGNUP_DAYS)
            print(f"[daily] removed {len(gone)} unpaid sign-up(s) older than "
                  f"{config.ABANDONED_SIGNUP_DAYS} days" + (f": {', '.join(gone)}" if gone else ""))
        except Exception as e:
            print(f"[daily] sign-up cleanup failed: {e}")

    try:
        files, size = db.purge_old_files(config.FILE_RETENTION_DAYS, dry_run)
        print(f"[daily] {'would delete' if dry_run else 'deleted'} {files} "
              f"order file(s) older than {config.FILE_RETENTION_DAYS} days "
              f"({size / 1e6:.1f} MB); order records kept")
    except Exception as e:   # never let cleanup fail the billing run
        print(f"[daily] file cleanup failed: {e}")
    return sent, failed


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] != "daily":
        print(__doc__)
        sys.exit(2)
    _, failed = run_daily(dry_run="--dry-run" in args)
    sys.exit(1 if failed else 0)
