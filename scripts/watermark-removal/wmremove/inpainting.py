"""LaMa inpainting wrapper (CPU-friendly).

Uses *big-lama* — "Resolution Robust Large Mask Inpainting with Fourier
Convolutions" (Suvorov et al., 2021) — via the ``simple-lama-inpainting``
package.  The model weights (~196 MB TorchScript) are downloaded once from
GitHub releases and cached in ``~/.cache/torch/hub/checkpoints/``.

Two details that matter for quality:

* the TorchScript model pads inputs to a multiple of 8 and returns the
  *padded* canvas — :meth:`LamaInpainter.inpaint` crops the result back to
  the input size for you;
* CPU throughput is maximised by pinning this process to one thread per
  core and running one inpainter per worker process (see ``cli.py``).
"""

from __future__ import annotations

import numpy as np
import torch


class LamaInpainter:
    """Thin wrapper around big-lama TorchScript inference.

    Parameters
    ----------
    device:
        ``"cpu"`` (default) or any torch device string.
    threads:
        Torch intra-op threads.  Use ``1`` when running one worker process
        per CPU core; leave ``None`` for torch's default.
    """

    def __init__(self, device: str = "cpu", threads: int | None = 1):
        self.device = torch.device(device)
        if threads is not None:
            torch.set_num_threads(threads)
        from simple_lama_inpainting import SimpleLama  # deferred heavy import

        self._model = SimpleLama(device=self.device)
        self._warmed = False

    def _warm_up(self) -> None:
        if self._warmed:
            return
        dummy = np.zeros((64, 64, 3), np.uint8)
        mask = np.zeros((64, 64), np.uint8)
        mask[20:40, 20:40] = 255
        self._model(dummy, mask)
        self._warmed = True

    def inpaint(self, image_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """Reconstruct ``mask`` (>0) pixels of ``image_rgb`` (uint8, H×W×3).

        Returns a uint8 RGB array of the *same* size as the input.
        """
        self._warm_up()
        h, w = image_rgb.shape[:2]
        out = self._model(image_rgb, mask)          # PIL image, mod-8 padded
        out = np.asarray(out, dtype=np.uint8)[:h, :w]  # un-pad
        return out
