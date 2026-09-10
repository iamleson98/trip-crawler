"""Single-image removal pipeline: detect → mask → crop → LaMa → feathered paste.

The quality secret sauce, in order:

1. **Crop-based inference** — only the watermark bounding box plus context
   padding is sent to LaMa (~0.3 megapixels instead of the full photo).
   This is what makes CPU inference fast *and* gives the model a small,
   easy canvas on which it reconstructs texture convincingly.
2. **Feathered paste-back** — LaMa's reconstruction is blended back into the
   original with a ~1 px soft alpha *only on watermark pixels*.  Every pixel
   outside the mask is bit-identical to the source: no global re-rendering,
   no colour shift, no detail loss.
3. **Lossless passthrough** — images without a detectable watermark are
   copied byte-for-byte (no JPEG round-trip).
"""

from __future__ import annotations

import pathlib

import cv2
import numpy as np
from PIL import Image

from .detection import DEFAULT_TEMPLATE, WatermarkDetector
from .inpainting import LamaInpainter

DEFAULT_PAD = 48          # context padding around the watermark bbox (px)
CONTEXT_SCALE = 2.5       # padding = context_scale × logo height — LaMa
                           # reconstructs overlapping objects (wheels, seats)
                           # much better with generous context
JPEG_QUALITY = 95


def _feather_alpha(mask: np.ndarray) -> np.ndarray:
    """Mask → float alpha with a ~1 px soft border (seamless pasting)."""
    alpha = cv2.GaussianBlur(mask.astype(np.float32), (3, 3), 1.0) / 255.0
    return np.clip(alpha, 0.0, 1.0)[..., None]


class WatermarkRemover:
    """Removes the VeXeRe watermark from one image at a time.

    Parameters
    ----------
    template_path:
        Path to the logo template used by the detector.
    threads:
        Torch threads per process (``1`` = one worker per core, recommended).
    device:
        ``"cpu"`` or a torch device string.
    """

    def __init__(self, template_path=None, threads: int | None = 1,
                 device: str = "cpu", mask_margin: int = 8,
                 context_scale: float = CONTEXT_SCALE):
        self.mask_margin = mask_margin
        self.context_scale = context_scale
        self.detector = WatermarkDetector(template_path
                                          or DEFAULT_TEMPLATE)
        self.inpainter = LamaInpainter(device=device, threads=threads)

    # ------------------------------------------------------------------
    def remove(self, image_bgr: np.ndarray) -> tuple[np.ndarray, dict]:
        """Remove the watermark in-place.

        Returns ``(image, info)`` where ``info`` describes what happened:
        ``found``, ``score``, ``logo``, ``full_bbox``, ``crop``, ``mask_px``.
        """
        info: dict = {"found": False}
        det = self.detector.detect(image_bgr)
        if det is None:
            return image_bgr, info

        info.update(found=True, score=round(float(det["score"]), 3),
                    logo=det["logo"], full_bbox=det["full_bbox"])
        # solid rectangle over the FULL watermark extent (+ margin): removes
        # every semi-transparent remnant from the model's view — see
        # detection.build_mask for why this beats pixel-precise masks.
        mask = self.detector.build_mask(image_bgr, det, margin=self.mask_margin)

        h, w = image_bgr.shape[:2]
        x0, y0, x1, y1 = det["full_bbox"]
        logo_h = det["logo"][3]
        pad = max(DEFAULT_PAD, int(self.context_scale * logo_h))
        cx0, cy0 = max(0, x0 - pad), max(0, y0 - pad)
        cx1, cy1 = min(w, x1 + pad), min(h, y1 + pad)

        crop = image_bgr[cy0:cy1, cx0:cx1]
        cmask = mask[cy0:cy1, cx0:cx1]
        ch, cw = crop.shape[:2]
        if cw < 8 or ch < 8 or not (cmask > 0).any():
            return image_bgr, info

        crop_rgb = np.ascontiguousarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        filled = self.inpainter.inpaint(crop_rgb, cmask)
        filled_bgr = cv2.cvtColor(filled, cv2.COLOR_RGB2BGR)

        # paste ONLY the (feathered) watermark pixels back
        alpha = _feather_alpha(cmask)
        blended = (alpha * filled_bgr + (1.0 - alpha) * crop).astype(np.uint8)
        paste_zone = cv2.dilate(cmask, np.ones((3, 3), np.uint8)) > 0
        region = image_bgr[cy0:cy1, cx0:cx1]
        region[paste_zone] = blended[paste_zone]

        info.update(crop=(cx0, cy0, cx1, cy1), mask_px=int((cmask > 0).sum()))
        return image_bgr, info

    # ------------------------------------------------------------------
    def process_file(self, src: str | pathlib.Path, dst: str | pathlib.Path,
                     jpeg_quality: int = JPEG_QUALITY) -> dict:
        """Process one file.  Untouched images are copied byte-for-byte;
        cleaned images are re-encoded (JPEG q95 4:4:4 / PNG lossless)."""
        src, dst = str(src), str(dst)
        image = cv2.imread(src, cv2.IMREAD_COLOR)
        if image is None:
            return {"found": False, "error": "unreadable"}
        out, info = self.remove(image)
        dst_ext = pathlib.Path(dst).suffix.lower()
        if not info.get("found"):
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                fo.write(fi.read())
            return info
        if dst_ext in (".png",):
            Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB)).save(dst, "PNG")
        else:
            Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB)).save(
                dst, "JPEG", quality=jpeg_quality, subsampling=0, optimize=True)
        return info
