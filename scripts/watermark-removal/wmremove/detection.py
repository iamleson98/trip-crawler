"""Watermark detection and pixel-mask construction.

The VeXeRe watermark is a fixed artwork stamped in the bottom-left corner of
every photo::

    +--------+---------------------------+
    | yellow  |  white "vexere" text      |
    | logo    +---------------------------+
    | (bus)   |  small yellow smile curve |
    +--------+---------------------------+

Detection strategy
------------------
1. **Template matching** — the yellow logo square is matched at multiple
   scales (4%–16% of the image's long side) over the bottom-left region,
   in both raw BGR and an R−B "yellow-hue" channel that stays discriminative
   when the watermark is semi-transparent over dark backgrounds.
2. **Structure validation** — the best match must look like the real
   watermark: a solid yellow connected block covering most of the logo box,
   plus text/smile evidence (bright or yellowish pixels) in the band to the
   right.  This rejects false positives such as yellow buses or orange seats.

Mask construction
-----------------
The logo rectangle is always masked; text and smile pixels are found by
colour/contrast analysis inside the watermark band (white-ish text, strong
yellow smile, local contrast, dark anti-aliasing).  The mask is dilated to
cover anti-aliased edges and clamped to the watermark bounding box.

Because the watermark geometry is fixed *per image size*, a
:class:`WatermarkDetector` caches the detected logo box per ``(width,
height)``.  Subsequent same-size images skip the expensive multi-scale
search (~400 ms) after a cheap yellow-block presence check (~1 ms).
"""

from __future__ import annotations

import pathlib
from typing import Optional

import cv2
import numpy as np
from scipy import ndimage as ndi

DEFAULT_TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "template.png"

# ---------------------------------------------------------------------------
# Tunables (all measured against the full 3.7k-image corpus)
# ---------------------------------------------------------------------------
SCALE_MIN = 0.040          # logo width as a fraction of max(image side)
SCALE_MAX = 0.161
SCALE_STEP = 0.0015
MATCH_THRESHOLD = 0.42     # minimum normalised template-match score
ROI_X_FRACTION = 0.14      # logo x0 is always within the leftmost 14% of width
ROI_Y_FRACTION = 0.60      # logo y0 is always below 60% of height
TEXT_EXTENT = 5.6          # full watermark width ≈ 5.6 × logo width
TEXT_RISE = 0.18           # text band rises ~0.18 × logo height above logo y0


def _rb_channel(bgr: np.ndarray) -> np.ndarray:
    """R−B difference image: emphasises yellow hue, invariant to brightness."""
    r = bgr[..., 2].astype(np.int16)
    b = bgr[..., 0].astype(np.int16)
    return (r - b).astype(np.float32)


def _yellow_mask(bgr: np.ndarray) -> np.ndarray:
    """Boolean mask of strong-yellow pixels (the logo square)."""
    r = bgr[..., 2].astype(np.int16)
    g = bgr[..., 1].astype(np.int16)
    b = bgr[..., 0].astype(np.int16)
    return (r - b > 70) & (r > 130) & (g > 60) & (r >= g)


class Detection(dict):
    """Detection result: ``score``, ``logo`` (x, y, w, h), ``full_bbox``."""


class WatermarkDetector:
    """Detects the VeXeRe watermark and builds its pixel mask.

    Parameters
    ----------
    template_path:
        Path to the logo template PNG (yellow square + white bus icon).
        Defaults to ``template.png`` shipped next to this package.
    """

    def __init__(self, template_path: str | pathlib.Path = DEFAULT_TEMPLATE):
        template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if template is None:
            raise FileNotFoundError(f"logo template not found: {template_path}")
        self.template = template
        self.template_h, self.template_w = template.shape[:2]
        self.template_rb = _rb_channel(template)
        self._size_cache: dict[tuple[int, int], tuple[int, int, int, int]] = {}

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    def detect(self, image_bgr: np.ndarray) -> Optional[Detection]:
        """Locate the watermark.  Tries the per-size cache first (fast path),
        then falls back to full multi-scale template matching.

        Returns a :class:`Detection` or ``None``.
        """
        key = (image_bgr.shape[1], image_bgr.shape[0])
        cached = self._size_cache.get(key)
        if cached is not None:
            det = self._det_from_box(image_bgr.shape, cached, score=1.0)
            if self._logo_present(image_bgr, cached) and self._validate(image_bgr, det):
                return det

        hit = self._template_match(image_bgr)
        if hit is None:
            return None
        score, box = hit
        det = self._det_from_box(image_bgr.shape, box, score)
        if score >= MATCH_THRESHOLD and self._validate(image_bgr, det):
            self._size_cache[key] = box
            return det
        return None

    # ------------------------------------------------------------------
    # Mask
    # ------------------------------------------------------------------
    def build_mask(self, image_bgr: np.ndarray, det: Detection,
                   margin: int = 8) -> np.ndarray:
        """SOLID RECTANGLE mask over the full watermark extent.

        Why a solid rectangle instead of pixel-precise watermark pixels?
        The watermark is semi-transparent: faint remnants (halo, JPEG
        ghosting, anti-aliased text edges) survive beyond any pixel-level
        colour threshold.  LaMa treats visible remnants as *context* and
        faithfully "completes" the watermark inside the hole — the classic
        failure mode of the earlier pixel-mask approach.  Masking the whole
        bounding box removes every remnant from view, and a rectangular hole
        matches the model's training distribution (random boxes).

        ``margin`` pads the bounding box to absorb detection quantisation
        (the detected logo box can sit a few px inside the true artwork).
        """
        h, w = image_bgr.shape[:2]
        x0, y0, x1, y1 = det["full_bbox"]
        mask = np.zeros((h, w), np.uint8)
        mask[max(0, y0 - margin):min(h, y1 + margin),
             max(0, x0 - margin):min(w, x1 + margin)] = 255
        return mask

    def build_pixel_mask(self, image_bgr: np.ndarray, det: Detection,
                         dilate: int = 2) -> np.ndarray:
        """Legacy pixel-precise mask (kept for reference/analysis).

        Marks the logo rectangle plus text/smile pixels found by colour and
        contrast analysis inside the watermark band, dilated by ``dilate`` px
        and clamped to the watermark bounding box.  NOTE: leaves faint
        semi-transparent remnants unmasked — prefer :meth:`build_mask`.
        """
        h, w = image_bgr.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        x, y, tw, th = det["logo"]

        # 1. the logo rectangle itself
        mask[y:y + th, x:x + tw] = 255

        # 2. text + smile band to the right, found by colour/contrast
        tx0 = x + int(0.95 * tw)
        ty0 = max(0, y - int(0.16 * th))
        tx1 = min(w, x + int(5.55 * tw))
        ty1 = min(h, y + int(1.0 * th))
        if tx1 > tx0 and ty1 > ty0:
            band = image_bgr[ty0:ty1, tx0:tx1].astype(np.int16)
            r, g, b = band[..., 2], band[..., 1], band[..., 0]
            gray = (r + g + b) / 3.0
            whiteish = (r > 170) & (g > 170) & (b > 170) & (np.abs(r - b) < 45)
            smile = (r - b > 50) & (r > 120) & (g > 60) & (r >= g) & (b < g)
            k = max(9, (th // 4) * 2 + 1)
            bgmed = cv2.medianBlur(gray.astype(np.uint8), k).astype(np.int16)
            contrast = gray > (bgmed + 22)
            darker = gray < (bgmed - 25)
            m = (whiteish | smile | contrast | darker).astype(np.uint8) * 255
            mask[ty0:ty1, tx0:tx1] = np.maximum(mask[ty0:ty1, tx0:tx1], m)

        # 3. dilate for anti-aliasing, clamp to the watermark bbox
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
        if dilate > 0:
            mask = cv2.dilate(mask, np.ones((2 * dilate + 1,) * 2, np.uint8), 1)
        x0, y0, x1, y1 = det["full_bbox"]
        pad = 3
        keep = np.zeros((h, w), np.uint8)
        keep[max(0, y0 - pad):min(h, y1 + pad),
             max(0, x0 - pad):min(w, x1 + pad)] = 255
        return cv2.bitwise_and(mask, keep)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _det_from_box(self, shape: tuple[int, ...], box: tuple[int, int, int, int],
                      score: float) -> Detection:
        h, w = shape[:2]
        x, y, tw, th = box
        return Detection(
            score=score,
            logo=box,
            full_bbox=(x, max(0, y - int(TEXT_RISE * th)),
                       min(w, x + int(TEXT_EXTENT * tw)),
                       min(h, y + int(1.05 * th))),
        )

    def _template_match(self, image_bgr: np.ndarray):
        """Multi-scale template search over the bottom-left region.

        Returns ``(score, (x, y, w, h))`` of the best match or ``None``
        when the search region is degenerate.
        """
        h, w = image_bgr.shape[:2]
        m = max(w, h)
        x_hi = min(w, int(w * ROI_X_FRACTION) + int(m * (SCALE_MAX + 0.005)) + 4)
        y_lo = int(h * ROI_Y_FRACTION)
        if x_hi < 20 or h - y_lo < 20:
            return None
        roi = image_bgr[y_lo:, :x_hi]
        roi_rb = _rb_channel(roi)
        best = (0.0, None)
        for wf in np.arange(SCALE_MIN, SCALE_MAX, SCALE_STEP):
            tw = int(round(m * wf))
            th = int(round(tw * self.template_h / self.template_w))
            if tw < 10 or th < 10 or tw >= roi.shape[1] or th >= roi.shape[0]:
                continue
            interp = cv2.INTER_AREA
            tmpl = cv2.resize(self.template, (tw, th), interpolation=interp)
            tmpl_rb = cv2.resize(self.template_rb, (tw, th), interpolation=interp)
            for src, tp in ((roi, tmpl), (roi_rb, tmpl_rb)):
                res = cv2.matchTemplate(src, tp, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(res)
                if mx > best[0]:
                    best = (float(mx), (loc[0], loc[1] + y_lo, tw, th))
        return best if best[1] is not None else None

    def _logo_present(self, image_bgr: np.ndarray, box: tuple[int, int, int, int]) -> bool:
        """Cheap check that a solid yellow block sits at ``box`` (~1 ms)."""
        h, w = image_bgr.shape[:2]
        x, y, tw, th = box
        lx1, ly1 = min(w, x + tw), min(h, y + th)
        if lx1 - x < 5 or ly1 - y < 5:
            return False
        return 0.25 <= float(_yellow_mask(image_bgr[y:ly1, x:lx1]).mean()) <= 1.01

    def _validate(self, image_bgr: np.ndarray, det: Detection) -> bool:
        """Structure validation: solid yellow square + text/smile evidence."""
        h, w = image_bgr.shape[:2]
        x, y, tw, th = det["logo"]
        lx0, ly0, lx1, ly1 = x, y, min(w, x + tw), min(h, y + th)
        if lx1 - lx0 < 5 or ly1 - ly0 < 5:
            return False
        logo = image_bgr[ly0:ly1, lx0:lx1]
        ymask = _yellow_mask(logo)
        yfrac = float(ymask.mean())
        # Real watermarks over yellow backgrounds read fully yellow — those
        # pass only with a very high template score.
        ycap = 1.01 if det["score"] > 0.93 else 0.90
        if not (0.25 <= yfrac <= ycap):
            return False

        # largest yellow component must be a solid block covering most of the box
        lbl, n = ndi.label(ymask)
        if n == 0:
            return False
        sizes = ndi.sum(ymask, lbl, range(1, n + 1))
        big = int(np.argmax(sizes)) + 1
        if sizes[big - 1] < 0.30 * ymask.size:
            return False
        ys, xs = np.nonzero(lbl == big)
        if (xs.max() - xs.min() + 1) < 0.75 * (lx1 - lx0):
            return False
        if (ys.max() - ys.min() + 1) < 0.75 * (ly1 - ly0):
            return False

        # text band to the right: bright text, yellowish smile, or a very
        # confident logo match (watermark over bright sky hides the text)
        tx0, tx1 = min(w, x + int(1.0 * tw)), min(w, x + int(5.5 * tw))
        ty0, ty1 = max(0, y - int(0.15 * th)), min(h, y + int(0.85 * th))
        if tx1 - tx0 >= 8:
            band = image_bgr[ty0:ty1, tx0:tx1]
            gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY).astype(np.int16)
            med = float(np.median(gray))
            bright = float((gray > max(med + 18, 200)).mean())
            yellowish = float((_yellow_mask(band)).mean())
            if bright > 0.02 or yellowish > 0.02:
                return True
        return det["score"] > 0.70
