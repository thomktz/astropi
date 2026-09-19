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


def autostretch(data: np.ndarray, *, black_point: float = 0.25, midtone: float = 0.25) -> np.ndarray:
    """Screen stretch for display, leaving the stored data untouched.

    Raw astronomical frames look black: the interesting signal occupies a
    tiny fraction of the range just above the background. This is the usual
    midtone transfer function - clip near the sky level, then pull the
    midtones up hard - applied for viewing only.
    """
    sample = data[::4, ::4].astype(np.float32)
    median = float(np.median(sample))
    deviation = float(np.median(np.abs(sample - median))) * 1.4826

    low = max(0.0, median - black_point * deviation * 4.0)
    high = float(np.percentile(sample, 99.8))
    if high <= low:
        high = low + 1.0

    normalized = np.clip((data.astype(np.float32) - low) / (high - low), 0.0, 1.0)
    # Midtone transfer function, as used by PixInsight and Siril.
    numerator = (midtone - 1.0) * normalized
    denominator = (2.0 * midtone - 1.0) * normalized - midtone
    with np.errstate(divide="ignore", invalid="ignore"):
        stretched = np.where(denominator == 0, normalized, numerator / denominator)
    return (np.clip(stretched, 0.0, 1.0) * 255).astype(np.uint8)


def to_png(data: np.ndarray, *, max_dimension: int = 1400, stretch: bool = True) -> bytes:
    """Encode a frame as a PNG for the browser.

    Downsampled first: sending 26 megapixels to a phone over LAN Wi-Fi to
    display in a few hundred pixels of viewport wastes both bandwidth and
    the time it takes to decode.
    """
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "preview rendering needs Pillow; install it with `uv pip install pillow`"
        ) from error

    height, width = data.shape[:2]
    step = max(1, int(max(height, width) / max_dimension))
    reduced = data[::step, ::step]

    pixels = autostretch(reduced) if stretch else (reduced >> 8).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()
