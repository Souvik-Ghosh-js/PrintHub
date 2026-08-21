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


def detect_corners(bgr):
    """Detect document corners.

    Order of preference: DocAligner (accurate on photographed ID cards),
    then the older docunet model, then classical OpenCV. Returns
    (corners (4,2), confidence, detector-name).
    """
    results = []

    if os.path.exists(DOCALIGNER_ONNX):
        try:
            corners, conf = find_corners_docaligner(bgr)
            if conf >= DOCALIGNER_MIN_PEAK:
                return order_corners(corners), conf, "docaligner"
            results.append((corners, conf, "docaligner"))
        except Exception as e:
            print(f"[docscan] DocAligner unavailable ({e}); trying other detectors")

    if os.path.exists(DOCUNET_ONNX):
        try:
            from docenh.geometry.ml_corners import find_document_corners_ml
            corners, conf = find_document_corners_ml(bgr, onnx_path=DOCUNET_ONNX)
            if conf >= DETECT_MIN_CONFIDENCE:
                return corners, conf, "ml"
            results.append((corners, conf, "ml"))
        except Exception as e:
            print(f"[docscan] docunet unavailable ({e}); falling back to classical CV")

    from docenh.geometry.corners import find_document_corners
    corners, conf = find_document_corners(bgr)
    results.append((corners, conf, "classical"))

    # Nothing was confident — return whichever scored highest.
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
