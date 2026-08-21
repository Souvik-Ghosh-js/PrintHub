"""Page Cutting & AI Processing (spec §3) — wraps the DocumentAI package.

- Auto Precise Cut: ML corner detection (docunet_mobile.onnx) with a
  classical-OpenCV fallback when the model is missing or unsure.
- Light / Burn filter: docenh's enhancement pipeline (shadow removal,
  white balance, contrast, sharpen; gray / B&W modes).
- Manual Adjustment happens client-side (Cropper.js corner handles).
"""
import os

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCUNET_ONNX = os.path.join(BASE_DIR, "weights", "docunet_mobile.onnx")

# DocAligner FastViT_T8 (DocsaidLab, Apache-2.0) — trained on MIDV-500/2020,
# i.e. photographed ID cards and passports, which is exactly our workload.
# Benchmarked against the old docunet model on Aadhaar/PAN/Voter scenes:
#   easy scenes  18/18 vs 9/18 correct, 0.75% vs 6.0% mean corner error
#   hard scenes  10/12 vs 1/12 correct (shadow, hand, clutter, low light)
# at the same speed (~20 ms on CPU).
DOCALIGNER_ONNX = os.path.join(BASE_DIR, "weights", "docaligner_fastvit_t8.onnx")

# Below this confidence the detection is likely a full-frame fallback quad,
# so the frontend should keep Cropper.js's own default box instead.
DETECT_MIN_CONFIDENCE = 0.30
# DocAligner emits a per-corner heatmap; the weakest corner's peak is a good
# reliability signal. Below this we prefer another detector.
DOCALIGNER_MIN_PEAK = 0.35

_DA_SESSION = None


def _docaligner_session():
    """Lazily create the ONNX session (kept warm between requests)."""
    global _DA_SESSION
    if _DA_SESSION is None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2          # small shared instance
        _DA_SESSION = ort.InferenceSession(DOCALIGNER_ONNX, so,
                                           providers=["CPUExecutionProvider"])
    return _DA_SESSION


def find_corners_docaligner(bgr):
    """Corner detection with DocAligner. Returns (corners (4,2) TL,TR,BR,BL
    in image pixels, confidence 0..1)."""
    h, w = bgr.shape[:2]
    sess = _docaligner_session()
    inp = cv2.resize(bgr, (256, 256)).astype(np.float32) / 255.0
    inp = inp.transpose(2, 0, 1)[None]
    heat = sess.run(None, {sess.get_inputs()[0].name: inp})[0][0]   # (4,H,W)

    pts, peaks = [], []
    for c in range(heat.shape[0]):
        m = heat[c]
        yy, xx = divmod(int(np.argmax(m)), m.shape[1])
        peaks.append(float(m[yy, xx]))
        # sub-pixel: centroid of the 3x3 neighbourhood around the peak
        y0, y1 = max(0, yy - 1), min(m.shape[0], yy + 2)
        x0, x1 = max(0, xx - 1), min(m.shape[1], xx + 2)
        patch = m[y0:y1, x0:x1].astype(np.float64)
        tot = patch.sum()
        if tot > 1e-6:
            gy, gx = np.mgrid[y0:y1, x0:x1]
            yy = float((gy * patch).sum() / tot)
            xx = float((gx * patch).sum() / tot)
        pts.append([xx / m.shape[1] * w, yy / m.shape[0] * h])

    return np.asarray(pts, dtype=np.float32), float(np.min(peaks))


def _is_multi_panel_shape(quad, min_aspect=1.95):
    """A folded multi-panel document is markedly wider (or taller) than a
    single ID card, which is about 1.6:1. Used to avoid overriding a good
    single-card detection with a sprawling panel box."""
    q = order_corners(np.asarray(quad, dtype=np.float32))
    wid = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    hei = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    if min(wid, hei) < 1e-6:
        return False
    return (max(wid, hei) / min(wid, hei)) >= min_aspect


def find_document_panels(bgr, min_frac=0.045):
    """Boundary of a document made of SEVERAL panels.

    The old long-format Aadhaar letter is printed as two panels side by side
    with a cut line between them. Contour detectors lock onto one panel and
    crop the other half away, so here we find every paper-like region and
    return the quad that encloses all of them.

    Returns (corners (4,2) or None, confidence).
    """
    h, w = bgr.shape[:2]
    scale = 800.0 / max(h, w)
    small = cv2.resize(bgr, None, fx=scale, fy=scale) if scale < 1 else bgr.copy()
    # A document photographed edge-to-edge has no background border, so its
    # contour merges with the frame. Add a synthetic dark border, then undo
    # the offset afterwards.
    pad = max(6, int(0.02 * max(small.shape[:2])))
    small = cv2.copyMakeBorder(small, pad, pad, pad, pad,
                               cv2.BORDER_CONSTANT, value=(0, 0, 0))
    sh, sw = small.shape[:2]
    area = float(sh * sw)

    # Paper is bright and near-neutral; the backgrounds these documents are
    # photographed on (pink sheet, wood, cloth) are darker and/or coloured.
    # Combine three cues and keep whichever separates panels best.
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    b, g, r = cv2.split(small.astype(np.int16))
    # neutrality: paper has little spread between channels
    spread = (np.maximum(np.maximum(b, g), r) -
              np.minimum(np.minimum(b, g), r)).astype(np.uint8)

    masks = []
    otsu_v = cv2.threshold(val, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    masks.append(((otsu_v > 0) & (spread < 60)).astype(np.uint8) * 255)
    masks.append(((val > 150) & (sat < 70)).astype(np.uint8) * 255)
    masks.append(((spread < 40) & (val > int(np.percentile(val, 45)))
                  ).astype(np.uint8) * 255)

    best_panels, best_mask_score, best_nregions = None, -1.0, 0
    kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    kernel_light = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    for m in masks:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel_open)
        # Light closing keeps the cut line between panels visible, so we can
        # tell a folded letter from a single sheet...
        light = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel_light)
        lc, _ = cv2.findContours(light, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        n_regions = len([c for c in lc if cv2.contourArea(c) > area * min_frac])
        # ...while strong closing fills the print inside each panel, giving a
        # clean outline to fit the quad to.
        strong = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel_close)
        cnts, _ = cv2.findContours(strong, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cand = [c for c in cnts if cv2.contourArea(c) > area * min_frac]
        if not cand:
            continue
        cov = sum(cv2.contourArea(c) for c in cand) / area
        # prefer masks that cover a believable amount of the frame; a mask
        # covering nearly everything has simply merged doc and background.
        score = cov if cov < 0.92 else 0.1
        if score > best_mask_score:
            best_mask_score, best_panels, best_nregions = score, cand, n_regions

    panels = best_panels
    if not panels:
        return None, 0.0

    # Keep panels that are roughly as "deep" as the biggest one and close to
    # it, so we merge the halves of one letter without swallowing clutter.
    boxes = [cv2.boundingRect(c) for c in panels]
    big = max(range(len(panels)), key=lambda i: cv2.contourArea(panels[i]))
    bx, by, bw, bh = boxes[big]
    keep = []
    for c, (x, y, ww, hh) in zip(panels, boxes):
        vertical_overlap = (min(by + bh, y + hh) - max(by, y)) / max(1, min(bh, hh))
        horizontal_overlap = (min(bx + bw, x + ww) - max(bx, x)) / max(1, min(bw, ww))
        near = (vertical_overlap > 0.55 or horizontal_overlap > 0.55)
        if c is panels[big] or near:
            keep.append(c)
    if not keep:
        return None, 0.0

    allpts = np.vstack(keep)
    covered = sum(cv2.contourArea(c) for c in keep) / area
    if covered < min_frac:
        return None, 0.0

    # The panels must line up like a folded letter: similar depth, sitting
    # side by side (or stacked), not scattered around the frame.
    rects = [cv2.boundingRect(c) for c in keep]
    xs0 = min(r[0] for r in rects); ys0 = min(r[1] for r in rects)
    xs1 = max(r[0] + r[2] for r in rects); ys1 = max(r[1] + r[3] for r in rects)
    span_w, span_h = xs1 - xs0, ys1 - ys0
    if span_w < 1 or span_h < 1:
        return None, 0.0
    side_by_side = all(
        (min(ys1, r[1] + r[3]) - max(ys0, r[1])) > 0.55 * span_h for r in rects)
    stacked = all(
        (min(xs1, r[0] + r[2]) - max(xs0, r[0])) > 0.55 * span_w for r in rects)
    if not (side_by_side or stacked):
        return None, 0.0

    quad = cv2.boxPoints(cv2.minAreaRect(allpts)).astype(np.float32)
    quad -= pad                       # remove the synthetic border
    if scale < 1:
        quad /= scale
    quad[:, 0] = np.clip(quad[:, 0], 0, w - 1)
    quad[:, 1] = np.clip(quad[:, 1], 0, h - 1)

    # Confidence: how much of the enclosing quad is actually paper (a tight
    # fit around two panels scores high; a sprawling box over clutter does not).
    qa = cv2.contourArea(quad) / float(w * h)
    if qa > 0.97:            # essentially the whole frame -> not a real find
        return None, 0.0
    fill = min(1.0, covered / max(qa, 1e-6))
    if fill < 0.62:          # lots of non-paper inside the box
        return None, 0.0

    # This detector is for MULTI-PANEL documents. Accept only when the mask
    # actually saw separate panels, or the outline is clearly letter-shaped
    # (much wider than a single ID card). Otherwise let the single-document
    # detectors handle it — they are more accurate there.
    quad_ordered = order_corners(quad)
    if not (best_nregions >= 2 or _is_multi_panel_shape(quad_ordered)):
        return None, 0.0

    conf = float(np.clip(0.45 + 0.5 * fill, 0.0, 0.97))
    return quad_ordered, conf


def detect_corners(bgr):
    """Detect document corners.

    Order of preference: DocAligner (accurate on photographed ID cards),
    then the older docunet model, then classical OpenCV. Returns
    (corners (4,2), confidence, detector-name).
    """
    results = []

    # Multi-panel documents (the old long-format Aadhaar letter is two panels
    # side by side) must be handled before the single-card detectors, which
    # would happily crop one panel and throw the other half away.
    panels_quad = None
    try:
        panels_quad, panels_conf = find_document_panels(bgr)
        if panels_quad is not None:
            results.append((panels_quad, panels_conf, "panels"))
    except Exception as e:
        print(f"[docscan] panel detector failed ({e})")

    if os.path.exists(DOCALIGNER_ONNX):
        try:
            corners, conf = find_corners_docaligner(bgr)
            corners = order_corners(corners)
            if conf >= DOCALIGNER_MIN_PEAK:
                # If a multi-panel document was found and DocAligner's quad
                # covers appreciably less of it, DocAligner has locked onto a
                # single panel — prefer the full document.
                if panels_quad is not None and panels_conf >= 0.6:
                    a_da = abs(cv2.contourArea(corners.astype(np.float32)))
                    a_pn = abs(cv2.contourArea(panels_quad.astype(np.float32)))
                    if a_pn > a_da * 1.35 and _is_multi_panel_shape(panels_quad):
                        return panels_quad, panels_conf, "panels"
                return corners, conf, "docaligner"
            results.append((corners, conf, "docaligner"))
        except Exception as e:
            print(f"[docscan] DocAligner unavailable ({e}); trying other detectors")

    if os.path.exists(DOCUNET_ONNX):
        try:
            from docenh.geometry.ml_corners import find_document_corners_ml
            corners, conf = find_document_corners_ml(bgr, onnx_path=DOCUNET_ONNX)
            corners = order_corners(np.asarray(corners, dtype=np.float32))
            # Only take this shortcut when no larger multi-panel document was
            # found; otherwise fall through so the two can be compared.
            if conf >= DETECT_MIN_CONFIDENCE and panels_quad is None:
                return corners, conf, "ml"
            results.append((corners, conf, "ml"))
        except Exception as e:
            print(f"[docscan] docunet unavailable ({e}); falling back to classical CV")

    from docenh.geometry.corners import find_document_corners
    corners, conf = find_document_corners(bgr)
    corners = order_corners(np.asarray(corners, dtype=np.float32))
    results.append((corners, conf, "classical"))

    # A confident multi-panel result wins when the single-document detectors
    # have clearly cropped part of the document away (the long-format Aadhaar
    # letter: two panels with a cut line, where contour detectors keep one).
    if (panels_quad is not None and panels_conf >= 0.6
            and _is_multi_panel_shape(panels_quad)):
        a_pn = abs(cv2.contourArea(panels_quad.astype(np.float32)))
        others = [r for r in results if r[2] != "panels"]
        if others:
            a_best = max(abs(cv2.contourArea(np.asarray(r[0], np.float32)))
                         for r in others)
            if a_pn > a_best * 1.35:
                return panels_quad, panels_conf, "panels"

    best = max(results, key=lambda r: r[1])
    return best[0], best[1], best[2]


def decode_image(data: bytes):
    """Raw upload bytes -> BGR image (or None)."""
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def enhance_image(bgr, mode: str):
    """Light/Burn filter (spec §3.1). mode: none|color|gray|bw.

    'color' = shadow removal + white balance + contrast + sharpen;
    'gray'/'bw' additionally convert. 'none' returns the input unchanged.
    """
    if mode in (None, "", "none"):
        return bgr
    from docenh.enhancement.enhance import enhance
    return enhance(bgr, mode=mode)


def encode_jpeg(bgr, quality=92) -> bytes:
    ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return enc.tobytes()


def warp_perspective(bgr, corners):
    """Perspective-correct a photo to a flat, front-parallel page (spec §3).

    corners: 4 (x, y) points in image pixels, ordered TL, TR, BR, BL — what
    the customer's drag handles produce. Returns the warped BGR image.
    """
    from docenh.perspective.warp import warp_document
    pts = np.asarray(corners, dtype=np.float32).reshape(4, 2)
    warped, _H = warp_document(bgr, pts)
    return warped


def order_corners(pts):
    """Sort 4 arbitrary points into TL, TR, BR, BL."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)],    # TL: smallest x+y
                     pts[np.argmin(d)],    # TR: smallest y-x
                     pts[np.argmax(s)],    # BR: largest x+y
                     pts[np.argmax(d)]],   # BL: largest y-x
                    dtype=np.float32)


def tilt_image(bgr, horizontal=0.0, vertical=0.0, rotate=0.0, zoom=1.0):
    """Phone-gallery style perspective adjustment.

    Instead of asking where the document's corners are, this tilts the whole
    image around its axes the way the Perspective tool in a phone's photo
    editor does:
        horizontal  -1..1  swing left / right (rotate about the vertical axis)
        vertical    -1..1  lean back / forward (rotate about the horizontal axis)
        rotate      degrees, straighten a crooked shot
        zoom        >1 crops in, used to hide the empty corners a tilt creates
    Returns a BGR image the same size as the input.
    """
    h, w = bgr.shape[:2]
    hf = float(np.clip(horizontal, -1.0, 1.0))
    vf = float(np.clip(vertical, -1.0, 1.0))

    # Move the source corners inward on one side; the homography then
    # stretches that side back out, which is exactly a tilt about that axis.
    dx, dy = 0.22 * w * hf, 0.22 * h * vf
    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    dst = np.array([
        [0 + max(dx, 0), 0 + max(dy, 0)],
        [w + min(dx, 0), 0 - min(dy, 0)],
        [w + min(dx, 0), h + min(dy, 0)],
        [0 + max(dx, 0), h - max(dy, 0)],
    ], dtype=np.float32)

    out = bgr
    if abs(hf) > 1e-3 or abs(vf) > 1e-3:
        M = cv2.getPerspectiveTransform(src, dst)
        out = cv2.warpPerspective(bgr, M, (w, h), flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_REPLICATE)

    if abs(float(rotate)) > 1e-3 or abs(float(zoom) - 1.0) > 1e-3:
        R = cv2.getRotationMatrix2D((w / 2, h / 2), float(rotate),
                                    max(0.2, float(zoom)))
        out = cv2.warpAffine(out, R, (w, h), flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
    return out
