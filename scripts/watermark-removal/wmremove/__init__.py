"""wmremove — AI watermark removal for VeXeRe vehicle photos (CPU-only).

Public API:
    from wmremove import WatermarkRemover
    remover = WatermarkRemover()
    image, info = remover.remove(image_bgr)
"""

from .detection import WatermarkDetector
from .inpainting import LamaInpainter
from .pipeline import WatermarkRemover

__all__ = ["WatermarkDetector", "LamaInpainter", "WatermarkRemover"]
__version__ = "2.0.0"
