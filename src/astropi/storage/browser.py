"""Browsing the frames on the SSD, with JPEG previews made once and kept.

The previews live beside the frames in a hidden `.previews` folder that
mirrors the archive, so they survive restarts and a frame is only ever
decoded once per size. A preview older than its frame is made again.

Rendering runs on one low-priority thread. A 52 MB frame takes a second or
two of CPU on the Pi, and opening a folder of thumbnails must never delay
the capture that is writing the next one: one worker keeps memory to a
single frame at a time, and `nice` lets the camera and guider go first.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from astropi.core.errors import AstropiError
from astropi.services.preview import _bin_down
from astropi.storage.frames import autostretch_lut

logger = logging.getLogger(__name__)

PREVIEW_DIR = ".previews"
#: Longest side in pixels: a grid tile, and the size a frame is opened at.
SIZES = {"thumb": 360, "large": 1600}
FRAME_SUFFIXES = (".fits", ".fit")


def _lower_priority() -> None:
    # On Linux `setpriority` on a thread id renices just that thread.
    with_id = getattr(threading, "get_native_id", None)
    if with_id is not None and hasattr(os, "setpriority"):
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, with_id(), 10)


class FrameBrowser:
    def __init__(self, root: Path) -> None:
        self.root = root
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
        await loop.run_in_executor(self._renderer, _render, frame, jpeg, SIZES[size])
        return jpeg

    def close(self) -> None:
        self._renderer.shutdown(wait=False, cancel_futures=True)


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
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buffer, format="JPEG", quality=82)
    jpeg.parent.mkdir(parents=True, exist_ok=True)
    partial = jpeg.with_suffix(".jpg.part")
    partial.write_bytes(buffer.getvalue())
    partial.replace(jpeg)
