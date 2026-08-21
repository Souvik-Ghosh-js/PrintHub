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

# Below this confidence the detection is likely a full-frame fallback quad,
# so the frontend should keep Cropper.js's own default box instead.
DETECT_MIN_CONFIDENCE = 0.30


def detect_corners(bgr):
    """Detect document corners with the ML model, falling back to the
    classical OpenCV detector when the model is unavailable OR unsure.
    Returns (corners (4,2), confidence, detector) with detector "ml" or
    "classical"."""
    ml_result = None
    if os.path.exists(DOCUNET_ONNX):
        try:
            from docenh.geometry.ml_corners import find_document_corners_ml
            corners, conf = find_document_corners_ml(bgr, onnx_path=DOCUNET_ONNX)
            if conf >= DETECT_MIN_CONFIDENCE:
                return corners, conf, "ml"
            ml_result = (corners, conf)
        except Exception as e:
            print(f"[docscan] ML detector unavailable ({e}); "
                  f"falling back to classical CV")
    else:
        print(f"[docscan] model file missing ({DOCUNET_ONNX}); "
              f"falling back to classical CV")
    from docenh.geometry.corners import find_document_corners
    corners, conf = find_document_corners(bgr)
    # Keep whichever detector was more confident.
    if ml_result and ml_result[1] >= conf:
        return ml_result[0], ml_result[1], "ml"
    return corners, conf, "classical"


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
