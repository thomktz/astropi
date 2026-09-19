"""In-memory frame store with preview rendering.

Bounded on purpose. A full ASI2600MC frame is about 52 MB of raw pixels, so
an unbounded cache would exhaust a Raspberry Pi within a few minutes of an
imaging run. Recent frames are kept for preview and inspection; anything
that must survive belongs on disk as FITS, which is the next thing to build
here.
"""

from __future__ import annotations

import io
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from astropi.devices.camera import Frame


@dataclass(slots=True)
class StoredFrame:
    id: str
    frame: Frame
    stored_at: float = field(default_factory=time.time)

    def summary(self) -> dict[str, Any]:
        height, width = self.frame.shape
        return {
            "id": self.id,
            "width": width,
            "height": height,
            "duration_s": self.frame.request.duration_s,
            "kind": str(self.frame.request.kind),
            "stored_at": self.stored_at,
            # Simulator ground truth is stripped: nothing outside the
            # simulated solver may see where a frame was really taken.
            "metadata": {k: v for k, v in self.frame.metadata.items() if not k.startswith("sim_")},
        }


class FrameStore:
    def __init__(self, capacity: int = 12) -> None:
        self._frames: OrderedDict[str, StoredFrame] = OrderedDict()
        self._capacity = max(1, capacity)

    def add(self, frame: Frame) -> str:
        frame_id = uuid.uuid4().hex[:12]
        self._frames[frame_id] = StoredFrame(id=frame_id, frame=frame)
        while len(self._frames) > self._capacity:
            self._frames.popitem(last=False)
        return frame_id

    def get(self, frame_id: str) -> StoredFrame | None:
        return self._frames.get(frame_id)

    def latest(self) -> StoredFrame | None:
        if not self._frames:
            return None
        return next(reversed(self._frames.values()))

    def list(self) -> list[StoredFrame]:
        return list(reversed(self._frames.values()))

    def __len__(self) -> int:
        return len(self._frames)


def _midtone_transfer(values: np.ndarray, midtone: float) -> np.ndarray:
    """The midtone transfer function used by PixInsight and Siril.

    Maps [0, 1] onto [0, 1], bending the curve so that `midtone` lands on
    0.5. It is the standard non-linear display stretch for astronomical
    data - a plain gamma would blow out the star cores long before the
    faint structure became visible.
    """
    if midtone <= 0.0:
        return np.ones_like(values)
    if midtone >= 1.0:
        return np.zeros_like(values)
    numerator = (midtone - 1.0) * values
    denominator = (2.0 * midtone - 1.0) * values - midtone
    return np.where(denominator == 0.0, values, numerator / denominator)


def autostretch(
    data: np.ndarray,
    *,
    target_background: float = 0.12,
    shadow_clip: float = 2.8,
    bit_depth: int = 16,
) -> np.ndarray:
    """Screen stretch for display, leaving the stored data untouched.

    A raw astronomical frame shown linearly is a black rectangle: the sky
    background sits a few hundred ADU above zero and everything
    interesting is a sliver above that.

    The clipping point is set *below* the background by a few times the
    noise, and the whole range above it is stretched so the background
    lands at `target_background`. Scaling between the background and a
    high percentile instead - which is the obvious thing to try - maps the
    noise itself across the full output range and renders the frame as
    television static, because on a real star field the overwhelming
    majority of pixels *are* background.
    """
    sample = data[::4, ::4].astype(np.float32)
    full_scale = float((1 << bit_depth) - 1)

    normalized = sample / full_scale
    median = float(np.median(normalized))
    # Median absolute deviation, scaled to be comparable to a standard
    # deviation, so the clip is a meaningful number of noise widths.
    deviation = float(np.median(np.abs(normalized - median))) * 1.4826
    if deviation <= 0.0:
        deviation = 1.0 / full_scale

    black = max(0.0, median - shadow_clip * deviation)
    span = max(1.0 - black, 1e-6)

    # Where the background sits once the black point is removed; the
    # midtone is then chosen to lift exactly that value to the target.
    background = (median - black) / span
    midtone = _solve_midtone(background, target_background)

    scaled = np.clip((data.astype(np.float32) / full_scale - black) / span, 0.0, 1.0)
    stretched = _midtone_transfer(scaled, midtone)
    return (np.clip(stretched, 0.0, 1.0) * 255.0).astype(np.uint8)


def _solve_midtone(value: float, target: float) -> float:
    """The midtone that maps `value` to `target` under the transfer function."""
    if value <= 0.0:
        return 0.5
    if value >= 1.0:
        return 0.5
    denominator = 2.0 * target * value - target - value
    if abs(denominator) < 1e-12:
        return 0.5
    return float(np.clip(value * (target - 1.0) / denominator, 1e-4, 1.0 - 1e-4))


def _box_downsample(data: np.ndarray, step: int) -> np.ndarray:
    """Average `step` x `step` blocks down to one pixel.

    Averaging rather than taking every nth pixel. Striding keeps each
    surviving pixel's full noise, so a reduced frame is exactly as grainy
    as the original but with the grain now the size of a screen pixel -
    which is what turns a perfectly good sub-exposure into television
    static on screen. Averaging divides the noise by `step`, the same thing
    binning does in hardware.
    """
    if step <= 1:
        return data
    height = (data.shape[0] // step) * step
    width = (data.shape[1] // step) * step
    if height == 0 or width == 0:
        return data
    cropped = data[:height, :width].astype(np.float32)
    return cropped.reshape(height // step, step, width // step, step).mean(axis=(1, 3))


def to_png(
    data: np.ndarray,
    *,
    max_dimension: int = 1400,
    stretch: bool = True,
    bit_depth: int = 16,
) -> bytes:
    """Encode a frame as a PNG for the browser.

    Downsampled first: sending 26 megapixels to a phone over LAN Wi-Fi to
    display in a few hundred pixels of viewport wastes both the bandwidth
    and the time it takes to decode.
    """
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "preview rendering needs Pillow; install it with `uv pip install pillow`"
        ) from error

    height, width = data.shape[:2]
    step = max(1, int(max(height, width) / max_dimension))
    reduced = _box_downsample(data, step)

    if stretch:
        pixels = autostretch(reduced, bit_depth=bit_depth)
    else:
        pixels = np.clip(reduced / (1 << (bit_depth - 8)), 0, 255).astype(np.uint8)

    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()
