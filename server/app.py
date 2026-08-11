"""PrintHub — multi-vendor document scanning & printing platform.

Route groups:
  Customer   /shop/<code>, /api/detect_document, /api/enhance_page, orders
  Vendor     /vendor/login, /vendor (dashboard), pricing + printer config
  Worker API /worker/api/* (desktop print worker, token-guarded)
  Admin      /admin/* (Super Admin panel: vendors, payments, activity, enforcement)

Core scanning/composing logic is ported from the working prototype
(printer/printer) — see compose_id_card_pdf / build_pdf_from_images there.
"""
import io
import json
import os
import uuid
from functools import wraps

from flask import (Flask, request, jsonify, render_template, send_file,
                   session, redirect, url_for, flash)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from PIL import Image

import billing
import config
import db
import docscan

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


# ===========================================================================
# Auth decorators
# ===========================================================================
def vendor_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        vid = session.get("vendor_id")
        if not vid:
            return redirect(url_for("vendor_login"))
        vendor = billing.refresh_status(db.get_vendor(vid))
        if not vendor:
            session.pop("vendor_id", None)
            return redirect(url_for("vendor_login"))
        return f(vendor, *args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def worker_vendor():
    """Resolve the vendor from a worker request's token (query or header)."""
    token = request.args.get("token") or request.headers.get("X-Worker-Token")
    if not token:
        return None
    return billing.refresh_status(db.get_vendor_by_token(token))


# ===========================================================================
# Health
# ===========================================================================
@app.route("/health")
def health():
    status = {"app": "ok", "db": "ok"}
    code = 200
    try:
        db.query("SELECT 1")
    except Exception as e:
        status["db"] = "error"
        status["db_error"] = str(e)
        code = 503
    status["detector"] = "ml" if os.path.exists(docscan.DOCUNET_ONNX) else "classical"
    return jsonify(status), code


# ===========================================================================
# Customer: shop pages + scanning APIs
# ===========================================================================
@app.route("/")
def index():
    """Landing: list of shops customers can print at (active or in grace)."""
    vendors = [billing.refresh_status(v) for v in db.list_vendors()]
    shops = [v for v in vendors if billing.has_access(v)]
    return render_template("landing.html", shops=shops)


@app.route("/shop/<code>")
def shop(code):
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return render_template("landing.html", shops=[],
                               error="This shop is not available right now."), 404
    return render_template("customer.html", vendor=vendor,
                           formats=config.DOC_FORMATS)


@app.route("/api/detect_document", methods=["POST"])
def detect_document():
    """Auto Precise Cut (spec §3.1): return document corners for an image so
    the frontend pre-fills the Cropper.js box. ok=false => keep default box."""
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "reason": "no file"}), 400
    try:
        bgr = docscan.decode_image(f.read())
        if bgr is None:
            return jsonify({"ok": False, "reason": "could not decode image"}), 400
        corners, conf, detector = docscan.detect_corners(bgr)
        print(f"[detect_document] detector={detector} confidence={conf:.2f}")
        if conf < docscan.DETECT_MIN_CONFIDENCE:
            return jsonify({"ok": False, "reason": "low confidence",
                            "detector": detector,
                            "confidence": round(float(conf), 3)})
        import numpy as np
        return jsonify({"ok": True, "detector": detector,
                        "confidence": round(float(conf), 3),
                        "corners": np.asarray(corners).round(1).tolist()})
    except Exception as e:
        print(f"[detect_document] error: {e}")
        return jsonify({"ok": False, "reason": str(e)}), 500


@app.route("/api/enhance_page", methods=["POST"])
def enhance_page():
    """Light/Burn filter preview (spec §3.1): returns the enhanced JPEG."""
    f = request.files.get("file")
    mode = request.form.get("mode", "color")
    if not f:
        return jsonify({"error": "no file"}), 400
    try:
        bgr = docscan.decode_image(f.read())
        if bgr is None:
            return jsonify({"error": "could not decode image"}), 400
        out = docscan.enhance_image(bgr, mode)
        return send_file(io.BytesIO(docscan.encode_jpeg(out)),
                         mimetype="image/jpeg")
    except Exception as e:
        print(f"[enhance_page] error: {e}")
        return jsonify({"error": str(e)}), 500


# --- ID-card compose (ported from the prototype, extended for single page) ---
def compose_id_pdf(sides, layout, enhance_mode):
    """Compose 1 or 2 card sides on one A4 portrait page at REAL card size
    (ISO ID-1: 85.6mm x 54mm).

    sides: list of raw image bytes (already cropped client-side), 1 or 2 items.
    layout: 'side_by_side' | 'stacked'. A single side is centred (spec §2).
    Returns PDF bytes.
    """
    DPI = 300
    A4_W, A4_H = int(8.27 * DPI), int(11.69 * DPI)
    GAP = int(0.3 * DPI)
    MM = DPI / 25.4
    CARD_W, CARD_H = int(85.6 * MM), int(54.0 * MM)

    cards = []
    for data in sides:
        bgr = docscan.decode_image(data)
        if bgr is None:
            raise ValueError("could not decode a page image")
        bgr = docscan.enhance_image(bgr, enhance_mode)
        rgb = bgr[:, :, ::-1]  # BGR -> RGB for PIL
        img = Image.fromarray(rgb).resize((CARD_W, CARD_H), Image.LANCZOS)
        cards.append(img)

    canvas = Image.new("RGB", (A4_W, A4_H), "white")
    if len(cards) == 1:
        # Auto-centre on single upload (spec §2).
        canvas.paste(cards[0], ((A4_W - CARD_W) // 2, (A4_H - CARD_H) // 2))
    elif layout == "side_by_side":
        total_w = CARD_W * 2 + GAP
        x = (A4_W - total_w) // 2
        y = (A4_H - CARD_H) // 2
        canvas.paste(cards[0], (x, y))
        canvas.paste(cards[1], (x + CARD_W + GAP, y))
    else:  # stacked (PAN)
        total_h = CARD_H * 2 + GAP
        x = (A4_W - CARD_W) // 2
        y = (A4_H - total_h) // 2
        canvas.paste(cards[0], (x, y))
        canvas.paste(cards[1], (x, y + CARD_H + GAP))

    out = io.BytesIO()
    canvas.save(out, format="PDF", resolution=DPI)
    return out.getvalue()


def build_document_pdf(files, enhance_mode):
    """Multi-page scan -> one A4-fit PDF (ported from the prototype)."""
    DPI = 200
    A4_W, A4_H = int(8.27 * DPI), int(11.69 * DPI)
    MARGIN = int(0.3 * DPI)

    pages = []
    for f in files:
        bgr = docscan.decode_image(f.read())
        if bgr is None:
            raise ValueError("could not decode a page image")
        bgr = docscan.enhance_image(bgr, enhance_mode)
        img = Image.fromarray(bgr[:, :, ::-1])
        max_w, max_h = A4_W - 2 * MARGIN, A4_H - 2 * MARGIN
        scale = min(max_w / img.width, max_h / img.height)
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
        page = Image.new("RGB", (A4_W, A4_H), "white")
        page.paste(img, ((A4_W - img.width) // 2, (A4_H - img.height) // 2))
        pages.append(page)
    if not pages:
        raise ValueError("no pages")
    out = io.BytesIO()
    pages[0].save(out, format="PDF", resolution=DPI,
                  save_all=True, append_images=pages[1:])
    return out.getvalue()


def _order_pdf_from_request(doc_format):
    """Build the order PDF + page count from the uploaded form files."""
    enhance_mode = request.form.get("enhance_mode", "none")
    if doc_format in config.DOC_FORMATS:
        sides = []
        for field in ("front", "back"):
            f = request.files.get(field)
            if f:
                sides.append(f.read())
        if not sides:
            raise ValueError("upload at least one side")
        layout = config.DOC_FORMATS[doc_format]["layout"]
        return compose_id_pdf(sides, layout, enhance_mode), 1
    # Generic multi-page document scan.
    files = request.files.getlist("pages")
    if not files:
        raise ValueError("no pages uploaded")
    return build_document_pdf(files, enhance_mode), len(files)


@app.route("/shop/<code>/preview", methods=["POST"])
def shop_preview(code):
    """Return the composed PDF so the customer can confirm before ordering."""
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return jsonify({"error": "shop unavailable"}), 404
    try:
        pdf_bytes, total_pages = _order_pdf_from_request(
            request.form.get("doc_format", "document"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    resp = send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    resp.headers["X-Total-Pages"] = str(total_pages)
    return resp


@app.route("/shop/<code>/order", methods=["POST"])
def shop_order(code):
    """Create a print job at this vendor. Price = vendor's per-page price
    (spec §6.5) x pages x copies. Payment at the counter in v1."""
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return jsonify({"error": "shop unavailable"}), 404

    doc_format = request.form.get("doc_format", "document")
    color_mode = request.form.get("color_mode", "bw")
    copies = max(1, int(request.form.get("copies", 1)))
    customer_name = request.form.get("customer_name", "")[:250]

    try:
        pdf_bytes, total_pages = _order_pdf_from_request(doc_format)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    per_page = float(vendor["price_colour"] if color_mode == "color"
                     else vendor["price_bw"])
    price = round(per_page * total_pages * copies, 2)

    label = config.DOC_FORMATS.get(doc_format, {}).get("label", "Document")
    storage_key = f"{uuid.uuid4().hex}.pdf"
    db.storage_save(storage_key, pdf_bytes)
    job_id = db.insert_job({
        "vendor_id": vendor["id"],
        "customer_id": request.form.get("customer_id") or uuid.uuid4().hex[:12],
        "customer_name": customer_name,
        "doc_format": doc_format,
        "storage_key": storage_key,
        "file_url": db.public_url(storage_key, vendor["worker_token"]),
        "original_filename": f"{label} - {customer_name or 'customer'}.pdf",
        "status": "confirmed",          # ready for the shop's worker to print
        "total_pages": total_pages,
        "color_mode": color_mode,
        "copies": copies,
        "price": price,
        "payment_status": "counter",
    })
    db.log_activity("customer", "job_created",
                    f"job {job_id}: {label}, {color_mode}, x{copies}, ₹{price}",
                    vendor_id=vendor["id"])
    return jsonify({"ok": True, "job_id": job_id, "price": price,
                    "total_pages": total_pages})


# ===========================================================================
# Files (worker downloads; token must belong to the job's vendor)
# ===========================================================================
@app.route("/files/<path:storage_key>")
def serve_file(storage_key):
    vendor = worker_vendor()
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    safe = secure_filename(storage_key)
    rows = db.query("SELECT vendor_id FROM print_jobs WHERE storage_key = %s",
                    (safe,))
    if not rows or rows[0]["vendor_id"] != vendor["id"]:
        return jsonify({"error": "not found"}), 404
    path = db.storage_path(safe)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    return send_file(path)


# ===========================================================================
# Vendor portal (web)
# ===========================================================================
@app.route("/vendor/login", methods=["GET", "POST"])
def vendor_login():
    if request.method == "POST":
        login_id = request.form.get("login_id", "").strip()
        password = request.form.get("password", "")
        vendor = db.get_vendor_by_login(login_id)
        if (vendor and vendor.get("password_hash")
                and check_password_hash(vendor["password_hash"], password)):
            vendor = billing.refresh_status(vendor)
            if vendor["status"] == "rejected":
                flash("Your subscription was rejected. Contact support to "
                      "clear payment and restore access.")
            else:
                session["vendor_id"] = vendor["id"]
                db.log_activity("vendor", "login", "web portal",
                                vendor_id=vendor["id"])
                return redirect(url_for("vendor_dashboard"))
        else:
            flash("Invalid login ID or password.")
    return render_template("vendor_login.html")


@app.route("/vendor/logout")
def vendor_logout():
    session.pop("vendor_id", None)
    return redirect(url_for("vendor_login"))


@app.route("/vendor")
@vendor_required
def vendor_dashboard(vendor):
    jobs = db.get_vendor_jobs(vendor["id"], limit=50)
    payments = db.list_payments(vendor_id=vendor["id"], limit=20)
    return render_template("vendor_dashboard.html", vendor=vendor, jobs=jobs,
                           payments=payments, plans=config.PLANS)


@app.route("/vendor/pricing", methods=["POST"])
@vendor_required
def vendor_pricing(vendor):
    """Vendor pricing control (spec §6.5): B/W + colour per-page prices."""
    try:
        price_bw = round(float(request.form.get("price_bw", 0)), 2)
        price_colour = round(float(request.form.get("price_colour", 0)), 2)
        if price_bw < 0 or price_colour < 0:
            raise ValueError
    except ValueError:
        flash("Prices must be non-negative numbers.")
        return redirect(url_for("vendor_dashboard"))
    db.update_vendor(vendor["id"], {"price_bw": price_bw,
                                    "price_colour": price_colour})
    db.log_activity("vendor", "pricing_changed",
                    f"B/W ₹{price_bw}, Colour ₹{price_colour}",
                    vendor_id=vendor["id"])
    flash("Prices updated.")
    return redirect(url_for("vendor_dashboard"))


# ===========================================================================
# Worker API (desktop app)
# ===========================================================================
def _printer_config(vendor):
    return {k: vendor.get(k) for k in (
        "printer_mode", "printer_single", "tray_single",
        "printer_bw", "tray_bw", "printer_colour", "tray_colour")}


@app.route("/worker/api/login", methods=["POST"])
def worker_login():
    data = request.get_json(silent=True) or {}
    vendor = db.get_vendor_by_login(data.get("login_id", "").strip())
    if not (vendor and vendor.get("password_hash")
            and check_password_hash(vendor["password_hash"],
                                    data.get("password", ""))):
        return jsonify({"error": "invalid credentials"}), 401
    vendor = billing.refresh_status(vendor)
    if not billing.has_access(vendor):
        return jsonify({"error": f"subscription {vendor['status']}"}), 403
    db.log_activity("worker", "login", "desktop worker", vendor_id=vendor["id"])
    return jsonify({"token": vendor["worker_token"],
                    "shop_name": vendor["shop_name"],
                    "status": vendor["status"],
                    "printer_config": _printer_config(vendor)})


@app.route("/worker/api/jobs")
def worker_jobs():
    vendor = worker_vendor()
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    if not billing.has_access(vendor):
        # Suspended/rejected vendors lose access until payment clears (§8.3).
        return jsonify({"error": f"subscription {vendor['status']}"}), 403
    jobs = db.get_vendor_jobs(vendor["id"], status="confirmed")
    for j in jobs:
        j["created_at"] = str(j.get("created_at"))
        j["price"] = float(j.get("price") or 0)
    return jsonify({"jobs": jobs,
                    "printer_config": _printer_config(vendor)})


@app.route("/worker/api/jobs/<int:job_id>/printed", methods=["POST"])
def worker_mark_printed(job_id):
    vendor = worker_vendor()
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    job = db.get_job(job_id)
    if not job or job["vendor_id"] != vendor["id"]:
        return jsonify({"error": "not found"}), 404
    db.update_job(job_id, {"status": "printed"})
    if job.get("storage_key"):
        db.storage_remove(job["storage_key"])
    db.log_activity("worker", "job_printed",
                    f"job {job_id}: {job.get('original_filename')}",
                    vendor_id=vendor["id"])
    return jsonify({"status": "ok"})


@app.route("/worker/api/printer-config", methods=["POST"])
def worker_printer_config():
    """The worker syncs the vendor's printer setup (spec §7): single/multi
    mode (max 2 printers), B/W + colour assignment, preferred trays."""
    vendor = worker_vendor()
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    mode = data.get("printer_mode", "single")
    if mode not in ("single", "multi"):
        return jsonify({"error": "printer_mode must be single|multi"}), 400
    fields = {"printer_mode": mode}
    for k in ("printer_single", "tray_single", "printer_bw", "tray_bw",
              "printer_colour", "tray_colour"):
        if k in data:
            fields[k] = (data.get(k) or "")[:250] or None
    db.update_vendor(vendor["id"], fields)
    db.log_activity("worker", "printer_config_changed", json.dumps(fields),
                    vendor_id=vendor["id"])
    return jsonify({"status": "ok"})


# ===========================================================================
# Super Admin panel (spec §8)
# ===========================================================================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (request.form.get("username") == config.ADMIN_USER
                and request.form.get("password") == config.ADMIN_PASSWORD):
            session["is_admin"] = True
            db.log_activity("admin", "login", "super admin panel")
            return redirect(url_for("admin_panel"))
        flash("Invalid admin credentials.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_panel():
    vendors = [billing.refresh_status(v) for v in db.list_vendors()]
    payments = db.list_payments(limit=100)
    activity = db.list_activity(limit=150)
    creds = session.pop("new_credentials", None)  # shown exactly once
    return render_template("admin.html", vendors=vendors, payments=payments,
                           activity=activity, plans=config.PLANS,
                           installation_fee=config.INSTALLATION_FEE,
                           new_credentials=creds)


@app.route("/admin/vendors", methods=["POST"])
@admin_required
def admin_create_vendor():
    """Register a vendor (pending payment). Credentials are generated only
    after the first payment is recorded (spec §4, §6.4)."""
    name = request.form.get("name", "").strip()
    shop_name = request.form.get("shop_name", "").strip()
    plan = request.form.get("plan", "monthly")
    if not name or not shop_name or plan not in config.PLANS:
        flash("Vendor name, shop name and a valid plan are required.")
        return redirect(url_for("admin_panel"))
    shop_code = (request.form.get("shop_code", "").strip().lower()
                 or uuid.uuid4().hex[:8])
    if db.get_vendor_by_code(shop_code):
        flash(f"Shop code '{shop_code}' is already taken.")
        return redirect(url_for("admin_panel"))
    vendor_id = db.insert_vendor({
        "name": name, "shop_name": shop_name,
        "phone": request.form.get("phone", "").strip(),
        "email": request.form.get("email", "").strip(),
        "shop_code": shop_code, "plan": plan,
        "status": "pending_payment",
    })
    amounts = billing.first_payment(plan)
    db.log_activity("admin", "vendor_registered",
                    f"{shop_name} ({plan}); first payment due ₹{amounts['total']}",
                    vendor_id=vendor_id)
    flash(f"Vendor registered. First payment due: ₹{amounts['total']} "
          f"(₹{amounts['subscription_fee']} {plan} + "
          f"₹{amounts['installation_fee']} installation).")
    return redirect(url_for("admin_panel"))


@app.route("/admin/vendors/<int:vendor_id>/first-payment", methods=["POST"])
@admin_required
def admin_first_payment(vendor_id):
    vendor = db.get_vendor(vendor_id)
    if not vendor or vendor["status"] != "pending_payment":
        flash("Vendor not found or already activated.")
        return redirect(url_for("admin_panel"))
    creds = billing.record_first_payment(vendor, method="manual")
    # Stash for one-time display on the next page load (spec: delivered once).
    session["new_credentials"] = {"shop_name": vendor["shop_name"], **creds}
    return redirect(url_for("admin_panel"))


@app.route("/admin/vendors/<int:vendor_id>/renewal-payment", methods=["POST"])
@admin_required
def admin_renewal_payment(vendor_id):
    vendor = db.get_vendor(vendor_id)
    if not vendor or not vendor.get("login_id"):
        flash("Vendor not found or not yet activated.")
        return redirect(url_for("admin_panel"))
    amount = billing.record_renewal_payment(vendor, method="manual")
    flash(f"Renewal of ₹{amount} recorded for {vendor['shop_name']}; "
          f"subscription extended.")
    return redirect(url_for("admin_panel"))


@app.route("/admin/vendors/<int:vendor_id>/decide", methods=["POST"])
@admin_required
def admin_decide(vendor_id):
    """After the grace period the admin manually approves or rejects the
    vendor's continued subscription (spec §8.3)."""
    vendor = db.get_vendor(vendor_id)
    decision = request.form.get("decision")
    if not vendor or decision not in ("approve", "reject"):
        flash("Invalid request.")
        return redirect(url_for("admin_panel"))
    if decision == "approve":
        db.update_vendor(vendor_id, {"status": "active"})
        db.log_activity("admin", "subscription_approved",
                        "continued access approved after grace period",
                        vendor_id=vendor_id)
        flash(f"{vendor['shop_name']} approved — access restored.")
    else:
        db.update_vendor(vendor_id, {"status": "rejected"})
        db.log_activity("admin", "subscription_rejected",
                        "access revoked until payment is cleared",
                        vendor_id=vendor_id)
        flash(f"{vendor['shop_name']} rejected — access revoked until "
              f"payment is cleared.")
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
