"""Phase 6 - Image Quality Assessment (classical, no-reference).

Produces interpretable scores and an accept/retake decision. These map to the
.md's requested outputs (blur, noise, overall) using cheap no-reference
metrics. A learned IQA head can later replace `assess` behind the same dict API.
"""

from __future__ import annotations

import cv2
import numpy as np


def blur_score(bgr: np.ndarray) -> float:
    """Higher = sharper. Variance of Laplacian, normalized to ~[0,1]."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    v = cv2.Laplacian(gray, cv2.CV_64F).var()
    return float(min(1.0, v / 500.0))


def noise_score(bgr: np.ndarray) -> float:
    """Higher = cleaner. Estimate noise from high-freq residual."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    resid = gray - cv2.GaussianBlur(gray, (0, 0), 1.0)
    sigma = resid.std()
    return float(max(0.0, 1.0 - sigma / 25.0))


def exposure_score(bgr: np.ndarray) -> float:
    """Higher = better exposed (penalize clipped/black or blown-out)."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    hist = hist / hist.sum()
    clipped = hist[:5].sum() + hist[-5:].sum()
    return float(max(0.0, 1.0 - clipped))


def assess(bgr: np.ndarray, corner_confidence: float | None = None) -> dict:
    """Return quality scores and an accept/retake verdict."""
    b = blur_score(bgr)
    n = noise_score(bgr)
    e = exposure_score(bgr)

    parts = [b, n, e]
    if corner_confidence is not None:
        parts.append(corner_confidence)
    overall = float(np.mean(parts))

    verdict = "accept" if (overall >= 0.5 and b >= 0.25) else "retake"
    return {
        "blur_score": round(b, 3),
        "noise_score": round(n, 3),
        "exposure_score": round(e, 3),
        "crop_confidence": None if corner_confidence is None else round(corner_confidence, 3),
        "overall_quality": round(overall, 3),
        "verdict": verdict,
        "enhancement_confidence": round(overall, 3),
    }
