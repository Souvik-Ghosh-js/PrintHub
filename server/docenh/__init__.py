"""docenh — Universal AI Document Enhancement package.

Phase-aligned modules:
    understanding  -> Phase 1: document detection / segmentation
    geometry       -> Phase 2: contour, corners, geometry AI
    perspective    -> Phase 3: homography + perspective correction
    enhancement    -> Phase 4/5: shadow removal, illumination, restoration
    quality        -> Phase 6: image quality assessment

Every module exposes a plain function/class so a classical-CV implementation
can later be swapped for a trained deep-learning model without touching the
pipeline that orchestrates them.
"""

__version__ = "0.1.0"
