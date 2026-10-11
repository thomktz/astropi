"""Browsing the frames on the SSD, with JPEG previews made once and kept.

The previews live beside the frames in a hidden `.previews` folder that
mirrors the archive, so they survive restarts and a frame is only ever
decoded once per size. A preview older than its frame is made again.

Rendering runs on one low-priority thread. A 52 MB frame takes a second or
two of CPU on the Pi, and opening a folder of thumbnails must never delay
the capture that is writing the next one: one worker keeps memory to a
single frame at a time, and `nice` lets the camera and guider go first.

The full-quality render is the frame at its own resolution, debayered to
colour, and only made when asked for: it is a few seconds of CPU and a
JPEG of several megabytes.

Frames and folders can be deleted from here. For real - there is no
trash - and never what a running sequence is writing into.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from astropi.core.errors import AstropiError
from astropi.services.preview import _bin_down
from astropi.storage.frames import autostretch_lut

logger = logging.getLogger(__name__)

PREVIEW_DIR = ".previews"
#: Longest side in pixels: a grid tile, and the size a frame is opened at.
SIZES = {"thumb": 360, "large": 1600, "full": 0}
FRAME_SUFFIXES = (".fits", ".fit")


def _lower_priority() -> None:
    # On Linux `setpriority` on a thread id renices just that thread.
    with_id = getattr(threading, "get_native_id", None)
    if with_id is not None and hasattr(os, "setpriority"):
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, with_id(), 10)


class FrameBrowser:
    def __init__(self, root: Path, *, writing: Callable[[], Path | None] | None = None) -> None:
        self.root = root
        #: The folder a running sequence is writing frames into, if any.
        self._writing = writing or (lambda: None)
        self._renderer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="frame-preview", initializer=_lower_priority
        )

    def _inside(self, relative: str) -> Path:
        root = self.root.resolve()
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            raise AstropiError(f"{relative} is outside the frame archive")
        if PREVIEW_DIR in path.relative_to(root).parts:
            raise AstropiError(f"{relative} is not a frame folder")
        return path

    def listing(self, relative: str = "") -> dict[str, object]:
        """One folder: its subfolders and its frames, newest first."""
        folder = self._inside(relative)
        if not relative.strip("/") and not folder.exists():
            return {"path": "", "dirs": [], "frames": []}
        if not folder.is_dir():
            raise AstropiError(f"{relative or 'the archive'} is not a folder")
        dirs, frames = [], []
        for entry in os.scandir(folder):
            if entry.name.startswith("."):
                continue
            stat = entry.stat()
            rel = str(Path(entry.path).relative_to(self.root.resolve()))
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": rel, "modified": stat.st_mtime})
            elif entry.name.lower().endswith(FRAME_SUFFIXES):
                frames.append(
                    {"name": entry.name, "path": rel, "size": stat.st_size, "modified": stat.st_mtime}
                )
        dirs.sort(key=lambda d: d["name"], reverse=True)
        frames.sort(key=lambda f: (f["modified"], f["name"]), reverse=True)
        path = "" if folder == self.root.resolve() else relative.strip("/")
        return {"path": path, "dirs": dirs, "frames": frames}

    async def preview(self, relative: str, size: str) -> Path:
        """A JPEG of the frame at `size`, rendered now if there is none newer."""
        if size not in SIZES:
            raise AstropiError(f"size must be one of {', '.join(SIZES)}")
        frame = self._inside(relative)
        if not frame.is_file() or not frame.name.lower().endswith(FRAME_SUFFIXES):
            raise AstropiError(f"{relative} is not a frame in the archive")
        rel = frame.relative_to(self.root.resolve())
        jpeg = self.root.resolve() / PREVIEW_DIR / rel.parent / f"{rel.stem}.{size}.jpg"
        if jpeg.exists() and jpeg.stat().st_mtime >= frame.stat().st_mtime:
            return jpeg
        loop = asyncio.get_running_loop()
        render = _render_full if size == "full" else _render
        await loop.run_in_executor(self._renderer, render, frame, jpeg, SIZES[size])
        return jpeg

    def summary(self, relative: str) -> dict[str, object]:
        """What deleting `relative` would remove, for the confirmation."""
        path = self._deletable(relative)
        if path.is_file():
            return {"path": relative, "kind": "frame", "files": 1, "frames": 1, "bytes": path.stat().st_size}
        files = frames = size = 0
        for directory, _, names in os.walk(path):
            for name in names:
                files += 1
                frames += name.lower().endswith(FRAME_SUFFIXES)
                with contextlib.suppress(OSError):
                    size += os.stat(os.path.join(directory, name)).st_size
        return {"path": relative, "kind": "folder", "files": files, "frames": frames, "bytes": size}

    def delete(self, relative: str) -> dict[str, object]:
        """Delete a frame or a folder, and its cached previews. No undo."""
        summary = self.summary(relative)
        path = self._deletable(relative)
        rel = path.relative_to(self.root.resolve())
        previews = self.root.resolve() / PREVIEW_DIR / rel
        if path.is_file():
            path.unlink()
            for jpeg in previews.parent.glob(f"{rel.stem}.*.jpg"):
                jpeg.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
            shutil.rmtree(previews, ignore_errors=True)
        logger.info("deleted %s (%s files)", rel, summary["files"])
        return summary

    def _deletable(self, relative: str) -> Path:
        path = self._inside(relative)
        root = self.root.resolve()
        if path == root:
            raise AstropiError("the whole archive cannot be deleted from here")
        if not path.exists():
            raise AstropiError(f"{relative} is not in the archive")
        if path.is_file() and not path.name.lower().endswith(FRAME_SUFFIXES):
            raise AstropiError(f"{relative} is not a frame")
        writing = self._writing()
        if writing is not None:
            writing = writing.resolve()
            if path == writing or path in writing.parents or writing in path.parents:
                raise AstropiError(
                    f"{relative} is where the running sequence is saving frames; stop it first"
                )
        return path

    def close(self) -> None:
        self._renderer.shutdown(wait=False, cancel_futures=True)


def _save_jpeg(image, jpeg: Path, quality: int) -> None:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    jpeg.parent.mkdir(parents=True, exist_ok=True)
    partial = jpeg.with_suffix(".jpg.part")
    partial.write_bytes(buffer.getvalue())
    partial.replace(jpeg)


#: Where each colour sits in a 2x2 Bayer cell, as (row, column).
_BAYER_OFFSETS = {
    "RGGB": {"R": (0, 0), "B": (1, 1)},
    "BGGR": {"R": (1, 1), "B": (0, 0)},
    "GRBG": {"R": (0, 1), "B": (1, 0)},
    "GBRG": {"R": (1, 0), "B": (0, 1)},
}
#: Rows debayered at a time. A whole 26-megapixel frame in float32 is a
#: few hundred megabytes per channel; strips keep the Pi's memory flat.
_STRIP_ROWS = 256


def _demosaic(data: np.ndarray, pattern: str) -> np.ndarray:
    """Bilinear debayer to 16-bit RGB at the sensor's own resolution.

    Strip by strip, each padded by a row from its neighbours, so only one
    strip is ever held in floating point.
    """
    offsets = _BAYER_OFFSETS[pattern]
    height, width = data.shape
    corner = np.array([[0.25, 0.5, 0.25], [0.5, 1.0, 0.5], [0.25, 0.5, 0.25]], dtype=np.float32)
    cross = np.array([[0.0, 0.25, 0.0], [0.25, 1.0, 0.25], [0.0, 0.25, 0.0]], dtype=np.float32)
    kernels = {"R": corner, "G": cross, "B": corner}
    out = np.empty((height, width, 3), dtype=np.uint16)
    for top in range(0, height, _STRIP_ROWS):
        bottom = min(height, top + _STRIP_ROWS)
        # Rows top-1 .. bottom, mirrored at the frame's edges as np.pad's
        # "reflect" does, which keeps every pixel's Bayer colour.
        row_index = np.abs(np.arange(top - 1, bottom + 1))
        row_index = np.where(row_index >= height, 2 * (height - 1) - row_index, row_index)
        col_index = np.abs(np.arange(-1, width + 1))
        col_index = np.where(col_index >= width, 2 * (width - 1) - col_index, col_index)
        strip = data[row_index][:, col_index].astype(np.float32)
        rows = row_index[:, None] % 2
        cols = col_index[None, :] % 2
        masks = {
            "R": (rows == offsets["R"][0]) & (cols == offsets["R"][1]),
            "B": (rows == offsets["B"][0]) & (cols == offsets["B"][1]),
        }
        masks["G"] = ~(masks["R"] | masks["B"])
        for channel, colour in enumerate("RGB"):
            plane = strip * masks[colour]
            kernel = kernels[colour]
            total = np.zeros((bottom - top, width), dtype=np.float32)
            for dy in range(3):
                for dx in range(3):
                    if kernel[dy, dx]:
                        total += kernel[dy, dx] * plane[dy : dy + bottom - top, dx : dx + width]
            out[top:bottom, :, channel] = np.clip(total, 0, 65535).astype(np.uint16)
    return out


def _render_full(frame: Path, jpeg: Path, _max_dimension: int) -> None:
    """The frame at full resolution: debayered to colour if it has a Bayer matrix."""
    from astropy.io import fits
    from PIL import Image

    with fits.open(frame) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.uint16)
        pattern = str(hdul[0].header.get("BAYERPAT", "")).strip().upper()
    if pattern not in _BAYER_OFFSETS:
        _save_jpeg(Image.fromarray(autostretch_lut(data)[data], mode="L"), jpeg, 90)
        return
    rgb = _demosaic(data, pattern)
    # A rough white balance: each channel's background brought to green's,
    # so the stretch below does not come out green.
    sample = rgb[::8, ::8].reshape(-1, 3).astype(np.float32)
    medians = np.maximum(np.median(sample, axis=0), 1.0)
    for channel in (0, 2):
        scaled = rgb[:, :, channel].astype(np.float32) * (medians[1] / medians[channel])
        rgb[:, :, channel] = np.clip(scaled, 0, 65535).astype(np.uint16)
    lut = autostretch_lut(rgb[:, :, 1])
    _save_jpeg(Image.fromarray(lut[rgb], mode="RGB"), jpeg, 90)


def _render(frame: Path, jpeg: Path, max_dimension: int) -> None:
    from astropy.io import fits
    from PIL import Image

    with fits.open(frame) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.uint16)
    step = max(1, -(-max(data.shape) // max_dimension))
    # An even step averages whole Bayer cells, so a colour frame comes out
    # as clean luminance rather than a checkerboard.
    if step > 1 and step % 2:
        step += 1
    reduced = _bin_down(data, step)
    pixels = autostretch_lut(reduced)[reduced]
    _save_jpeg(Image.fromarray(pixels, mode="L"), jpeg, 82)
