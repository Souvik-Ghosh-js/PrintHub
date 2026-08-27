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
import re
import time
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, request, jsonify, render_template, send_file,
                   session, redirect, url_for, flash)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from PIL import Image

import billing
import cashfree
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
    """Landing: shops customers can actually order from — subscription live
    AND their own Cashfree account configured (payment is online-only)."""
    vendors = [billing.refresh_status(v) for v in db.list_vendors()]
    shops = [v for v in vendors
             if billing.has_access(v) and _vendor_gateway_ready(v)
             and float(v.get("price_bw") or 0) > 0]
    return render_template("landing.html", shops=shops)


def _vendor_gateway_ready(vendor) -> bool:
    """True if this shop's OWN Cashfree account is configured (per-site keys)."""
    return cashfree.configured(vendor.get("cashfree_app_id"),
                               vendor.get("cashfree_secret_key"))


@app.route("/shop/<code>")
def shop(code):
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return render_template("landing.html", shops=[],
                               error="This shop is not available right now."), 404
    # Payment is online-only, so a shop is only orderable once it has its own
    # Cashfree account AND real prices set.
    if not _vendor_gateway_ready(vendor) or float(vendor.get("price_bw") or 0) <= 0:
        return render_template(
            "landing.html", shops=[],
            error=f"{vendor['shop_name']} is still finishing setup and can't "
                  f"take orders yet. Please ask at the counter."), 503
    return render_template("customer.html", vendor=vendor,
                           formats=config.DOC_FORMATS)


# Canonical short URL — we're the provider, each vendor gets a clean branded
# link on our domain (what the counter QR encodes):
#     https://<our-domain>/<shop_code>     e.g. mohiniprintshop.org/vendor2
# Static routes (/admin, /vendor, /pay, ...) always win over this dynamic
# rule in Flask's matcher; RESERVED_CODES additionally stops a vendor from
# registering a code that shadows one of them.
RESERVED_CODES = {"shop", "vendor", "admin", "worker", "api", "files", "qr",
                  "pay", "payment", "health", "static", "favicon.ico"}


@app.route("/<code>")
def shop_short(code):
    if code in RESERVED_CODES:
        return render_template("landing.html", shops=[],
                               error="This shop is not available right now."), 404
    return shop(code)


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


@app.route("/api/warp_page", methods=["POST"])
def warp_page():
    """Perspective correction (spec §3): the customer drags the four corner
    handles over their photo and we flatten it to a front-parallel page.
    corners = JSON [[x,y] x4] in image pixels. Optionally enhances too."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    try:
        corners = json.loads(request.form.get("corners", "[]"))
        if len(corners) != 4:
            return jsonify({"error": "need exactly 4 corners"}), 400
        bgr = docscan.decode_image(f.read())
        if bgr is None:
            return jsonify({"error": "could not decode image"}), 400
        out = docscan.warp_perspective(bgr, docscan.order_corners(corners))
        mode = request.form.get("mode", "none")
        if mode and mode != "none":
            out = docscan.enhance_image(out, mode)
        return send_file(io.BytesIO(docscan.encode_jpeg(out)),
                         mimetype="image/jpeg")
    except Exception as e:
        print(f"[warp_page] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tilt_page", methods=["POST"])
def tilt_page():
    """Phone-gallery style perspective: tilt the image on its horizontal /
    vertical axis, straighten and zoom — the same controls a phone's photo
    editor offers. All values are optional and default to no change."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400

    def num(name, default=0.0):
        try:
            return float(request.form.get(name, default))
        except (TypeError, ValueError):
            return default

    try:
        bgr = docscan.decode_image(f.read())
        if bgr is None:
            return jsonify({"error": "could not decode image"}), 400
        out = docscan.tilt_image(bgr,
                                 horizontal=num("horizontal"),
                                 vertical=num("vertical"),
                                 rotate=num("rotate"),
                                 zoom=num("zoom", 1.0))
        mode = request.form.get("mode", "none")
        if mode and mode != "none":
            out = docscan.enhance_image(out, mode)
        return send_file(io.BytesIO(docscan.encode_jpeg(out)),
                         mimetype="image/jpeg")
    except Exception as e:
        print(f"[tilt_page] error: {e}")
        return jsonify({"error": str(e)}), 500


# --- ID-card compose (ported from the prototype, extended for single page) ---
# An ID-1 card (Aadhaar/PAN/Voter plastic card) is 85.6 x 54 mm. Anything
# whose aspect ratio is close to that is printed at true card size; anything
# else (the long-format Aadhaar letter, a photocopy, an A4 page) keeps its own
# proportions and is printed as large as the sheet allows. Forcing everything
# to card size is what made a full letter print out PAN-card sized.
CARD_ASPECT = 85.6 / 54.0          # 1.585
# Tight tolerance on purpose: A4 is 1.414 and a photocopy of a page must NOT
# be mistaken for a card and shrunk to 86x54 mm. 8% keeps real ID cards
# (1.55-1.62 once cropped) in while leaving A4 and letters out.
CARD_ASPECT_TOL = 0.08


def _looks_like_card(img_w, img_h):
    ar = max(img_w, img_h) / max(1.0, min(img_w, img_h))
    return abs(ar - CARD_ASPECT) / CARD_ASPECT <= CARD_ASPECT_TOL


def compose_id_pdf(sides, layout, enhance_mode):
    """Compose 1 or 2 document sides on one A4 portrait page.

    A real ID-1 card is placed at its true physical size (85.6 x 54 mm) so it
    prints life-size. A document with any other shape — the long-format
    Aadhaar letter, a photocopy, a page — keeps its own aspect ratio and is
    scaled to fill the sheet instead of being squashed into a card.

    sides: list of raw image bytes (already cropped client-side), 1 or 2 items.
    layout: 'side_by_side' | 'stacked'. A single side is centred (spec §2).
    Returns PDF bytes.
    """
    DPI = 300
    A4_W, A4_H = int(8.27 * DPI), int(11.69 * DPI)
    GAP = int(0.3 * DPI)
    MARGIN = int(0.35 * DPI)
    MM = DPI / 25.4
    CARD_W, CARD_H = int(85.6 * MM), int(54.0 * MM)

    raw = []
    for data in sides:
        bgr = docscan.decode_image(data)
        if bgr is None:
            raise ValueError("could not decode a page image")
        bgr = docscan.enhance_image(bgr, enhance_mode)
        raw.append(Image.fromarray(bgr[:, :, ::-1]))     # BGR -> RGB

    n = len(raw)
    all_cards = all(_looks_like_card(im.width, im.height) for im in raw)

    if all_cards:
        # life-size ID cards, in the requested layout
        cards = [im.resize((CARD_W, CARD_H), Image.LANCZOS) for im in raw]
    else:
        # keep each image's own proportions; fit the available area
        if n == 1:
            avail_w, avail_h = A4_W - 2 * MARGIN, A4_H - 2 * MARGIN
        elif layout == "side_by_side":
            avail_w = (A4_W - 2 * MARGIN - GAP) // 2
            avail_h = A4_H - 2 * MARGIN
        else:
            avail_w = A4_W - 2 * MARGIN
            avail_h = (A4_H - 2 * MARGIN - GAP) // 2
        cards = []
        for im in raw:
            # A wide document on a portrait sheet prints larger rotated 90.
            if n == 1 and im.width > im.height and avail_h > avail_w:
                im = im.rotate(90, expand=True)
            k = min(avail_w / im.width, avail_h / im.height)
            cards.append(im.resize((max(1, int(im.width * k)),
                                    max(1, int(im.height * k))), Image.LANCZOS))

    canvas = Image.new("RGB", (A4_W, A4_H), "white")
    if len(cards) == 1:
        c = cards[0]
        canvas.paste(c, ((A4_W - c.width) // 2, (A4_H - c.height) // 2))
    elif layout == "side_by_side":
        total_w = cards[0].width + GAP + cards[1].width
        x = (A4_W - total_w) // 2
        for c in cards:
            canvas.paste(c, (x, (A4_H - c.height) // 2))
            x += c.width + GAP
    else:  # stacked
        total_h = cards[0].height + GAP + cards[1].height
        y = (A4_H - total_h) // 2
        for c in cards:
            canvas.paste(c, ((A4_W - c.width) // 2, y))
            y += c.height + GAP

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


MAX_IMAGE_PAGES = 10          # customer may attach up to 10 photos
MAX_PDF_PAGES = 100           # sanity cap per uploaded PDF
MAX_PDFS = 5                  # how many PDFs one order may contain
MAX_TOTAL_PAGES = 200         # total printed pages in a single order


def _pdf_page_count(data: bytes) -> int:
    """Page count of an uploaded PDF (raises ValueError if unreadable)."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ValueError("this PDF is password-protected")
            return doc.page_count
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"could not read the PDF ({e})")


def parse_page_range(spec: str, total: int):
    """Turn a human page range into a sorted list of 1-based page numbers.

    Accepts "all"/"" (everything), "5", "3-5", "1,4,7-9". Raises ValueError
    with a readable message for anything out of range or malformed, so the
    customer is told exactly what is wrong before they pay.
    """
    spec = (spec or "").strip().lower()
    if spec in ("", "all"):
        return list(range(1, total + 1))

    pages = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            bits = part.split("-")
            if len(bits) != 2 or not all(b.isdigit() for b in bits):
                raise ValueError(f"'{part}' is not a valid page range")
            a, b = int(bits[0]), int(bits[1])
            if a < 1 or b < 1:
                raise ValueError("page numbers start at 1")
            if a > b:
                raise ValueError(f"'{part}': the first page must not be "
                                 f"after the last")
            if b > total:
                raise ValueError(f"'{part}': this PDF only has {total} pages")
            pages.extend(range(a, b + 1))
        else:
            if not part.isdigit():
                raise ValueError(f"'{part}' is not a page number")
            n = int(part)
            if n < 1:
                raise ValueError("page numbers start at 1")
            if n > total:
                raise ValueError(f"page {n}: this PDF only has {total} pages")
            pages.append(n)

    pages = sorted(set(pages))
    if not pages:
        raise ValueError("no pages selected")
    return pages


def extract_pdf_pages(data: bytes, pages):
    """Return a new PDF containing only `pages` (1-based), in order."""
    import fitz
    src = fitz.open(stream=data, filetype="pdf")
    try:
        out = fitz.open()
        try:
            for n in pages:
                out.insert_pdf(src, from_page=n - 1, to_page=n - 1)
            return out.tobytes()
        finally:
            out.close()
    finally:
        src.close()


def merge_pdfs(parts):
    """Concatenate several PDFs (bytes) into one, in the given order."""
    if len(parts) == 1:
        return parts[0]
    import fitz
    out = fitz.open()
    try:
        for data in parts:
            src = fitz.open(stream=data, filetype="pdf")
            try:
                out.insert_pdf(src)
            finally:
                src.close()
        return out.tobytes()
    finally:
        out.close()


def _collect_pdfs():
    """Read the uploaded PDFs and their per-file page ranges.

    Fields: pdf (repeated) and range_0, range_1, ... matching by index.
    Returns (merged_pdf_bytes, total_pages).
    """
    files = request.files.getlist("pdf")
    if not files:
        return None, 0
    if len(files) > MAX_PDFS:
        raise ValueError(f"you can attach at most {MAX_PDFS} PDFs")

    parts, total = [], 0
    for i, f in enumerate(files):
        data = f.read()
        if not data[:5].startswith(b"%PDF"):
            raise ValueError(f"'{f.filename}' is not a valid PDF")
        count = _pdf_page_count(data)
        if count < 1:
            raise ValueError(f"'{f.filename}' has no pages")
        if count > MAX_PDF_PAGES:
            raise ValueError(f"'{f.filename}' has {count} pages; the limit "
                             f"is {MAX_PDF_PAGES}")
        spec = request.form.get(f"range_{i}", "all")
        try:
            wanted = parse_page_range(spec, count)
        except ValueError as e:
            raise ValueError(f"'{f.filename}': {e}")
        parts.append(extract_pdf_pages(data, wanted)
                     if len(wanted) != count else data)
        total += len(wanted)

    if total > MAX_TOTAL_PAGES:
        raise ValueError(f"that is {total} pages; a single order is limited "
                         f"to {MAX_TOTAL_PAGES}")
    return merge_pdfs(parts), total


def _order_pdf_from_request(doc_format):
    """Build the order PDF + page count from the uploaded form files.

    Three shapes are supported:
      * an ID format (aadhaar/pan/voter) -> 1 or 2 cropped sides on one sheet
      * an uploaded PDF                  -> printed as-is, all pages
      * up to MAX_IMAGE_PAGES photos     -> one A4 page each
    """
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

    # PDFs the customer uploaded: printed as supplied, honouring the page
    # range chosen for each one, merged into a single print job.
    if request.files.getlist("pdf"):
        merged, pages = _collect_pdfs()
        if merged:
            return merged, pages

    # Photos -> one A4 page each.
    files = request.files.getlist("pages")
    if not files:
        raise ValueError("no pages uploaded")
    if len(files) > MAX_IMAGE_PAGES:
        raise ValueError(f"you can attach at most {MAX_IMAGE_PAGES} images")
    return build_document_pdf(files, enhance_mode), len(files)


@app.route("/shop/<code>/pdf-info", methods=["POST"])
def shop_pdf_info(code):
    """Page count for an uploaded PDF so the customer sees the real price
    before paying. The server is the authority here — the price charged is
    computed from this same count."""
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return jsonify({"error": "shop unavailable"}), 404
    f = request.files.get("pdf")
    if not f:
        return jsonify({"error": "no pdf uploaded"}), 400
    data = f.read()
    if not data[:5].startswith(b"%PDF"):
        return jsonify({"error": "that file is not a valid PDF"}), 400
    try:
        pages = _pdf_page_count(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if pages > MAX_PDF_PAGES:
        return jsonify({"error": f"PDFs are limited to {MAX_PDF_PAGES} pages"}), 400
    # Validate a page range in the same request when one is supplied, so the
    # customer sees the selected count (and any mistake) immediately.
    spec = request.form.get("range", "all")
    try:
        selected = len(parse_page_range(spec, pages))
    except ValueError as e:
        return jsonify({"pages": pages, "error": str(e)}), 400
    return jsonify({"pages": pages, "selected": selected})


PREVIEW_MAX_IMAGE_PAGES = 12       # rasterising more than this is wasteful
PREVIEW_DPI = 96                   # enough to read on a phone, small to send


def _pdf_to_png_pages(pdf_bytes, max_pages=PREVIEW_MAX_IMAGE_PAGES,
                      dpi=PREVIEW_DPI):
    """Rasterise a PDF to a list of PNG bytes, one per page."""
    import fitz
    pages = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pages.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    return pages


@app.route("/shop/<code>/preview", methods=["POST"])
def shop_preview(code):
    """Composed document for the confirm-before-paying step.

    Returns the PDF by default. With ?format=images it returns the pages
    rendered as PNGs instead: Android Chrome has no built-in PDF viewer, so
    an embedded PDF shows a grey "Open" stub and the customer cannot see
    what they are about to pay for.
    """
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return jsonify({"error": "shop unavailable"}), 404
    try:
        pdf_bytes, total_pages = _order_pdf_from_request(
            request.form.get("doc_format", "document"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if request.args.get("format") == "images":
        try:
            import base64
            pages = _pdf_to_png_pages(pdf_bytes)
            return jsonify({
                "total_pages": total_pages,
                "shown": len(pages),
                "pages": ["data:image/png;base64," +
                          base64.b64encode(p).decode() for p in pages],
            })
        except Exception as e:
            print(f"[preview] raster failed: {e}")
            # fall through and return the PDF rather than failing outright

    resp = send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    resp.headers["X-Total-Pages"] = str(total_pages)
    return resp


@app.route("/shop/<code>/order", methods=["POST"])
def shop_order(code):
    """Create a print job at this vendor and start its ONLINE payment.

    Payment is online-only: a Cashfree order is created on THIS VENDOR'S OWN
    Cashfree account (every site has a different account), and the job only
    reaches the shop's printer after the payment actually succeeds. There is
    no counter/manual path — nothing prints unpaid.
    """
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return jsonify({"error": "shop unavailable"}), 404
    if not _vendor_gateway_ready(vendor):
        return jsonify({"error": "This shop has not finished setting up online "
                                 "payments yet. Please ask at the counter."}), 503

    doc_format = request.form.get("doc_format", "document")
    color_mode = request.form.get("color_mode", "bw")
    copies = max(1, int(request.form.get("copies", 1)))
    customer_name = request.form.get("customer_name", "")[:250]
    customer_phone = re.sub(r"\D", "", request.form.get("customer_phone", ""))[:15]

    per_page = float(vendor["price_colour"] if color_mode == "color"
                     else vendor["price_bw"])
    if per_page <= 0:
        return jsonify({"error": "This shop has not set its prices yet. "
                                 "Please ask at the counter."}), 503

    try:
        pdf_bytes, total_pages = _order_pdf_from_request(doc_format)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    price = round(per_page * total_pages * copies, 2)
    if price <= 0:
        return jsonify({"error": "Could not price this order."}), 400

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
        "status": "awaiting_payment",   # printed only after payment succeeds
        "total_pages": total_pages,
        "color_mode": color_mode,
        "copies": copies,
        "price": price,
        "payment_status": "pending",
    })
    db.log_activity("customer", "job_created",
                    f"job {job_id}: {label}, {color_mode}, x{copies}, ₹{price}",
                    vendor_id=vendor["id"])

    order_id = f"PJOB_{job_id}_{int(time.time())}"
    result = cashfree.create_order(
        app_id=vendor["cashfree_app_id"],
        secret_key=vendor["cashfree_secret_key"],
        env=vendor.get("cashfree_env") or "production",
        order_id=order_id, amount=price,
        customer_id=f"cust_{job_id}",
        customer_email="customer@example.com",
        customer_phone=customer_phone or "9999999999",
        return_url=f"{request.url_root}{code}",
        note=f"Print job #{job_id} at {vendor['shop_name']}",
    )
    if not result["success"]:
        # No payment session -> no job. Never leave an unpayable job queued.
        db.update_job(job_id, {"status": "cancelled",
                               "payment_status": "failed"})
        db.storage_remove(storage_key)
        db.log_activity("system", "order_failed",
                        f"job {job_id}: gateway error: {result['error']}",
                        vendor_id=vendor["id"])
        return jsonify({"error": "Could not start the payment. Please try "
                                 "again in a moment.",
                        "detail": result["error"]}), 502

    db.update_job(job_id, {"order_id": order_id})
    db.insert_gateway_order({"order_id": order_id, "vendor_id": vendor["id"],
                             "purpose": "print_job", "amount": price})
    return jsonify({"ok": True, "job_id": job_id, "price": price,
                    "total_pages": total_pages,
                    "order_id": order_id,
                    "payment_session_id": result["payment_session_id"],
                    "cf_env": vendor.get("cashfree_env") or "production"})


def _finalize_print_order(order, success, transaction_id=None):
    """Idempotently apply a print-job payment result (webhook or poll).

    Unpaid orders are cancelled and their stored PDF removed — nothing that
    was never paid for is left queued or on disk.
    """
    if order["status"] == "paid":
        return "paid"
    if not success:
        db.update_gateway_order(order["order_id"], {"status": "failed"})
        for job in db.get_jobs_by_order(order["order_id"]):
            db.update_job(job["id"], {"status": "cancelled",
                                      "payment_status": "failed"})
            if job.get("storage_key"):
                db.storage_remove(job["storage_key"])
        return "failed"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.update_gateway_order(order["order_id"],
                            {"status": "paid", "transaction_id": transaction_id})
    db.update_jobs_by_order(order["order_id"], {
        "status": "confirmed", "payment_status": "paid",
        "transaction_id": transaction_id, "paid_at": now})
    db.log_activity("system", "print_payment_received",
                    f"order {order['order_id']} paid ₹{order['amount']}",
                    vendor_id=order.get("vendor_id"))
    return "paid"


@app.route("/shop/<code>/payment-status/<order_id>")
def shop_payment_status(code, order_id):
    """Poll endpoint for the customer page. Also asks Cashfree directly
    (vendor's own keys) so payments confirm even when the public webhook
    can't reach this server (e.g. local testing)."""
    vendor = db.get_vendor_by_code(code)
    order = db.get_gateway_order(order_id)
    if not vendor or not order or order.get("vendor_id") != vendor["id"]:
        return jsonify({"error": "not found"}), 404
    if order["status"] == "pending":
        cf_status = cashfree.order_status(
            vendor["cashfree_app_id"], vendor["cashfree_secret_key"],
            vendor.get("cashfree_env") or "production", order_id)
        if cf_status == "PAID":
            _finalize_print_order(order, True)
            order = db.get_gateway_order(order_id)
        elif cf_status in ("EXPIRED", "TERMINATED"):
            _finalize_print_order(order, False)
            order = db.get_gateway_order(order_id)
    return jsonify({"order_id": order_id, "status": order["status"]})


# How long a pending print order stays reconcilable. Cashfree sessions expire
# well inside this, so anything older is genuinely abandoned.
# 24h: a shop can close for the night with a paid job unconfirmed, and the
# customer will come back for their print the next morning. Cashfree keeps
# order records far longer, so asking about a day-old order is still valid.
RECONCILE_MAX_AGE_MIN = 24 * 60
RECONCILE_MIN_AGE_SEC = 45      # give the live poll a chance first


def reconcile_pending_orders(vendor_id=None, limit=25):
    """Ask Cashfree about print orders we still think are pending.

    The browser poll only runs while the customer keeps the page open, and a
    vendor may not have configured their webhook, so a real payment can go
    unrecorded — the customer is charged and nothing prints. This closes that
    gap by asking the gateway itself. Safe to call often; it only touches
    orders that are still pending.
    """
    sql = ("SELECT * FROM gateway_orders WHERE purpose = 'print_job' "
           "AND status = 'pending'")
    params = []
    if vendor_id:
        sql += " AND vendor_id = %s"
        params.append(vendor_id)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))

    changed = 0
    for order in db.query(sql, tuple(params)):
        created = order.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                created = None
        if created:
            age = (datetime.now() - created).total_seconds()
            if age < RECONCILE_MIN_AGE_SEC or age > RECONCILE_MAX_AGE_MIN * 60:
                continue
        vendor = db.get_vendor(order["vendor_id"])
        if not vendor or not _vendor_gateway_ready(vendor):
            continue
        status = cashfree.order_status(
            vendor["cashfree_app_id"], vendor["cashfree_secret_key"],
            vendor.get("cashfree_env") or "production", order["order_id"])
        if status == "PAID":
            _finalize_print_order(order, True)
            db.log_activity("system", "payment_reconciled",
                            f"order {order['order_id']} was paid at Cashfree "
                            f"but never confirmed here; job released to print",
                            vendor_id=vendor["id"])
            changed += 1
        elif status in ("EXPIRED", "TERMINATED"):
            _finalize_print_order(order, False)
            changed += 1
    return changed


@app.route("/payment/webhook/vendor", methods=["POST"])
def vendor_payment_webhook():
    """Cashfree webhook for PRINT-JOB payments. Every vendor points their
    OWN Cashfree account's webhook at this one URL; we resolve the vendor
    from the order and verify against THAT vendor's webhook secret."""
    data = request.get_json(silent=True) or {}
    order_id = data.get("data", {}).get("order", {}).get("order_id")
    payment = data.get("data", {}).get("payment", {})
    if not order_id:
        return jsonify({"error": "missing order_id"}), 400
    order = db.get_gateway_order(order_id)
    if not order or order["purpose"] != "print_job":
        return jsonify({"error": "unknown order"}), 404
    vendor = db.get_vendor(order["vendor_id"])
    if not vendor:
        return jsonify({"error": "unknown vendor"}), 404
    if not cashfree.verify_webhook(vendor.get("cashfree_webhook_secret"),
                                   request.headers,
                                   request.get_data(as_text=True)):
        print(f"[webhook/vendor] REJECTED: bad signature for {order_id}")
        return jsonify({"error": "invalid signature"}), 401
    status = payment.get("payment_status", "").upper()
    _finalize_print_order(order, status == "SUCCESS",
                          payment.get("cf_payment_id"))
    return jsonify({"status": "ok"})


# ===========================================================================
# Shop QR code — customers don't log in: they scan the QR at the counter and
# land on /shop/<code> to upload & pay. The admin hands each vendor their QR
# (poster) at onboarding; vendors can reprint it from their dashboard.
# ===========================================================================
def _shop_url(vendor) -> str:
    """Public customer URL encoded in the QR — the vendor's short branded
    link on OUR domain (we are the provider):  <PRINTHUB_BASE_URL>/<code>,
    e.g. mohiniprintshop.org/vendor2. Set PRINTHUB_BASE_URL in production."""
    base = db.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/{vendor['shop_code']}"


def _qr_png(data: str) -> bytes:
    import qrcode
    img = qrcode.make(data, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _qr_vendor_or_none(vendor_id):
    """The admin may fetch any vendor's QR; a logged-in vendor only their own."""
    if session.get("is_admin"):
        return db.get_vendor(vendor_id)
    if session.get("vendor_id") == vendor_id:
        return db.get_vendor(vendor_id)
    return None


@app.route("/qr/<int:vendor_id>.png")
def shop_qr_png(vendor_id):
    vendor = _qr_vendor_or_none(vendor_id)
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    resp = send_file(io.BytesIO(_qr_png(_shop_url(vendor))), mimetype="image/png")
    # Nice filename when the admin/vendor right-clicks "save image as".
    resp.headers["Content-Disposition"] = \
        f'inline; filename="printhub-qr-{vendor["shop_code"]}.png"'
    return resp


@app.route("/qr/<int:vendor_id>/poster")
def shop_qr_poster(vendor_id):
    """Print-ready poster: shop name + big QR + scan instructions."""
    vendor = _qr_vendor_or_none(vendor_id)
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    return render_template("qr_poster.html", vendor=vendor,
                           shop_url=_shop_url(vendor))


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
    has_gateway = _vendor_gateway_ready(vendor)
    has_prices = float(vendor.get("price_bw") or 0) > 0
    stats = {
        "printed": sum(1 for j in jobs if j["status"] == "printed"),
        "queued": sum(1 for j in jobs
                      if j["status"] in ("confirmed", "printing")),
        "attention": sum(1 for j in jobs if j["status"] == "needs_attention"),
        "revenue": sum(float(j["price"] or 0) for j in jobs
                       if j.get("payment_status") == "paid"),
    }
    base = db.PUBLIC_BASE_URL.rstrip("/")
    return render_template(
        "vendor_dashboard.html", vendor=vendor, jobs=jobs, payments=payments,
        plans=config.PLANS, worker_exe=worker_exe_available(),
        has_gateway=has_gateway, has_prices=has_prices, stats=stats,
        live=billing.has_access(vendor) and has_gateway and has_prices,
        shop_url=f"{base}/{vendor['shop_code']}",
        webhook_url=f"{base}/payment/webhook/vendor")


@app.route("/vendor/recheck-payments", methods=["POST"])
@vendor_required
def vendor_recheck_payments(vendor):
    """Manually re-check pending payments with Cashfree (for a customer who
    says they paid but whose job is still awaiting payment)."""
    try:
        n = reconcile_pending_orders(vendor_id=vendor["id"], limit=50)
    except Exception as e:
        flash(f"Could not reach Cashfree: {e}")
        return redirect(url_for("vendor_dashboard"))
    flash(f"Checked pending payments with Cashfree — {n} updated."
          if n else "Checked pending payments — nothing new was paid.")
    return redirect(url_for("vendor_dashboard"))


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


@app.route("/vendor/cashfree", methods=["POST"])
@vendor_required
def vendor_cashfree(vendor):
    """Save this shop's OWN Cashfree account keys (every site has a
    different account). Blank secret fields keep the stored value."""
    fields = {
        "cashfree_app_id": request.form.get("cashfree_app_id", "").strip(),
        "cashfree_secret_key": request.form.get("cashfree_secret_key", "").strip(),
        "cashfree_webhook_secret": request.form.get("cashfree_webhook_secret", "").strip(),
    }
    fields = {k: v for k, v in fields.items() if v}   # keep saved secrets
    fields["cashfree_env"] = ("sandbox" if request.form.get("cashfree_env") == "sandbox"
                              else "production")
    db.update_vendor(vendor["id"], fields)
    db.log_activity("vendor", "cashfree_updated",
                    "shop Cashfree account keys changed", vendor_id=vendor["id"])
    flash("Cashfree account saved. Customers can now pay online at your shop page.")
    return redirect(url_for("vendor_dashboard"))


# ===========================================================================
# Vendor subscription payments — PLATFORM Cashfree account (spec §5, §6)
# ===========================================================================
def _start_subscription_checkout(vendor, purpose):
    """Create a platform-account Cashfree order for a vendor's first payment
    (plan fee + installation) or renewal (plan fee only). Returns a rendered
    checkout page or an error string."""
    if purpose == "first_payment":
        amounts = billing.first_payment(vendor["plan"])
        amount = amounts["total"]
        plan_fee = amounts["subscription_fee"]
        install_fee = amounts["installation_fee"]
        prefix = "SUB"
        note = (f"PrintHub {vendor['plan']} subscription "
                f"+ ₹{install_fee} installation")
    else:
        amount = billing.renewal_amount(vendor["plan"])
        plan_fee, install_fee = amount, 0
        prefix = "REN"
        note = f"PrintHub {vendor['plan']} renewal"

    order_id = f"{prefix}_{vendor['id']}_{int(time.time())}"
    result = cashfree.create_order(
        app_id=config.PLATFORM_CASHFREE_APP_ID,
        secret_key=config.PLATFORM_CASHFREE_SECRET_KEY,
        env=config.PLATFORM_CASHFREE_ENV,
        order_id=order_id, amount=amount,
        customer_id=f"vendor_{vendor['id']}",
        customer_email=vendor.get("email"),
        customer_phone=vendor.get("phone"),
        return_url=f"{request.url_root}pay/return?order_id={order_id}",
        note=note,
    )
    if not result["success"]:
        return None, result["error"]
    db.insert_gateway_order({"order_id": order_id, "vendor_id": vendor["id"],
                             "purpose": purpose, "amount": amount})
    return render_template("pay.html", shop_name=vendor["shop_name"],
                           plan=vendor["plan"], amount=amount,
                           plan_fee=plan_fee, install_fee=install_fee,
                           order_id=order_id, purpose=purpose,
                           payment_session_id=result["payment_session_id"],
                           cf_env=config.PLATFORM_CASHFREE_ENV), None


@app.route("/pay/onboard/<code>")
def pay_onboard(code):
    """Public onboarding payment link (sent to a freshly registered vendor).
    On success the auto-generated credentials are shown ONCE (spec §6.4)."""
    vendor = db.get_vendor_by_code(code)
    if not vendor:
        return render_template("pay_result.html", ok=False,
                               message="Unknown shop code."), 404
    if vendor["status"] != "pending_payment":
        return render_template("pay_result.html", ok=False,
                               message="This vendor is already activated — "
                                       "log in instead.")
    page, err = _start_subscription_checkout(vendor, "first_payment")
    if err:
        return render_template("pay_result.html", ok=False,
                               message=f"Payment setup failed: {err}")
    return page


@app.route("/vendor/renew-online", methods=["POST"])
@vendor_required
def vendor_renew_online(vendor):
    """Renewal payment — plan fee only, no installation fee repeated (§6.2)."""
    if vendor["plan"] == "lifetime":
        flash("Lifetime plan has no renewals.")
        return redirect(url_for("vendor_dashboard"))
    page, err = _start_subscription_checkout(vendor, "renewal")
    if err:
        flash(f"Payment setup failed: {err}")
        return redirect(url_for("vendor_dashboard"))
    return page


def _finalize_subscription_order(order, success, transaction_id=None,
                                 consume_creds=False):
    """Idempotently apply a subscription payment result.

    On the paid transition of a first payment the vendor's credentials are
    generated (spec §6.4) and parked one-time in the order row, so they
    survive a webhook-first ordering. Only a caller that will DISPLAY them
    (the vendor's return page) passes consume_creds=True, which pops them.
    """
    if order["status"] == "paid":
        meta = order.get("meta")
        if meta and consume_creds:  # pop the one-time credentials
            db.update_gateway_order(order["order_id"], {"meta": None})
            return "paid", json.loads(meta)
        return "paid", None
    if not success:
        db.update_gateway_order(order["order_id"], {"status": "failed"})
        return "failed", None

    vendor = db.get_vendor(order["vendor_id"])
    creds = None
    if order["purpose"] == "first_payment" and vendor["status"] == "pending_payment":
        creds = billing.record_first_payment(vendor, method="cashfree",
                                             reference=order["order_id"])
    elif order["purpose"] == "renewal":
        billing.record_renewal_payment(vendor, method="cashfree",
                                       reference=order["order_id"])
    db.update_gateway_order(order["order_id"], {
        "status": "paid", "transaction_id": transaction_id,
        "meta": None if (creds and consume_creds) else
                (json.dumps(creds) if creds else None)})
    return "paid", (creds if consume_creds else None)


@app.route("/pay/return")
def pay_return():
    """Vendor lands here after the platform-account checkout. Confirms the
    order with Cashfree directly (never trusts the redirect)."""
    order_id = request.args.get("order_id", "")
    order = db.get_gateway_order(order_id)
    if not order or order["purpose"] not in ("first_payment", "renewal"):
        return render_template("pay_result.html", ok=False,
                               message="Unknown payment order."), 404
    creds = None
    if order["status"] == "pending":
        cf_status = cashfree.order_status(
            config.PLATFORM_CASHFREE_APP_ID, config.PLATFORM_CASHFREE_SECRET_KEY,
            config.PLATFORM_CASHFREE_ENV, order_id)
        status, creds = _finalize_subscription_order(
            order, cf_status == "PAID", consume_creds=True)
    else:
        status, creds = _finalize_subscription_order(
            order, order["status"] == "paid", consume_creds=True)
    if status != "paid":
        return render_template(
            "pay_result.html", ok=False,
            message="Payment not confirmed yet. If you were charged, refresh "
                    "this page in a minute — the confirmation may still be "
                    "on its way.")
    vendor = db.get_vendor(order["vendor_id"])
    if order["purpose"] == "first_payment":
        message = (f"Subscription for {vendor['shop_name']} is active. "
                   f"Amount paid: ₹{order['amount']}.")
    else:
        message = (f"Renewal received — subscription extended for "
                   f"{vendor['shop_name']}. Amount paid: ₹{order['amount']}.")
    return render_template("pay_result.html", ok=True, message=message,
                           creds=creds, vendor=vendor)


@app.route("/payment/webhook/platform", methods=["POST"])
def platform_payment_webhook():
    """Cashfree webhook for the PLATFORM account (subscriptions/renewals)."""
    if not cashfree.verify_webhook(config.PLATFORM_CASHFREE_WEBHOOK_SECRET,
                                   request.headers,
                                   request.get_data(as_text=True)):
        return jsonify({"error": "invalid signature"}), 401
    data = request.get_json(silent=True) or {}
    order_id = data.get("data", {}).get("order", {}).get("order_id")
    payment = data.get("data", {}).get("payment", {})
    if not order_id:
        return jsonify({"error": "missing order_id"}), 400
    order = db.get_gateway_order(order_id)
    if not order or order["purpose"] not in ("first_payment", "renewal"):
        return jsonify({"error": "unknown order"}), 404
    status = payment.get("payment_status", "").upper()
    _finalize_subscription_order(order, status == "SUCCESS",
                                 payment.get("cf_payment_id"))
    return jsonify({"status": "ok"})


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
    # Catch payments the browser never confirmed (closed tab, no webhook):
    # ask Cashfree directly so a paid job still reaches the printer.
    try:
        reconcile_pending_orders(vendor_id=vendor["id"])
    except Exception as e:
        print(f"[reconcile] {e}")

    # Give back any job whose worker vanished mid-print, then take out of
    # circulation anything that has already been tried too many times.
    db.release_stale_claims()
    db.park_exhausted_jobs()
    jobs = db.get_vendor_jobs(vendor["id"], status="confirmed")
    out = []
    for j in jobs:
        # Hand a job to the worker ONCE. It is claimed here (status ->
        # printing) so a later poll can never serve it again — the failure
        # that reprinted a customer's document forever. If the worker dies
        # mid-print, _release_stale_claims() below puts it back after a
        # timeout, and the worker's own guard stops a same-session repeat.
        if db.claim_job(j["id"]) == 0:
            continue                      # another poll already took it
        j["created_at"] = str(j.get("created_at"))
        j["price"] = float(j.get("price") or 0)
        out.append(j)
    # shop_name lets the worker resume a saved session without re-login.
    return jsonify({"jobs": out,
                    "shop_name": vendor["shop_name"],
                    "printer_config": _printer_config(vendor)})


@app.route("/worker/api/jobs/<int:job_id>/printed", methods=["POST"])
def worker_mark_printed(job_id):
    vendor = worker_vendor()
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    job = db.get_job(job_id)
    if not job or job["vendor_id"] != vendor["id"]:
        return jsonify({"error": "not found"}), 404
    if job["status"] == "printed":
        return jsonify({"status": "ok"})       # idempotent re-confirmation
    db.update_job(job_id, {"status": "printed"})
    if job.get("storage_key"):
        db.storage_remove(job["storage_key"])
    db.log_activity("worker", "job_printed",
                    f"job {job_id}: {job.get('original_filename')}",
                    vendor_id=vendor["id"])
    return jsonify({"status": "ok"})


@app.route("/worker/api/jobs/<int:job_id>/release", methods=["POST"])
def worker_release_job(job_id):
    """The worker could NOT print this job (printer offline, out of paper,
    submit error). Put it straight back in the queue so it is retried — a
    paid job must never be silently dropped."""
    vendor = worker_vendor()
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    job = db.get_job(job_id)
    if not job or job["vendor_id"] != vendor["id"]:
        return jsonify({"error": "not found"}), 404
    if job["status"] == "printing":
        db.update_job(job_id, {"status": "confirmed", "claimed_at": None})
        reason = (request.get_json(silent=True) or {}).get("reason", "")
        db.log_activity("worker", "job_requeued",
                        f"job {job_id}: {reason or 'not printed'}",
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
# Worker app download — ONE build serves every vendor (identity comes from
# the login). Build it once on a Windows PC (worker/build_exe.bat), upload
# dist/PrintHubWorker.exe to server/downloads/, and every vendor grabs it
# from their dashboard.
# ===========================================================================
WORKER_EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "downloads", "PrintHubWorker.exe")


def worker_exe_available() -> bool:
    return os.path.exists(WORKER_EXE)


@app.route("/download/worker")
def download_worker():
    if not (session.get("vendor_id") or session.get("is_admin")):
        return redirect(url_for("vendor_login"))
    if not worker_exe_available():
        return ("The worker app has not been uploaded yet — build it with "
                "worker/build_exe.bat and place PrintHubWorker.exe in "
                "server/downloads/ on the server.", 404)
    return send_file(WORKER_EXE, as_attachment=True,
                     download_name="PrintHubWorker.exe")


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
    stats = {
        "live": sum(1 for v in vendors if billing.has_access(v)
                    and _vendor_gateway_ready(v)
                    and float(v.get("price_bw") or 0) > 0),
        "pending": sum(1 for v in vendors if v["status"] == "pending_payment"),
        "revenue": sum(float(p["amount"] or 0) for p in payments
                       if p.get("status") == "paid"),
    }
    return render_template("admin.html", vendors=vendors, payments=payments,
                           activity=activity, plans=config.PLANS,
                           installation_fee=config.INSTALLATION_FEE,
                           new_credentials=creds, stats=stats,
                           base_url=db.PUBLIC_BASE_URL.rstrip("/"))


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
    # The code becomes the vendor's short URL on our domain (/<code>), so it
    # must be a clean slug and must not shadow a platform route.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", shop_code):
        flash("Shop code must be 2-32 chars: letters, digits, hyphens.")
        return redirect(url_for("admin_panel"))
    if shop_code in RESERVED_CODES:
        flash(f"Shop code '{shop_code}' is reserved — choose another.")
        return redirect(url_for("admin_panel"))
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
    # Activation happens ONLY through a real Cashfree payment on this link.
    flash(f"Vendor registered. Send them this payment link to activate — "
          f"₹{amounts['total']} (₹{amounts['subscription_fee']} {plan} + "
          f"₹{amounts['installation_fee']} installation): "
          f"{request.url_root.rstrip('/')}/pay/onboard/{shop_code}")
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
