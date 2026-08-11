"""Phase 3 - Perspective Reconstruction (classical CV).

Given four ordered document corners, compute the homography and warp the
document to a front-parallel, rectangular image. The output size is derived
from the physical edge lengths so aspect ratio is preserved as well as the
perspective allows.
"""

from __future__ import annotations

import cv2
import numpy as np


def _edge_len(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))


def target_size(corners: np.ndarray) -> tuple[int, int]:
    """Estimate output (width, height) from corner geometry.

    corners ordered TL, TR, BR, BL.
    """
    tl, tr, br, bl = corners
    width = max(_edge_len(br, bl), _edge_len(tr, tl))
    height = max(_edge_len(tr, br), _edge_len(tl, bl))
    return max(1, int(round(width))), max(1, int(round(height)))


def warp_document(
    image: np.ndarray,
    corners: np.ndarray,
    out_size: tuple[int, int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Perspective-correct the document.

    Args:
        image: BGR uint8 source image.
        corners: (4,2) float32 ordered TL,TR,BR,BL in image coordinates.
        out_size: optional (width, height); estimated from geometry if None.

    Returns:
        (warped_bgr, H) where H is the 3x3 homography used.
    """
    corners = np.asarray(corners, dtype=np.float32).reshape(4, 2)
    if out_size is None:
        w, h = target_size(corners)
    else:
        w, h = out_size

    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    H = cv2.getPerspectiveTransform(corners, dst)
    warped = cv2.warpPerspective(image, H, (w, h), flags=cv2.INTER_CUBIC)
    return warped, H
