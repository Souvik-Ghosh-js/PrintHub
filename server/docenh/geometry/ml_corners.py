"""Phase 1/2 (learned) - Document corners from a trained DocUNet.

Drop-in replacement for docenh.geometry.corners.find_document_corners with the
SAME signature, so pipeline/scan.py can switch detectors by swapping the import.

Strategy: run the network to get a document-probability mask, then extract the
4 corners from the mask's largest contour (contour -> quad). This is more
accurate than the network's regressed corner head, which converges slowly, and
it is interpretable. If the mask is empty/uncertain we fall back to the
network's regressed corners, then to the full frame.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .corners import order_corners, _quad_from_contour  # reuse classical helpers

_SESSION = None      # lazy onnxruntime session
_TORCH = None        # lazy torch fallback (model, size)


def _load_onnx(onnx_path: str):
    global _SESSION
    if _SESSION is None:
        import onnxruntime as ort
        _SESSION = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    return _SESSION


def _infer_mask(image: np.ndarray, onnx_path: str, size: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (prob_mask HxW float[0,1] at `size`, regressed_corners (4,2) norm)."""
    sess = _load_onnx(onnx_path)
    inp = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2RGB), (size, size)).astype(np.float32) / 255.0
    inp = inp.transpose(2, 0, 1)[None]
    mask_logits, corners = sess.run(None, {"image": inp})
    prob = 1.0 / (1.0 + np.exp(-mask_logits[0, 0]))
    return prob, corners.reshape(4, 2)


def find_document_corners_ml(
    image: np.ndarray,
    onnx_path: str = "weights/docunet.onnx",
    size: int = 192,
    prob_thresh: float = 0.5,
    min_area_frac: float = 0.05,
) -> tuple[np.ndarray, float]:
    """Learned corner detection. Returns (corners (4,2) in ORIGINAL coords, confidence)."""
    if not Path(onnx_path).exists():
        raise FileNotFoundError(f"{onnx_path} not found - train + export the model first")

    h0, w0 = image.shape[:2]
    prob, reg_corners = _infer_mask(image, onnx_path, size)

    # Binarize mask and find the document quad.
    m = (prob >= prob_thresh).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    quad = None
    conf = 0.05
    if contours:
        c = max(contours, key=cv2.contourArea)
        area_frac = cv2.contourArea(c) / float(size * size)
        if area_frac >= min_area_frac:
            quad = _quad_from_contour(c)
            # Confidence: how "document-like" the masked region is (mean prob
            # inside the quad) blended with coverage.
            filled = np.zeros_like(m)
            cv2.fillConvexPoly(filled, quad.astype(np.int32), 255)
            inside = prob[filled > 0]
            mean_prob = float(inside.mean()) if inside.size else 0.0
            conf = float(min(1.0, 0.5 * mean_prob + 0.5 * min(area_frac / 0.85, 1.0)))

    if quad is None:
        # Fallback to the regressed corner head (already normalized [0,1]).
        quad = reg_corners * [size, size]
        conf = 0.15

    # Scale mask/quad coords (in `size` space) back to original image.
    quad = quad.astype(np.float32)
    quad[:, 0] *= w0 / size
    quad[:, 1] *= h0 / size
    return order_corners(quad), conf
