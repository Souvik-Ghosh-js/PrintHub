"""Phase 4/5 - Computational Photography + Document Restoration (classical CV).

Turns a perspective-corrected document crop into a clean, evenly-lit,
scanner-quality image. Order matters:

    1. Shadow / uneven-illumination removal via background division.
       Estimate the local background (paper) with a large-kernel morphology /
       blur, then divide the image by it. This flattens gradients from
       side-lighting and soft shadows without touching the text strokes.
    2. White balance (gray-world) so paper reads neutral.
    3. Auto contrast stretch + gentle CLAHE for local contrast.
    4. Sharpen (unsharp mask) for crisp text edges.
    5. Optional adaptive binarization for a pure black-on-white "scan".

Every step is deliberately hand-tuned classical CV so it runs on a phone CPU.
Each is a pure function -> a learned restoration net (NAFNet/Restormer) can
later replace `remove_shadows` or `restore` behind the same signature.
"""

from __future__ import annotations

import cv2
import numpy as np


def remove_shadows(bgr: np.ndarray, kernel_frac: float = 0.09) -> np.ndarray:
    """Flatten illumination by dividing out an estimated background.

    Works per channel. kernel_frac sets the background-estimation kernel size
    relative to the image's larger dimension (bigger = smoother background).
    """
    h, w = bgr.shape[:2]
    k = max(3, int(min(h, w) * kernel_frac) | 1)  # force odd
    result = np.empty_like(bgr)
    for c in range(3):
        ch = bgr[:, :, c]
        # Morphological closing with a large kernel approximates the paper
        # background (removes dark text), then blur to smooth it.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        bg = cv2.morphologyEx(ch, cv2.MORPH_CLOSE, kernel)
        bg = cv2.GaussianBlur(bg, (0, 0), sigmaX=k / 3.0)
        bg = np.where(bg == 0, 1, bg).astype(np.float32)
        norm = (ch.astype(np.float32) / bg) * 255.0
        result[:, :, c] = np.clip(norm, 0, 255).astype(np.uint8)
    return result


def gray_world_white_balance(bgr: np.ndarray) -> np.ndarray:
    """Neutralize color cast so paper reads white/gray."""
    f = bgr.astype(np.float32)
    means = f.reshape(-1, 3).mean(axis=0)
    gray = means.mean()
    scale = gray / np.where(means == 0, 1, means)
    out = f * scale
    return np.clip(out, 0, 255).astype(np.uint8)


def auto_contrast(bgr: np.ndarray, clip_percent: float = 0.5) -> np.ndarray:
    """Percentile contrast stretch + CLAHE on the luminance channel."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)

    lo = np.percentile(l, clip_percent)
    hi = np.percentile(l, 100 - clip_percent)
    if hi <= lo:
        hi = lo + 1
    l = np.clip((l.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)

    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


def unsharp(bgr: np.ndarray, amount: float = 1.0, sigma: float = 1.2) -> np.ndarray:
    """Unsharp-mask sharpening for crisp text edges."""
    blur = cv2.GaussianBlur(bgr, (0, 0), sigmaX=sigma)
    sharp = cv2.addWeighted(bgr, 1 + amount, blur, -amount, 0)
    return np.clip(sharp, 0, 255).astype(np.uint8)


def denoise(bgr: np.ndarray, strength: int = 5) -> np.ndarray:
    return cv2.fastNlMeansDenoisingColored(bgr, None, strength, strength, 7, 21)


def binarize(bgr: np.ndarray, block: int = 31, C: int = 15) -> np.ndarray:
    """Adaptive threshold -> pure black-on-white scan (returns 3-ch for uniform IO)."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    th = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block | 1, C
    )
    return cv2.cvtColor(th, cv2.COLOR_GRAY2BGR)


def enhance(
    bgr: np.ndarray,
    mode: str = "color",
    do_denoise: bool = False,
) -> np.ndarray:
    """Full enhancement chain.

    mode: "color" -> clean color scan (default)
          "gray"  -> grayscale clean scan
          "bw"    -> binarized black-on-white scan
    """
    out = remove_shadows(bgr)
    out = gray_world_white_balance(out)
    if do_denoise:
        out = denoise(out)
    out = auto_contrast(out)
    out = unsharp(out, amount=0.8)

    if mode == "gray":
        g = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        out = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    elif mode == "bw":
        out = binarize(out)
    return out
