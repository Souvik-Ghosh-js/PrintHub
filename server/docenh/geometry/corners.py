"""Phase 2 - Geometry AI (classical-CV baseline).

Detects the document's four corners in a camera photo. This is the
swappable baseline: the public API is `find_document_corners(image) -> corners`
so a trained corner-regression / segmentation model can replace it later
behind the same signature.

Strategy (robust for mobile document photos):
    1. Downscale for speed and noise tolerance.
    2. Two candidate paths, take whichever gives the better quadrilateral:
         a) edge-based: grayscale -> blur -> Canny -> dilate -> contours
         b) saturation/brightness based: documents are usually brighter and
            lower-saturation than backgrounds -> Otsu -> largest contour
    3. Approximate the largest plausible contour to 4 points.
    4. Fall back to the full-frame rectangle if nothing convincing is found.
"""

from __future__ import annotations

import cv2
import numpy as np


def order_corners(pts: np.ndarray) -> np.ndarray:
    """Return corners ordered as [top-left, top-right, bottom-right, bottom-left]."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [
            pts[np.argmin(s)],  # top-left  (smallest x+y)
            pts[np.argmin(d)],  # top-right (smallest y-x)
            pts[np.argmax(s)],  # bottom-right (largest x+y)
            pts[np.argmax(d)],  # bottom-left (largest y-x)
        ],
        dtype=np.float32,
    )


def _quad_from_contour(contour: np.ndarray) -> np.ndarray | None:
    """Approximate a contour to a convex quadrilateral, or None."""
    peri = cv2.arcLength(contour, True)
    for frac in (0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(contour, frac * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return approx.reshape(4, 2).astype(np.float32)
    # Fallback: min-area rectangle around the contour.
    rect = cv2.minAreaRect(contour)
    box = cv2.boxPoints(rect)
    return box.astype(np.float32)


def _rectangularity(quad: np.ndarray) -> float:
    """1.0 for a perfect rectangle; lower for skewed/irregular quads.

    Compares the quad's area to its min-area bounding rectangle and rewards
    corner angles near 90 degrees. Used to reject junk quadrilaterals.
    """
    quad = quad.reshape(4, 2).astype(np.float32)
    area = cv2.contourArea(quad)
    if area <= 0:
        return 0.0
    rect_area = cv2.minAreaRect(quad)[1]
    rect_area = rect_area[0] * rect_area[1]
    fill = area / rect_area if rect_area > 0 else 0.0

    # Angle regularity: mean deviation of the 4 corner angles from 90 deg.
    angs = []
    for i in range(4):
        a = quad[(i - 1) % 4] - quad[i]
        b = quad[(i + 1) % 4] - quad[i]
        cosang = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6)
        angs.append(np.degrees(np.arccos(np.clip(cosang, -1, 1))))
    ang_score = 1.0 - min(1.0, np.mean(np.abs(np.array(angs) - 90)) / 45.0)
    return float(fill * ang_score)


def _touches_border(quad: np.ndarray, w: int, h: int, margin: int = 4) -> bool:
    """True if any corner sits on the frame edge (likely full-frame fallback)."""
    q = quad.reshape(4, 2)
    on = (
        (q[:, 0] <= margin) | (q[:, 0] >= w - 1 - margin)
        | (q[:, 1] <= margin) | (q[:, 1] >= h - 1 - margin)
    )
    return bool(on.sum() >= 3)


def _candidate_quads(mask: np.ndarray, min_area_frac: float) -> list[np.ndarray]:
    """All plausible document quadrilaterals from a binary mask."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    h, w = mask.shape[:2]
    img_area = float(h * w)
    out = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:6]:
        if cv2.contourArea(c) < min_area_frac * img_area:
            break
        quad = _quad_from_contour(c)
        if quad is not None:
            out.append(quad)
    return out


def find_document_corners(
    image: np.ndarray,
    working_width: int = 800,
    min_area_frac: float = 0.15,
) -> tuple[np.ndarray, float]:
    """Find document corners in an image.

    Args:
        image: BGR uint8 image (as read by cv2.imread).
        working_width: internal width the detection runs at (for speed).
        min_area_frac: reject candidate quads smaller than this fraction of frame.

    Returns:
        (corners, confidence) where corners is (4,2) float32 in ORIGINAL image
        coordinates, ordered TL,TR,BR,BL. confidence in [0,1]; a full-frame
        fallback returns low confidence.
    """
    h0, w0 = image.shape[:2]
    scale = working_width / float(w0)
    small = cv2.resize(image, (working_width, int(h0 * scale)), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    frame_area = float(sh * sw)

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)

    masks: list[np.ndarray] = []

    # Path a: edge based (good for high-contrast doc/background boundaries).
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    masks.append(edges)

    # Path b: brightness Otsu (bright paper on darker background).
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    masks.append(cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8)))

    # Path c: low-saturation mask (paper is near-gray; colored/busy bg is not).
    sat = hsv[:, :, 1]
    _, low_sat = cv2.threshold(sat, 60, 255, cv2.THRESH_BINARY_INV)
    masks.append(cv2.morphologyEx(low_sat, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)))

    # Score every candidate quad from every mask.
    best_quad = None
    best_score = -1.0
    for m in masks:
        for quad in _candidate_quads(m, min_area_frac):
            area = cv2.contourArea(quad.astype(np.int32))
            area_frac = area / frame_area
            rect = _rectangularity(quad)
            # Penalize quads that hug the frame edge (usually the fallback, not
            # the document): reward mid-to-large area, strong rectangularity.
            touches_frame = _touches_border(quad, sw, sh)
            area_term = min(area_frac / 0.85, 1.0)
            score = rect * (0.5 + 0.5 * area_term) * (0.6 if touches_frame else 1.0)
            if score > best_score:
                best_score, best_quad = score, quad

    if best_quad is not None and best_score > 0.35:
        corners_small = order_corners(best_quad)
        confidence = float(min(1.0, best_score))
    else:
        # Fallback: whole frame, low confidence.
        corners_small = order_corners(
            np.array([[0, 0], [sw - 1, 0], [sw - 1, sh - 1], [0, sh - 1]], dtype=np.float32)
        )
        confidence = 0.05

    corners = corners_small / scale  # back to original coords
    return corners.astype(np.float32), confidence
