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
from datetime import datetime, timedelta
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
import razorpay_pg as rzp
import docscan
import convert

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


@app.context_processor
def inject_contact():
    """Contact details are needed on the public pages and in the vendor
    portal, so make them available to every template."""
    return {"contact_phones": config.CONTACT_PHONES,
            "installation_fee": config.INSTALLATION_FEE}


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
        if vendor["status"] in billing.ADMIN_DISABLED:
            session.pop("vendor_id", None)
            flash(DISABLED_MESSAGE)
            return redirect(url_for("vendor_login"))
        return f(vendor, *args, **kwargs)
    return wrapper


DISABLED_MESSAGE = ("This shop has been disabled by PrintHub. Please contact "
                    "us to restore it.")


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
    AND their own Razorpay account configured (payment is online-only)."""
    vendors = [billing.refresh_status(v) for v in db.list_vendors()]
    shops = [v for v in vendors
             if billing.has_access(v) and _vendor_gateway_ready(v)
             and float(v.get("price_bw") or 0) > 0]
    return render_template("landing.html", shops=shops,
                           stats=db.platform_stats(),
                           plans=config.PLANS,
                           install_fee=config.INSTALLATION_FEE)


def _landing_error(message, code):
    """Render the landing page carrying a message.

    landing.html always needs stats/plans/install_fee for its live-stats and
    pricing sections; rendering it without them raises UndefinedError and
    turns a friendly 503 into a 500. Route every error render through here.
    """
    return render_template("landing.html", shops=[], error=message,
                           stats=db.platform_stats(), plans=config.PLANS,
                           install_fee=config.INSTALLATION_FEE), code


def _vendor_gateway_ready(vendor) -> bool:
    """True if this shop's OWN Razorpay account is configured (per-shop keys)."""
    return rzp.configured(vendor.get("razorpay_key_id"),
                          vendor.get("razorpay_key_secret"))


@app.route("/shop/<code>")
def shop(code):
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return _landing_error("This shop is not available right now.", 404)
    # Payment is online-only, so a shop is only orderable once it has its own
    # Razorpay account AND real prices set.
    if not _vendor_gateway_ready(vendor) or float(vendor.get("price_bw") or 0) <= 0:
        return _landing_error(
            f"{vendor['shop_name']} is still finishing setup and can't "
            f"take orders yet. Please ask at the counter.", 503)
    return render_template("customer.html", vendor=vendor,
                           formats=config.DOC_FORMATS,
                           duplex=duplex_offer(vendor))


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
        return _landing_error("This shop is not available right now.", 404)
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
    """Multi-page scan -> one PDF, each page on an A4 sheet.

    A landscape photo gets a LANDSCAPE sheet rather than being letterboxed
    into a portrait one: forcing a wide image onto a tall page shrinks it to
    the narrow dimension and prints big white bands above and below, wasting
    most of the paper. Each page is sized to its own image, so the content
    fills the sheet either way.
    """
    DPI = 200
    A4_SHORT, A4_LONG = int(8.27 * DPI), int(11.69 * DPI)
    MARGIN = int(0.3 * DPI)

    pages = []
    for f in files:
        bgr = docscan.decode_image(f.read())
        if bgr is None:
            raise ValueError("could not decode a page image")
        bgr = docscan.enhance_image(bgr, enhance_mode)
        img = Image.fromarray(bgr[:, :, ::-1])

        # Match the sheet to the image's own orientation.
        if img.width > img.height:
            sheet_w, sheet_h = A4_LONG, A4_SHORT      # landscape A4
        else:
            sheet_w, sheet_h = A4_SHORT, A4_LONG      # portrait A4

        max_w, max_h = sheet_w - 2 * MARGIN, sheet_h - 2 * MARGIN
        scale = min(max_w / img.width, max_h / img.height)
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
        page = Image.new("RGB", (sheet_w, sheet_h), "white")
        page.paste(img, ((sheet_w - img.width) // 2,
                         (sheet_h - img.height) // 2))
        pages.append(page)
    if not pages:
        raise ValueError("no pages")
    out = io.BytesIO()
    pages[0].save(out, format="PDF", resolution=DPI,
                  save_all=True, append_images=pages[1:])
    return out.getvalue()


MAX_IMAGE_PAGES = 10          # customer may attach up to 10 photos
MAX_PDF_PAGES = 100           # sanity cap per uploaded PDF
MAX_FILES = 10                # how many files (PDF/Word/Excel/images/...) per order
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
                raise ValueError(f"'{part}': this file only has {total} pages")
            pages.extend(range(a, b + 1))
        else:
            if not part.isdigit():
                raise ValueError(f"'{part}' is not a page number")
            n = int(part)
            if n < 1:
                raise ValueError("page numbers start at 1")
            if n > total:
                raise ValueError(f"page {n}: this file only has {total} pages")
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


def merge_pdfs(parts, double_sided=False):
    """Concatenate several PDFs (bytes) into one, in the given order.

    double_sided: after every file with an odd number of pages (except the
    last) add a blank page, so the next file starts on a fresh sheet
    instead of on the back of the previous one. Blank pages are not
    charged — the price is counted from the customer's pages only.
    """
    if len(parts) == 1:
        return parts[0]
    import fitz
    out = fitz.open()
    try:
        for i, data in enumerate(parts):
            src = fitz.open(stream=data, filetype="pdf")
            try:
                out.insert_pdf(src)
                if (double_sided and src.page_count % 2
                        and i < len(parts) - 1):
                    last = src[-1].rect
                    out.new_page(width=last.width, height=last.height)
            finally:
                src.close()
        return out.tobytes()
    finally:
        out.close()


def _upload_to_pdf(f):
    """(pdf_bytes, filename) for one uploaded file of any supported type."""
    data = f.read()
    if not data:
        raise ValueError(f"'{f.filename}' is empty")
    return convert.to_pdf(data, f.filename), f.filename


def duplex_offer(vendor):
    """The both-sides printing this shop offers, or None.

    A shop opts in from its portal: turn it on, choose the printer that
    prints both sides, and set a both-sides B/W price (colour is optional —
    0 means both sides is B/W only). Until all of that is in place the
    option never reaches customers.
    Returns {"printer", "bw", "color"} with per-page prices.
    """
    if not vendor or not int(vendor.get("duplex_enabled") or 0):
        return None
    printer = (vendor.get("printer_duplex") or "").strip()
    bw = float(vendor.get("price_bw_duplex") or 0)
    if not printer or bw <= 0:
        return None
    return {"printer": printer, "bw": bw,
            "color": float(vendor.get("price_colour_duplex") or 0)}


def _wants_double_sided(vendor, doc_format, color_mode):
    """True if this request asks for both sides AND the shop offers it for
    this colour mode. Raises ValueError when asked for but not offered, so
    the customer is never charged a both-sides price for a one-sided print
    (or the reverse)."""
    if request.form.get("sides") != "double" or doc_format in config.DOC_FORMATS:
        return False                  # ID cards are one sheet by design
    offer = duplex_offer(vendor)
    if not offer:
        raise ValueError("This shop doesn't offer both-sides printing right "
                         "now. Please refresh the page.")
    if color_mode == "color" and offer["color"] <= 0:
        raise ValueError("This shop prints both sides in Black & White only. "
                         "Switch to B/W or turn off both sides.")
    return True


def _collect_pdfs(double_sided=False):
    """Read the uploaded files and their per-file page ranges.

    Any supported type (PDF, Word, Excel, PowerPoint, text, images) is
    converted to PDF first, so ranges and pricing work the same for all.
    Fields: pdf (repeated) and range_0, range_1, ... matching by index.
    Returns (merged_pdf_bytes, total_pages).
    """
    files = request.files.getlist("pdf")
    if not files:
        return None, 0
    if len(files) > MAX_FILES:
        raise ValueError(f"you can attach at most {MAX_FILES} files")

    parts, total = [], 0
    for i, f in enumerate(files):
        data, _ = _upload_to_pdf(f)
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
    return merge_pdfs(parts, double_sided=double_sided), total


def _order_pdf_from_request(doc_format, double_sided=False):
    """Build the order PDF + page count from the uploaded form files.

    Three shapes are supported:
      * an ID format (aadhaar/pan/voter) -> 1 or 2 cropped sides on one sheet
      * uploaded files (PDF, Word, Excel, PowerPoint, text, images)
                                         -> converted to PDF, chosen pages
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

    # Files the customer uploaded: printed as supplied (office files and
    # images converted to PDF), honouring the page range chosen for each
    # one, merged into a single print job.
    if request.files.getlist("pdf"):
        merged, pages = _collect_pdfs(double_sided)
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
    """Page count for an uploaded file (any supported type, converted to
    PDF) so the customer sees the real price before paying. The server is
    the authority here — the price charged is computed from this same
    count, on the same conversion."""
    vendor = billing.refresh_status(db.get_vendor_by_code(code))
    if not billing.has_access(vendor):
        return jsonify({"error": "shop unavailable"}), 404
    f = request.files.get("pdf")
    if not f:
        return jsonify({"error": "no file uploaded"}), 400
    try:
        data, _ = _upload_to_pdf(f)
        pages = _pdf_page_count(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if pages > MAX_PDF_PAGES:
        return jsonify({"error": f"files are limited to {MAX_PDF_PAGES} pages"}), 400
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
    doc_format = request.form.get("doc_format", "document")
    try:
        double = _wants_double_sided(vendor, doc_format,
                                     request.form.get("color_mode", "bw"))
        pdf_bytes, total_pages = _order_pdf_from_request(doc_format, double)
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

    Payment is online-only: a Razorpay order is created on THIS VENDOR'S OWN
    Razorpay account (every shop has a different account), and the job only
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

    if float(vendor["price_colour"] if color_mode == "color"
             else vendor["price_bw"]) <= 0:
        return jsonify({"error": "This shop has not set its prices yet. "
                                 "Please ask at the counter."}), 503

    try:
        double = _wants_double_sided(vendor, doc_format, color_mode)
        pdf_bytes, total_pages = _order_pdf_from_request(doc_format, double)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Both sides only means something for a document of 2+ pages; a single
    # page prints (and is charged) one-sided. A both-sides order is charged
    # the shop's own both-sides price per page.
    sides = "double" if double and total_pages > 1 else "single"
    if sides == "double":
        offer = duplex_offer(vendor)
        per_page = offer["color"] if color_mode == "color" else offer["bw"]
    else:
        per_page = float(vendor["price_colour"] if color_mode == "color"
                         else vendor["price_bw"])
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
        "sides": sides,
        "copies": copies,
        "price": price,
        "payment_status": "pending",
    })
    db.log_activity("customer", "job_created",
                    f"job {job_id}: {label}, {color_mode}, {sides}-sided, "
                    f"x{copies}, ₹{price}",
                    vendor_id=vendor["id"])

    # Razorpay generates the order id; ours becomes the receipt, so the
    # shop can match a payment back to a job in their dashboard.
    result = rzp.create_order(
        vendor["razorpay_key_id"], vendor["razorpay_key_secret"],
        amount=price,
        receipt=f"PJOB_{job_id}_{int(time.time())}",
        notes={"job_id": job_id, "shop": vendor["shop_name"]},
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

    order_id = result["order_id"]
    db.update_job(job_id, {"order_id": order_id})
    db.insert_gateway_order({"order_id": order_id, "vendor_id": vendor["id"],
                             "purpose": "print_job", "amount": price})
    return jsonify({"ok": True, "job_id": job_id, "price": price,
                    "total_pages": total_pages,
                    "order_id": order_id,
                    "amount_paise": result["amount_paise"],
                    "key_id": result["key_id"],
                    "shop_name": vendor["shop_name"]})


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
    """Poll endpoint for the customer page. Also asks Razorpay directly
    (vendor's own keys) so payments confirm even when the public webhook
    can't reach this server (e.g. local testing)."""
    vendor = db.get_vendor_by_code(code)
    order = db.get_gateway_order(order_id)
    if not vendor or not order or order.get("vendor_id") != vendor["id"]:
        return jsonify({"error": "not found"}), 404
    if order["status"] == "pending":
        # Only 'paid' means captured. 'attempted' is still in flight, so we
        # keep waiting rather than cancelling a job the customer is paying for.
        if rzp.fetch_order(vendor["razorpay_key_id"],
                           vendor["razorpay_key_secret"], order_id) == "paid":
            _finalize_print_order(order, True)
            order = db.get_gateway_order(order_id)
    return jsonify({"order_id": order_id, "status": order["status"]})


# How long a pending print order stays reconcilable.
# 24h: a shop can close for the night with a paid job unconfirmed, and the
# customer will come back for their print the next morning. Razorpay keeps
# order records far longer, so asking about a day-old order is still valid.
RECONCILE_MAX_AGE_MIN = 24 * 60
RECONCILE_MIN_AGE_SEC = 45      # give the live poll a chance first


def reconcile_pending_orders(vendor_id=None, limit=25):
    """Ask Razorpay about print orders we still think are pending.

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
        status = rzp.fetch_order(vendor["razorpay_key_id"],
                                 vendor["razorpay_key_secret"],
                                 order["order_id"])
        if status == "paid":
            _finalize_print_order(order, True)
            db.log_activity("system", "payment_reconciled",
                            f"order {order['order_id']} was paid at Razorpay "
                            f"but never confirmed here; job released to print",
                            vendor_id=vendor["id"])
            changed += 1
    return changed


@app.route("/payment/webhook/vendor", methods=["POST"])
def vendor_payment_webhook():
    """Razorpay webhook for PRINT-JOB payments. Every vendor points their
    OWN Razorpay account's webhook at this one URL; we resolve the vendor
    from the order and verify against THAT vendor's webhook secret."""
    raw = request.get_data()          # RAW bytes — re-serialising breaks the HMAC
    ev = rzp.parse_webhook(raw)
    order_id = ev.get("order_id")
    if not order_id:
        return jsonify({"status": "ignored"}), 200
    order = db.get_gateway_order(order_id)
    if not order or order["purpose"] != "print_job":
        return jsonify({"error": "unknown order"}), 404
    vendor = db.get_vendor(order["vendor_id"])
    if not vendor:
        return jsonify({"error": "unknown vendor"}), 404
    if not rzp.verify_webhook(vendor.get("razorpay_webhook_secret"),
                              request.headers, raw):
        print(f"[webhook/vendor] REJECTED: bad signature for {order_id}")
        return jsonify({"error": "invalid signature"}), 401

    # Money is only ours once CAPTURED; 'authorized' can still be auto-refunded.
    if ev["event"] in (rzp.EVENT_PAYMENT_CAPTURED, rzp.EVENT_ORDER_PAID):
        _finalize_print_order(order, True, ev.get("payment_id"))
    elif ev["event"] == rzp.EVENT_PAYMENT_FAILED:
        _finalize_print_order(order, False, ev.get("payment_id"))
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
            if vendor["status"] in billing.ADMIN_DISABLED:
                flash(DISABLED_MESSAGE)
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


JOBS_PER_PAGE = 50


@app.route("/vendor")
@vendor_required
def vendor_dashboard(vendor):
    # Totals over ALL the shop's orders (the old cards counted only the
    # latest 50, so a busy shop saw a fraction of its real numbers).
    stats = db.vendor_job_stats(vendor["id"])
    # Every order, a page at a time, optionally filtered by status.
    show = request.args.get("show", "all")
    if show not in db.JOB_FILTERS:
        show = "all"
    per_page = JOBS_PER_PAGE
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    jobs, jobs_total = db.list_vendor_jobs(vendor["id"], show, per_page,
                                           (page - 1) * per_page)
    pages_total = max(1, -(-jobs_total // per_page))
    if page > pages_total:
        page = pages_total
        jobs, jobs_total = db.list_vendor_jobs(vendor["id"], show, per_page,
                                               (page - 1) * per_page)
    payments = db.list_payments(vendor_id=vendor["id"], limit=20)
    has_gateway = _vendor_gateway_ready(vendor)
    has_prices = float(vendor.get("price_bw") or 0) > 0
    base = db.PUBLIC_BASE_URL.rstrip("/")
    autopay_status = (vendor.get("autopay_status") or "").lower()
    return render_template(
        "vendor_dashboard.html", vendor=vendor, jobs=jobs, payments=payments,
        plans=config.PLANS, worker_exe=worker_exe_available(),
        has_gateway=has_gateway, has_prices=has_prices, stats=stats,
        show=show, page=page, pages_total=pages_total, jobs_total=jobs_total,
        per_page=per_page,
        duplex=duplex_offer(vendor), printers=printers_seen(vendor),
        live=billing.has_access(vendor) and has_gateway and has_prices,
        days_left=billing.days_until_renewal(vendor),
        autopay_on=autopay_status in billing.AUTOPAY_HEALTHY,
        autopay_supported=vendor["plan"] in AUTOPAY_INTERVAL,
        owes_first_month=billing.owes_first_month(vendor),
        autopay_pending=autopay_status == "created",
        # A debit failed: 'pending' while Razorpay retries, 'halted' after.
        autopay_failed=autopay_status in ("pending", "halted"),
        # Mandates approved before the 27th calendar keep their old price
        # and day until the vendor re-approves.
        autopay_outdated=_autopay_outdated(vendor),
        debit_day=config.AUTOPAY_DEBIT_DAY,
        renewal_amount=(billing.renewal_amount(vendor["plan"])
                        if vendor["plan"] != "lifetime" else 0),
        shop_url=f"{base}/{vendor['shop_code']}",
        # The credentials card shows the vendor their webhook URL; without
        # this it would render with a blank host and mislead them.
        base_url=base,
        webhook_url=f"{base}/payment/webhook/vendor")


@app.route("/vendor/recheck-payments", methods=["POST"])
@vendor_required
def vendor_recheck_payments(vendor):
    """Manually re-check pending payments with Razorpay (for a customer who
    says they paid but whose job is still awaiting payment)."""
    try:
        n = reconcile_pending_orders(vendor_id=vendor["id"], limit=50)
    except Exception as e:
        flash(f"Could not reach Razorpay: {e}")
        return redirect(url_for("vendor_dashboard"))
    flash(f"Checked pending payments with Razorpay — {n} updated."
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


@app.route("/vendor/duplex", methods=["POST"])
@vendor_required
def vendor_duplex(vendor):
    """Both-sides printing: the shop turns it on, picks the printer that
    prints both sides and sets its both-sides prices. Customers only see
    the option once all three are in place (see duplex_offer)."""
    enabled = request.form.get("duplex_enabled") == "1"
    printer = (request.form.get("printer_duplex") or "").strip()[:250]
    try:
        bw = round(float(request.form.get("price_bw_duplex") or 0), 2)
        colour = round(float(request.form.get("price_colour_duplex") or 0), 2)
        if bw < 0 or colour < 0:
            raise ValueError
    except ValueError:
        flash("Both-sides prices must be non-negative numbers.")
        return redirect(url_for("vendor_dashboard") + "#duplex")

    known = [p["name"] for p in printers_seen(vendor)]
    if printer and known and printer not in known:
        flash("That printer is not on your shop PC's list. Pick one from the list.")
        return redirect(url_for("vendor_dashboard") + "#duplex")
    if enabled and not printer:
        flash("Choose the printer that prints both sides before turning it on.")
        enabled = False
    elif enabled and bw <= 0:
        flash("Set a both-sides Black & White price before turning it on.")
        enabled = False

    db.update_vendor(vendor["id"], {
        "duplex_enabled": 1 if enabled else 0, "printer_duplex": printer or None,
        "price_bw_duplex": bw, "price_colour_duplex": colour})
    db.log_activity("vendor", "duplex_changed",
                    f"{'on' if enabled else 'off'}, printer {printer or '-'}, "
                    f"B/W ₹{bw}, Colour ₹{colour}", vendor_id=vendor["id"])
    if enabled:
        flash("Both-sides printing is on — customers can now choose it."
              + ("" if colour > 0 else " (Black & White only: no colour price set.)"))
    elif request.form.get("duplex_enabled") != "1":
        flash("Both-sides printing is off — customers print one side only.")
    return redirect(url_for("vendor_dashboard") + "#duplex")


@app.route("/vendor/razorpay", methods=["POST"])
@vendor_required
def vendor_razorpay(vendor):
    """Save this shop's OWN Razorpay account keys (every shop has a
    different account). Blank secret fields keep the stored value."""
    fields = {
        "razorpay_key_id": request.form.get("razorpay_key_id", "").strip(),
        "razorpay_key_secret": request.form.get("razorpay_key_secret", "").strip(),
        "razorpay_webhook_secret": request.form.get("razorpay_webhook_secret", "").strip(),
    }
    fields = {k: v for k, v in fields.items() if v}   # keep saved secrets
    db.update_vendor(vendor["id"], fields)
    db.log_activity("vendor", "razorpay_updated",
                    "shop Razorpay account keys changed", vendor_id=vendor["id"])
    if rzp.is_test_key(fields.get("razorpay_key_id") or vendor.get("razorpay_key_id")):
        flash("Saved — but these are TEST keys, so no real money will be "
              "collected. Use your live keys to take real payments.")
    else:
        flash("Razorpay account saved. Customers can now pay online at your "
              "shop page.")
    return redirect(url_for("vendor_dashboard"))


# ===========================================================================
# Vendor subscription payments — PLATFORM Razorpay account (spec §5, §6)
# ===========================================================================
def _start_subscription_checkout(vendor, purpose):
    """Create a platform-account Razorpay order for a vendor's first payment
    (plan fee + installation) or renewal (plan fee only). Returns a rendered
    checkout page or an error string."""
    if purpose == "first_payment":
        amounts = billing.first_payment(vendor["plan"])
        amount = amounts["total"]
        plan_fee = amounts["subscription_fee"]
        install_fee = amounts["installation_fee"]
        prefix = "SUB"
    else:
        amount = billing.renewal_amount(vendor["plan"])
        plan_fee, install_fee = amount, 0
        prefix = "REN"

    result = rzp.create_order(
        config.PLATFORM_RAZORPAY_KEY_ID,
        config.PLATFORM_RAZORPAY_KEY_SECRET,
        amount=amount,
        receipt=f"{prefix}_{vendor['id']}_{int(time.time())}",
        notes={"vendor_id": vendor["id"], "purpose": purpose,
               "shop": vendor["shop_name"]},
    )
    if not result["success"]:
        return None, result["error"]
    order_id = result["order_id"]
    db.insert_gateway_order({"order_id": order_id, "vendor_id": vendor["id"],
                             "purpose": purpose, "amount": amount})
    return render_template("pay.html", shop_name=vendor["shop_name"],
                           plan=vendor["plan"], amount=amount,
                           plan_fee=plan_fee, install_fee=install_fee,
                           autopay_next=(purpose == "first_payment"
                                         and billing.uses_autopay(vendor["plan"])),
                           debit_day=config.AUTOPAY_DEBIT_DAY,
                           order_id=order_id, purpose=purpose,
                           amount_paise=result["amount_paise"],
                           key_id=result["key_id"],
                           email=vendor.get("email"),
                           phone=vendor.get("phone")), None


# ===========================================================================
# Autopay — the vendor authorises a mandate once and Razorpay debits each
# cycle by itself (plan_type PERIODIC). We only choose the FIRST debit date
# (the 27th that pays their next unpaid month); Razorpay repeats it monthly.
# ===========================================================================
# Monthly only. Yearly is a one-time charge the vendor renews themselves.
def _charge_at_text(ts):
    """Razorpay reports the next debit as a unix timestamp; store it in the
    same YYYY-MM-DD... shape the dashboard already slices for display, on
    the billing calendar (IST) so its date matches the reminder logic."""
    if not ts:
        return None
    try:
        return billing.from_unix(ts).isoformat(" ", "seconds")
    except (TypeError, ValueError, OSError):
        return None


def _autopay_outdated(vendor):
    """A live mandate that does not debit on the 27th predates the billing
    calendar: it still charges the old price on the old day. The vendor has
    to re-approve once (the dashboard offers 'Update autopay')."""
    if (vendor.get("autopay_status") or "").lower() not in billing.AUTOPAY_HEALTHY:
        return False
    nxt = vendor.get("autopay_next_charge")
    if not nxt:
        return False
    try:
        return datetime.fromisoformat(str(nxt)).day != config.AUTOPAY_DEBIT_DAY
    except ValueError:
        return False


AUTOPAY_INTERVAL = {"monthly": ("monthly", 1)}
# Cycles a mandate may run before re-authorisation: ten years of months.
AUTOPAY_TOTAL_COUNT = 120


@app.route("/vendor/autopay/start", methods=["POST"])
@vendor_required
def vendor_autopay_start(vendor):
    """Create the mandate and send the vendor to Razorpay to approve it."""
    if vendor["plan"] == "lifetime":
        flash("Lifetime plans have nothing to renew — autopay is not needed.")
        return redirect(url_for("vendor_dashboard"))
    if vendor["plan"] not in AUTOPAY_INTERVAL:
        flash("Autopay is only available on the monthly plan. Yearly is a "
              "one-time payment you renew when it runs out.")
        return redirect(url_for("vendor_dashboard"))
    if not rzp.configured(config.PLATFORM_RAZORPAY_KEY_ID,
                          config.PLATFORM_RAZORPAY_KEY_SECRET):
        flash("Autopay is not available yet — please renew manually.")
        return redirect(url_for("vendor_dashboard"))

    amount = billing.renewal_amount(vendor["plan"])
    if amount > rzp.UPI_AUTOPAY_NO_AFA_LIMIT:
        flash(f"₹{amount} is above the ₹{rzp.UPI_AUTOPAY_NO_AFA_LIMIT} "
              f"UPI Autopay limit — please renew manually each period.")
        return redirect(url_for("vendor_dashboard"))

    period, interval = AUTOPAY_INTERVAL[vendor["plan"]]

    # A plan is the recurring price; reuse this vendor's if they have one, so
    # re-enabling autopay does not litter the account with duplicate plans —
    # but only while its price is still current. A plan created at an old
    # fee would keep debiting the old fee forever.
    plan_id = vendor.get("autopay_plan_id")
    if plan_id:
        existing = rzp.fetch_plan(config.PLATFORM_RAZORPAY_KEY_ID,
                                  config.PLATFORM_RAZORPAY_KEY_SECRET, plan_id)
        if not existing or rzp.plan_amount(existing) != float(amount):
            plan_id = None
    if not plan_id:
        pres = rzp.create_plan(
            config.PLATFORM_RAZORPAY_KEY_ID,
            config.PLATFORM_RAZORPAY_KEY_SECRET,
            amount=amount, period=period, interval=interval,
            name=f"PrintHub {vendor['plan']} plan")
        if not pres["success"]:
            flash(f"Could not start autopay: {pres.get('error', 'unknown error')}")
            return redirect(url_for("vendor_dashboard"))
        plan_id = pres["plan_id"]
        db.update_vendor(vendor["id"], {"autopay_plan_id": plan_id})

    # The first debit is the 27th that pays this vendor's next unpaid month;
    # Razorpay then repeats it on the 27th of every month.
    first_charge = billing.next_autopay_start(vendor)

    # One mandate per vendor: replacing one (an outdated price/day, or a
    # halted mandate) cancels the old one first so it can never debit too.
    old_sub = vendor.get("autopay_subscription_id")
    if old_sub and (vendor.get("autopay_status") or "").lower() not in (
            "cancelled", "completed", "expired"):
        rzp.cancel_subscription(config.PLATFORM_RAZORPAY_KEY_ID,
                                config.PLATFORM_RAZORPAY_KEY_SECRET, old_sub)
        db.log_activity("vendor", "autopay_replaced",
                        f"mandate {old_sub} cancelled for a new one",
                        vendor_id=vendor["id"])

    # total_count is how many cycles the mandate may run before the vendor
    # re-authorises. 120 months is a decade — effectively open-ended.
    res = rzp.create_subscription(
        config.PLATFORM_RAZORPAY_KEY_ID,
        config.PLATFORM_RAZORPAY_KEY_SECRET,
        plan_id=plan_id,
        total_count=AUTOPAY_TOTAL_COUNT,
        start_at=billing.to_unix(first_charge),
        notes={"vendor_id": vendor["id"], "shop": vendor["shop_name"]},
    )
    if not res["success"] or not res.get("subscription_id"):
        flash(f"Could not start autopay: {res.get('error', 'unknown error')}")
        return redirect(url_for("vendor_dashboard"))

    db.update_vendor(vendor["id"], {
        "autopay_subscription_id": res["subscription_id"],
        "autopay_status": (res.get("status") or "created").lower(),
        "autopay_next_charge": first_charge.isoformat(" ", "seconds"),
    })
    db.log_activity("vendor", "autopay_started",
                    f"mandate {res['subscription_id']} for ₹{amount} "
                    f"per {period}, first debit {first_charge:%Y-%m-%d}",
                    vendor_id=vendor["id"])
    # The first debit pays the month starting the 1st after it. If the
    # vendor is not paid up to that 1st (they are overdue, or this month's
    # debit day already passed), the gap must be paid by hand.
    renews_at = vendor.get("renews_at")
    if isinstance(renews_at, str):
        renews_at = datetime.fromisoformat(renews_at)
    pay_now = (not renews_at
               or renews_at < billing.next_month_start(first_charge))
    return render_template("autopay.html", vendor=vendor, amount=amount,
                           interval=period, first_charge=first_charge,
                           pay_now=pay_now,
                           subscription_id=res["subscription_id"],
                           key_id=res["key_id"])


@app.route("/vendor/autopay/return")
@vendor_required
def vendor_autopay_return(vendor):
    """Where Razorpay sends the vendor after they approve the mandate. The
    webhook is authoritative; this just reports what the API says now."""
    sub_id = vendor.get("autopay_subscription_id")
    status = None
    if sub_id:
        d = rzp.fetch_subscription(config.PLATFORM_RAZORPAY_KEY_ID,
                                   config.PLATFORM_RAZORPAY_KEY_SECRET,
                                   sub_id) or {}
        status = (d.get("status") or "").lower()
        if status:
            db.update_vendor(vendor["id"], {
                "autopay_status": status,
                "autopay_next_charge": _charge_at_text(d.get("charge_at")),
            })
    # 'authenticated' means the mandate is approved and the first charge is
    # on its way; 'active' means the billing cycle has started.
    if status in ("active", "authenticated"):
        flash("Autopay is on — your renewal will be paid automatically.")
    elif status == "created":
        flash("Autopay is still being confirmed. This can take a little "
              "while; the dashboard will update on its own.")
    else:
        flash("Autopay was not set up. You can try again or keep renewing "
              "manually.")
    return redirect(url_for("vendor_dashboard"))


@app.route("/vendor/autopay/cancel", methods=["POST"])
@vendor_required
def vendor_autopay_cancel(vendor):
    sub_id = vendor.get("autopay_subscription_id")
    if sub_id:
        rzp.cancel_subscription(config.PLATFORM_RAZORPAY_KEY_ID,
                                config.PLATFORM_RAZORPAY_KEY_SECRET, sub_id)
        db.log_activity("vendor", "autopay_cancelled", f"mandate {sub_id}",
                        vendor_id=vendor["id"])
    db.update_vendor(vendor["id"], {"autopay_status": "cancelled",
                                    "autopay_next_charge": None})
    flash("Autopay turned off. Remember to renew manually before your "
          "subscription expires.")
    return redirect(url_for("vendor_dashboard"))


@app.route("/payment/webhook/autopay", methods=["POST"])
def autopay_webhook():
    """Razorpay tells us about mandate authorisation and every automatic
    debit. A successful recurring charge extends the subscription exactly as
    a manual renewal would."""
    raw = request.get_data()          # RAW bytes — re-serialising breaks the HMAC
    # The autopay webhook is a separate endpoint in the Razorpay dashboard
    # and therefore carries its own secret.
    if not rzp.verify_webhook(config.AUTOPAY_RAZORPAY_WEBHOOK_SECRET,
                              request.headers, raw):
        print("[webhook/autopay] REJECTED: bad signature")
        return jsonify({"error": "invalid signature"}), 401

    ev = rzp.parse_webhook(raw)
    sub_id = ev.get("subscription_id")
    if not sub_id:
        return jsonify({"status": "ignored"}), 200
    vendor = db.get_vendor_by_autopay(sub_id)
    if not vendor:
        print(f"[webhook/autopay] unknown subscription {sub_id}")
        return jsonify({"status": "unknown subscription"}), 200

    fields = {}
    if ev.get("subscription_status"):
        fields["autopay_status"] = ev["subscription_status"]
    if ev.get("charge_at"):
        fields["autopay_next_charge"] = _charge_at_text(ev["charge_at"])

    event = ev.get("event")
    if event == rzp.EVENT_SUB_CHARGED:
        # The whole point: every automatic debit renews the subscription.
        # This covers the first charge at authorisation too, since Razorpay
        # keeps that one rather than refunding it.
        # Keyed on the payment id so a redelivered webhook is a no-op.
        recorded = billing.record_renewal_payment(
            vendor, method="autopay",
            reference=ev.get("payment_id") or None,
            amount=ev.get("payment_amount"))
        if recorded is not None:
            db.log_activity("system", "autopay_charged",
                            f"₹{ev.get('payment_amount')} debited automatically "
                            f"(mandate {sub_id})", vendor_id=vendor["id"])
    elif event == rzp.EVENT_SUB_AUTHENTICATED:
        db.log_activity("system", "autopay_authorised", f"mandate {sub_id}",
                        vendor_id=vendor["id"])
    elif event == rzp.EVENT_SUB_PENDING:
        # A debit failed; Razorpay will retry before giving up.
        db.log_activity("system", "autopay_failed",
                        f"automatic debit failed on mandate {sub_id} — "
                        f"Razorpay will retry", vendor_id=vendor["id"])
    elif event == rzp.EVENT_SUB_HALTED:
        db.log_activity("system", "autopay_halted",
                        f"mandate {sub_id} halted after failed retries — the "
                        f"vendor must renew manually", vendor_id=vendor["id"])
    elif event == rzp.EVENT_SUB_CANCELLED:
        fields["autopay_next_charge"] = None
        db.log_activity("system", "autopay_cancelled",
                        f"mandate {sub_id} cancelled at Razorpay",
                        vendor_id=vendor["id"])

    if fields:
        db.update_vendor(vendor["id"], fields)
    return jsonify({"status": "ok"})


@app.route("/pay/onboard/<code>")
def pay_onboard(code):
    """Public onboarding payment link (sent to a freshly registered vendor).
    On success the auto-generated credentials are shown ONCE (spec §6.4)."""
    vendor = db.get_vendor_by_code(code)
    if not vendor:
        return render_template("pay_result.html", ok=False,
                               message="Unknown shop code."), 404
    if vendor["status"] in billing.ADMIN_DISABLED:
        return render_template("pay_result.html", ok=False,
                               message=DISABLED_MESSAGE)
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
        creds = billing.record_first_payment(vendor, method="razorpay",
                                             reference=order["order_id"])
    elif order["purpose"] == "renewal":
        billing.record_renewal_payment(vendor, method="razorpay",
                                       reference=order["order_id"])
    db.update_gateway_order(order["order_id"], {
        "status": "paid", "transaction_id": transaction_id,
        "meta": None if (creds and consume_creds) else
                (json.dumps(creds) if creds else None)})
    return "paid", (creds if consume_creds else None)


@app.route("/pay/return")
def pay_return():
    """Vendor lands here after the platform-account checkout. Confirms the
    order with Razorpay directly (never trusts the redirect)."""
    order_id = request.args.get("order_id", "")
    order = db.get_gateway_order(order_id)
    if not order or order["purpose"] not in ("first_payment", "renewal"):
        return render_template("pay_result.html", ok=False,
                               message="Unknown payment order."), 404
    creds = None
    if order["status"] == "pending":
        paid = rzp.fetch_order(config.PLATFORM_RAZORPAY_KEY_ID,
                               config.PLATFORM_RAZORPAY_KEY_SECRET,
                               order_id) == "paid"
        status, creds = _finalize_subscription_order(
            order, paid, consume_creds=True)
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
        if billing.uses_autopay(vendor["plan"]):
            message = (f"Subscription for {vendor['shop_name']} is active. "
                       f"Amount paid: ₹{order['amount']}. Sign in and turn on "
                       f"autopay so ₹{billing.renewal_amount(vendor['plan'])} "
                       f"is paid automatically on the "
                       f"{config.AUTOPAY_DEBIT_DAY}th of each month.")
        else:
            message = (f"Subscription for {vendor['shop_name']} is active. "
                       f"Amount paid: ₹{order['amount']}.")
    else:
        message = (f"Renewal received — subscription extended for "
                   f"{vendor['shop_name']}. Amount paid: ₹{order['amount']}.")
    return render_template("pay_result.html", ok=True, message=message,
                           creds=creds, vendor=vendor)


@app.route("/payment/webhook/platform", methods=["POST"])
def platform_payment_webhook():
    """Razorpay webhook for the PLATFORM account (installation/renewals)."""
    raw = request.get_data()          # RAW bytes — re-serialising breaks the HMAC
    if not rzp.verify_webhook(config.PLATFORM_RAZORPAY_WEBHOOK_SECRET,
                              request.headers, raw):
        print("[webhook/platform] REJECTED: bad signature")
        return jsonify({"error": "invalid signature"}), 401
    ev = rzp.parse_webhook(raw)
    order_id = ev.get("order_id")
    if not order_id:
        return jsonify({"status": "ignored"}), 200
    order = db.get_gateway_order(order_id)
    if not order or order["purpose"] not in ("first_payment", "renewal"):
        return jsonify({"error": "unknown order"}), 404

    # Only CAPTURED is money in the account; 'authorized' auto-refunds later.
    if ev["event"] in (rzp.EVENT_PAYMENT_CAPTURED, rzp.EVENT_ORDER_PAID):
        _finalize_subscription_order(order, True, ev.get("payment_id"))
    elif ev["event"] == rzp.EVENT_PAYMENT_FAILED:
        _finalize_subscription_order(order, False, ev.get("payment_id"))
    return jsonify({"status": "ok"})


# ===========================================================================
# Worker API (desktop app)
# ===========================================================================
def _printer_config(vendor):
    return {k: vendor.get(k) for k in (
        "printer_mode", "printer_single", "tray_single",
        "printer_bw", "tray_bw", "printer_colour", "tray_colour",
        "printer_duplex")}


def printers_seen(vendor):
    """Printers the shop app last reported: [{"name", "duplex"}]."""
    try:
        items = json.loads(vendor.get("printers_seen") or "[]")
        return [p for p in items if isinstance(p, dict) and p.get("name")]
    except (TypeError, ValueError):
        return []


@app.route("/worker/api/printers", methods=["POST"])
def worker_report_printers():
    """The shop app reports the PC's printers (and which can print both
    sides) so the shop can pick its both-sides printer in the portal."""
    vendor = worker_vendor()
    if not vendor:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    items = []
    for p in (data.get("printers") or [])[:30]:
        name = str((p or {}).get("name") or "").strip()[:250]
        if name:
            duplex = p.get("duplex")
            items.append({"name": name,
                          "duplex": None if duplex is None else bool(duplex)})
    db.update_vendor(vendor["id"], {
        "printers_seen": json.dumps(items),
        "printers_seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return jsonify({"status": "ok", "count": len(items)})


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
    # ask Razorpay directly so a paid job still reaches the printer.
    try:
        reconcile_pending_orders(vendor_id=vendor["id"])
    except Exception as e:
        print(f"[reconcile] {e}")

    # Move expired subscriptions into grace/suspended even for shops whose
    # owners never open the dashboard.
    try:
        billing.sweep_subscriptions()
    except Exception as e:
        print(f"[sweep] {e}")

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
        # Both-sides orders go to the printer the shop chose for them in its
        # portal (the shop app prefers this over its own B/W/colour routing).
        if j.get("sides") == "double" and vendor.get("printer_duplex"):
            j["printer_target"] = vendor["printer_duplex"]
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
    billing.sweep_subscriptions()
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
    # Activation happens ONLY through a real Razorpay payment on this link.
    flash(f"Vendor registered. Send them this payment link to activate — "
          f"₹{amounts['total']} (₹{amounts['subscription_fee']} {plan} + "
          f"₹{amounts['installation_fee']} installation): "
          f"{request.url_root.rstrip('/')}/pay/onboard/{shop_code}")
    return redirect(url_for("admin_panel"))


def _cancel_autopay(vendor, why):
    """Stop a vendor's mandate so a disabled or deleted shop is never
    debited. Cancellation is final at Razorpay; the vendor can approve a new
    mandate after being enabled again."""
    sub_id = vendor.get("autopay_subscription_id")
    status = (vendor.get("autopay_status") or "").lower()
    if not sub_id or status in ("cancelled", "completed", "expired"):
        return
    res = rzp.cancel_subscription(config.PLATFORM_RAZORPAY_KEY_ID,
                                  config.PLATFORM_RAZORPAY_KEY_SECRET, sub_id)
    db.log_activity("admin", "autopay_cancelled",
                    f"mandate {sub_id} cancelled ({why})"
                    + ("" if res.get("success") else
                       f" — Razorpay said: {res.get('error')}"),
                    vendor_id=vendor["id"])
    db.update_vendor(vendor["id"], {"autopay_status": "cancelled",
                                    "autopay_next_charge": None})


@app.route("/admin/vendors/<int:vendor_id>/disable", methods=["POST"])
@admin_required
def admin_disable_vendor(vendor_id):
    """Take a shop offline by hand: customers can't order, the shop app
    stops printing and the owner can't sign in. Billing never undoes this;
    only Enable does. Autopay is cancelled so they are not charged."""
    vendor = db.get_vendor(vendor_id)
    if not vendor:
        flash("Vendor not found.")
        return redirect(url_for("admin_panel"))
    if vendor["status"] in billing.ADMIN_DISABLED:
        flash(f"{vendor['shop_name']} is already disabled.")
        return redirect(url_for("admin_panel"))
    had_autopay = (vendor.get("autopay_status") or "").lower() not in (
        "", "cancelled", "completed", "expired")
    _cancel_autopay(vendor, "vendor disabled by admin")
    db.update_vendor(vendor_id, {"status": "disabled"})
    db.log_activity("admin", "vendor_disabled",
                    f"{vendor['shop_name']} disabled (was {vendor['status']})",
                    vendor_id=vendor_id)
    flash(f"{vendor['shop_name']} disabled — the shop is offline"
          + (" and its autopay was cancelled." if had_autopay else "."))
    return redirect(url_for("admin_panel"))


@app.route("/admin/vendors/<int:vendor_id>/enable", methods=["POST"])
@admin_required
def admin_enable_vendor(vendor_id):
    """Undo Disable. The vendor returns to whatever their payments entitle
    them to: never paid -> pending payment; paid up -> active; overdue ->
    grace/suspended, exactly as if they had never been disabled."""
    vendor = db.get_vendor(vendor_id)
    if not vendor or vendor["status"] not in billing.ADMIN_DISABLED:
        flash("That vendor is not disabled.")
        return redirect(url_for("admin_panel"))
    status = "active" if vendor.get("login_id") else "pending_payment"
    db.update_vendor(vendor_id, {"status": status, "grace_until": None})
    vendor = billing.refresh_status(db.get_vendor(vendor_id))
    db.log_activity("admin", "vendor_enabled",
                    f"{vendor['shop_name']} enabled (now {vendor['status']})",
                    vendor_id=vendor_id)
    note = {"active": "the shop is live again.",
            "pending_payment": "they still need to pay the onboarding link.",
            "grace": "today is their last day to pay.",
            "suspended": "but their fee is unpaid, so the shop stays offline "
                         "until they pay."}.get(vendor["status"], "")
    flash(f"{vendor['shop_name']} enabled — {note}")
    return redirect(url_for("admin_panel"))


@app.route("/admin/vendors/<int:vendor_id>/delete", methods=["POST"])
@admin_required
def admin_delete_vendor(vendor_id):
    """Permanently remove a vendor, their print jobs and files. Requires the
    shop code typed back as confirmation. Payment records and the activity
    log are kept for the accounts."""
    vendor = db.get_vendor(vendor_id)
    if not vendor:
        flash("Vendor not found.")
        return redirect(url_for("admin_panel"))
    if request.form.get("confirm_code", "").strip().lower() != vendor["shop_code"]:
        flash(f"Not deleted — type the shop code '{vendor['shop_code']}' "
              f"exactly to confirm.")
        return redirect(url_for("admin_panel"))
    _cancel_autopay(vendor, "vendor deleted by admin")
    db.delete_vendor(vendor_id)
    db.log_activity("admin", "vendor_deleted",
                    f"{vendor['shop_name']} (/{vendor['shop_code']}, "
                    f"{vendor['plan']}, login {vendor.get('login_id') or '—'}) "
                    f"deleted with its print jobs; payments kept")
    flash(f"{vendor['shop_name']} deleted.")
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
