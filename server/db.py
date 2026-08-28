"""Data + storage layer for PrintHub (ported from the prototype's db.py).

MySQL via PyMySQL for vendors / payments / activity_log / print_jobs,
local filesystem for generated PDF storage.

Backend selection (env PRINTHUB_DB = mysql | sqlite | auto, default auto):
in auto mode MySQL is preferred; if it is unreachable we fall back to a local
SQLite file so the whole platform can be tested on a laptop with zero setup.
Production deployments should set PRINTHUB_DB=mysql explicitly.
"""
import os
import sqlite3
from datetime import datetime, timedelta

import pymysql
from pymysql.cursors import DictCursor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Config (env-overridable; defaults suit local development)
# ---------------------------------------------------------------------------
DB_HOST = os.environ.get("PRINTHUB_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("PRINTHUB_DB_PORT", "3306"))
DB_NAME = os.environ.get("PRINTHUB_DB_NAME", "printhub")
DB_USER = os.environ.get("PRINTHUB_DB_USER", "printhub_app")
DB_PASSWORD = os.environ.get("PRINTHUB_DB_PASSWORD", "CHANGE_ME_strong_db_password")

# Where generated PDFs live (server-local disk, same model as the prototype)
UPLOAD_DIR = os.environ.get("PRINTHUB_UPLOADS", os.path.join(BASE_DIR, "uploads"))

# Public base URL of this app (used to build file download URLs for workers)
PUBLIC_BASE_URL = os.environ.get("PRINTHUB_BASE_URL", "http://127.0.0.1:5000")


# ---------------------------------------------------------------------------
# Backend selection (MySQL preferred, SQLite fallback for local testing)
# ---------------------------------------------------------------------------
DB_MODE = os.environ.get("PRINTHUB_DB", "auto")          # mysql | sqlite | auto
SQLITE_PATH = os.environ.get("PRINTHUB_SQLITE",
                             os.path.join(BASE_DIR, "printhub.sqlite"))

sqlite3.register_adapter(datetime, lambda d: d.isoformat(" ", "seconds"))

_backend = None   # resolved on first use


def _resolve_backend():
    global _backend
    if _backend:
        return _backend
    if DB_MODE in ("mysql", "sqlite"):
        _backend = DB_MODE
    else:
        try:
            conn = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                                   password=DB_PASSWORD, database=DB_NAME,
                                   connect_timeout=2)
            conn.close()
            _backend = "mysql"
        except Exception:
            _backend = "sqlite"
            print(f"[db] MySQL unreachable — using local SQLite at "
                  f"{SQLITE_PATH} (set PRINTHUB_DB=mysql to require MySQL)")
    if _backend == "sqlite":
        _init_sqlite()
    return _backend


# SQLite translation of schema.sql (kept in sync by hand; idempotent).
_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, shop_name TEXT NOT NULL, phone TEXT, email TEXT,
    shop_code TEXT NOT NULL UNIQUE,
    login_id TEXT UNIQUE, password_hash TEXT, worker_token TEXT,
    plan TEXT NOT NULL DEFAULT 'monthly',
    status TEXT NOT NULL DEFAULT 'pending_payment',
    subscribed_at TEXT, renews_at TEXT, grace_until TEXT,
    price_bw REAL NOT NULL DEFAULT 0, price_colour REAL NOT NULL DEFAULT 0,
    printer_mode TEXT NOT NULL DEFAULT 'single',
    printer_single TEXT, tray_single TEXT,
    printer_bw TEXT, tray_bw TEXT, printer_colour TEXT, tray_colour TEXT,
    cashfree_app_id TEXT, cashfree_secret_key TEXT,
    cashfree_webhook_secret TEXT,
    cashfree_env TEXT NOT NULL DEFAULT 'production',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL, kind TEXT NOT NULL, plan TEXT,
    amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'paid',
    method TEXT DEFAULT 'manual', reference TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS print_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL, customer_id TEXT, customer_name TEXT,
    doc_format TEXT, file_url TEXT, storage_key TEXT, original_filename TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_payment',
    total_pages INTEGER DEFAULT 1, sides TEXT DEFAULT 'single',
    orientation TEXT DEFAULT 'portrait', color_mode TEXT DEFAULT 'bw',
    paper_size TEXT DEFAULT 'A4', price REAL DEFAULT 0,
    payment_status TEXT NOT NULL DEFAULT 'pending', copies INTEGER DEFAULT 1,
    order_id TEXT, transaction_id TEXT, paid_at TEXT, claimed_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS gateway_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,
    vendor_id INTEGER,
    purpose TEXT NOT NULL,          -- first_payment | renewal | print_job
    amount REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | paid | failed
    transaction_id TEXT,
    meta TEXT,                      -- one-time payload (e.g. generated creds)
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# Columns added after the first release — applied idempotently so an existing
# SQLite file (or a fresh one) always ends up with the full schema.
_SQLITE_MIGRATIONS = [
    "ALTER TABLE vendors ADD COLUMN cashfree_app_id TEXT",
    "ALTER TABLE vendors ADD COLUMN cashfree_secret_key TEXT",
    "ALTER TABLE vendors ADD COLUMN cashfree_webhook_secret TEXT",
    "ALTER TABLE vendors ADD COLUMN cashfree_env TEXT NOT NULL DEFAULT 'production'",
    "ALTER TABLE print_jobs ADD COLUMN order_id TEXT",
    "ALTER TABLE print_jobs ADD COLUMN transaction_id TEXT",
    "ALTER TABLE print_jobs ADD COLUMN paid_at TEXT",
    "ALTER TABLE print_jobs ADD COLUMN claimed_at TEXT",
    "ALTER TABLE print_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE gateway_orders ADD COLUMN meta TEXT",
]


def _init_sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    try:
        conn.executescript(_SQLITE_DDL)
        for mig in _SQLITE_MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def get_conn():
    """A fresh autocommit connection. Cheap enough for this workload."""
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, cursorclass=DictCursor, autocommit=True,
        charset="utf8mb4",
    )


def query(sql, params=None):
    if _resolve_backend() == "sqlite":
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql.replace("%s", "?"), params or ())
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


def execute(sql, params=None):
    if _resolve_backend() == "sqlite":
        conn = sqlite3.connect(SQLITE_PATH)
        try:
            cur = conn.execute(sql.replace("%s", "?"), params or ())
            conn.commit()
            return cur.rowcount, cur.lastrowid
        finally:
            conn.close()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.rowcount, cur.lastrowid
    finally:
        conn.close()


def _insert(table, payload: dict):
    cols = list(payload.keys())
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))})")
    _, new_id = execute(sql, tuple(payload[c] for c in cols))
    return new_id


def _update(table, row_id, fields: dict):
    if not fields:
        return 0
    sets = ", ".join(f"{k} = %s" for k in fields)
    rowcount, _ = execute(f"UPDATE {table} SET {sets} WHERE id = %s",
                          tuple(fields.values()) + (row_id,))
    return rowcount


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
def insert_vendor(payload):
    return _insert("vendors", payload)


def update_vendor(vendor_id, fields):
    return _update("vendors", vendor_id, fields)


def get_vendor(vendor_id):
    rows = query("SELECT * FROM vendors WHERE id = %s", (vendor_id,))
    return rows[0] if rows else None


def get_vendor_by_login(login_id):
    rows = query("SELECT * FROM vendors WHERE login_id = %s", (login_id,))
    return rows[0] if rows else None


def get_vendor_by_code(shop_code):
    rows = query("SELECT * FROM vendors WHERE shop_code = %s", (shop_code,))
    return rows[0] if rows else None


def get_vendor_by_token(worker_token):
    rows = query("SELECT * FROM vendors WHERE worker_token = %s", (worker_token,))
    return rows[0] if rows else None


def list_vendors():
    return query("SELECT * FROM vendors ORDER BY created_at DESC")


# ---------------------------------------------------------------------------
# Payments + activity log
# ---------------------------------------------------------------------------
def insert_payment(payload):
    return _insert("payments", payload)


def list_payments(vendor_id=None, limit=200):
    if vendor_id:
        return query(
            "SELECT p.*, v.shop_name FROM payments p "
            "LEFT JOIN vendors v ON v.id = p.vendor_id "
            "WHERE p.vendor_id = %s ORDER BY p.created_at DESC LIMIT %s",
            (vendor_id, limit))
    return query(
        "SELECT p.*, v.shop_name FROM payments p "
        "LEFT JOIN vendors v ON v.id = p.vendor_id "
        "ORDER BY p.created_at DESC LIMIT %s", (limit,))


def log_activity(actor, action, detail="", vendor_id=None):
    """Activity log (spec §8.1). Never raises — logging must not break flows."""
    try:
        _insert("activity_log", {"vendor_id": vendor_id, "actor": actor,
                                 "action": action, "detail": detail})
    except Exception as e:
        print(f"[activity_log] insert failed: {e}")


def list_activity(vendor_id=None, limit=300):
    if vendor_id:
        return query(
            "SELECT a.*, v.shop_name FROM activity_log a "
            "LEFT JOIN vendors v ON v.id = a.vendor_id "
            "WHERE a.vendor_id = %s ORDER BY a.created_at DESC LIMIT %s",
            (vendor_id, limit))
    return query(
        "SELECT a.*, v.shop_name FROM activity_log a "
        "LEFT JOIN vendors v ON v.id = a.vendor_id "
        "ORDER BY a.created_at DESC LIMIT %s", (limit,))


# ---------------------------------------------------------------------------
# Print jobs
# ---------------------------------------------------------------------------
def insert_job(payload):
    return _insert("print_jobs", payload)


def update_job(job_id, fields):
    return _update("print_jobs", job_id, fields)


def get_job(job_id):
    rows = query("SELECT * FROM print_jobs WHERE id = %s", (job_id,))
    return rows[0] if rows else None


# A job is handed to a worker at most this many times. Beyond it the job is
# parked as 'needs_attention' instead of being served again: a customer must
# never receive an endless stream of duplicate prints because of a bug or a
# misbehaving shop PC.
MAX_PRINT_ATTEMPTS = 3


def claim_job(job_id):
    """Atomically hand a confirmed job to a worker exactly once.

    The UPDATE only matches while the row is still 'confirmed', so two
    concurrent polls can never both take it. Returns 1 if claimed, 0 if
    somebody already has it or it has exhausted its attempts.
    """
    rowcount, _ = execute(
        "UPDATE print_jobs "
        "   SET status = 'printing', claimed_at = %s, "
        "       attempts = COALESCE(attempts, 0) + 1 "
        " WHERE id = %s AND status = 'confirmed' "
        "   AND COALESCE(attempts, 0) < %s",
        (datetime.now(), job_id, MAX_PRINT_ATTEMPTS))
    return rowcount


def park_exhausted_jobs():
    """Take jobs that have used up their attempts out of the queue so they
    stop being re-served (and re-printed). The vendor sees them as needing
    attention and can reprint deliberately."""
    rowcount, _ = execute(
        "UPDATE print_jobs SET status = 'needs_attention', claimed_at = NULL "
        " WHERE status = 'confirmed' AND COALESCE(attempts, 0) >= %s",
        (MAX_PRINT_ATTEMPTS,))
    return rowcount


def release_stale_claims(max_age_seconds=180):
    """Return jobs whose worker went away mid-print back to the queue, so a
    crash or power cut doesn't strand a paid job forever."""
    cutoff = datetime.now() - timedelta(seconds=max_age_seconds)
    rowcount, _ = execute(
        "UPDATE print_jobs SET status = 'confirmed' "
        "WHERE status = 'printing' AND claimed_at IS NOT NULL "
        "AND claimed_at < %s", (cutoff,))
    return rowcount


def get_jobs_by_order(order_id):
    return query("SELECT * FROM print_jobs WHERE order_id = %s", (order_id,))


def update_jobs_by_order(order_id, fields):
    if not fields:
        return 0
    sets = ", ".join(f"{k} = %s" for k in fields)
    rowcount, _ = execute(
        f"UPDATE print_jobs SET {sets} WHERE order_id = %s",
        tuple(fields.values()) + (order_id,))
    return rowcount


def get_vendor_jobs(vendor_id, status=None, limit=100):
    if status:
        return query(
            "SELECT * FROM print_jobs WHERE vendor_id = %s AND status = %s "
            "ORDER BY created_at ASC LIMIT %s", (vendor_id, status, limit))
    return query(
        "SELECT * FROM print_jobs WHERE vendor_id = %s "
        "ORDER BY created_at DESC LIMIT %s", (vendor_id, limit))


# ---------------------------------------------------------------------------
# Platform statistics (shown on the public landing page)
# ---------------------------------------------------------------------------
def platform_stats():
    """Live numbers for the marketing page — never invented.

    Returns shops (vendors able to take orders), pages actually printed,
    documents printed, and the money customers have paid shop owners.
    """
    out = {"shops": 0, "pages": 0, "documents": 0, "paid_to_shops": 0.0}
    try:
        r = query("SELECT COUNT(*) AS n FROM vendors WHERE status IN "
                  "('active','grace')")
        out["shops"] = int(r[0]["n"]) if r else 0
    except Exception:
        pass
    try:
        r = query("SELECT COUNT(*) AS docs, "
                  "       COALESCE(SUM(total_pages * copies), 0) AS pages, "
                  "       COALESCE(SUM(price), 0) AS paid "
                  "  FROM print_jobs WHERE status = 'printed'")
        if r:
            out["documents"] = int(r[0]["docs"] or 0)
            out["pages"] = int(r[0]["pages"] or 0)
            out["paid_to_shops"] = float(r[0]["paid"] or 0)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Gateway orders (one row per Cashfree order; drives webhook/return dispatch)
# ---------------------------------------------------------------------------
def insert_gateway_order(payload):
    return _insert("gateway_orders", payload)


def get_gateway_order(order_id):
    rows = query("SELECT * FROM gateway_orders WHERE order_id = %s", (order_id,))
    return rows[0] if rows else None


def update_gateway_order(order_id, fields):
    if not fields:
        return 0
    sets = ", ".join(f"{k} = %s" for k in fields)
    rowcount, _ = execute(
        f"UPDATE gateway_orders SET {sets} WHERE order_id = %s",
        tuple(fields.values()) + (order_id,))
    return rowcount


# ---------------------------------------------------------------------------
# File storage (local disk)
# ---------------------------------------------------------------------------
def storage_save(filename, data: bytes):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
        f.write(data)
    return filename


def storage_path(filename):
    return os.path.join(UPLOAD_DIR, filename)


def storage_remove(filename):
    """Best-effort delete; must never break the calling flow (e.g. Windows
    holds the file open while it is still being served)."""
    try:
        os.remove(os.path.join(UPLOAD_DIR, filename))
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        print(f"[storage] could not remove {filename}: {e}")
        return False


def public_url(storage_key, worker_token):
    """URL the vendor's worker uses to download a job file (token-guarded)."""
    return f"{PUBLIC_BASE_URL}/files/{storage_key}?token={worker_token}"
