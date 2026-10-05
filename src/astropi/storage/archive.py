"""Captured frames on disk, as FITS.

Every deliberate capture - the Capture button and every frame of a run -
is written here as it arrives. The in-memory frame store is only for
looking at recent frames; this is the record of the night.

Laid out the way stacking software expects to find it:

    <root>/<night>/<target>/<kind>_<target>_<exposure>s_g<gain>_<n>.fits

`night` is the date the evening started, so a session that runs past
midnight stays in one folder.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
from pathlib import Path

import numpy as np

from astropi.core.errors import AstropiError
from astropi.devices.camera import Frame

logger = logging.getLogger(__name__)


class ArchiveError(AstropiError):
    """A frame could not be written, so it would be lost."""


def _mount_point(path: Path) -> Path:
    path = path.resolve()
    while not os.path.ismount(path):
        path = path.parent
    return path


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "untitled"


class FrameArchive:
    def __init__(self, root: Path) -> None:
        self.root = root

    def check(self) -> None:
        """Refuse to write to the SD card when the drive meant for frames is missing.

        A folder under /mnt or /media that resolves to the root filesystem
        is an unplugged drive, and writing a night of 52 MB frames there
        fills the SD card and takes the Pi down with it.
        """
        existing = self.root
        while not existing.exists():
            existing = existing.parent
        if str(self.root).startswith(("/mnt/", "/media/")) and _mount_point(existing) == Path("/"):
            raise ArchiveError(f"{self.root} is not on a mounted drive - is the SSD plugged in and mounted?")

    async def save(
        self,
        frame: Frame,
        *,
        target: str | None,
        header: dict[str, object] | None = None,
        folder: str | None = None,
    ) -> Path:
        return await asyncio.to_thread(self._save, frame, target, header or {}, folder)

    def _save(
        self, frame: Frame, target: str | None, extra: dict[str, object], folder_name: str | None
    ) -> Path:
        from astropy.io import fits

        self.check()
        started = dt.datetime.fromtimestamp(frame.started_at).astimezone()
        night = (started - dt.timedelta(hours=12)).date().isoformat()
        name = _slug(target or "no_target")
        # Calibration frames belong to the night, not to a target:
        # `calibration/flats` rather than whatever was last pointed at.
        folder = self.root / night / (folder_name or name)
        folder.mkdir(parents=True, exist_ok=True)

        kind = str(frame.request.kind)
        gain = frame.metadata.get("gain")
        label = _slug(folder_name.rsplit("/", 1)[-1]) if folder_name else f"{kind}_{name}"
        stem = f"{label}_{frame.request.duration_s:g}s_g{gain}"
        index = 1 + sum(1 for _ in folder.glob(f"{stem}_*.fits"))
        path = folder / f"{stem}_{index:04d}.fits"

        hdu = fits.PrimaryHDU(np.ascontiguousarray(frame.data, dtype=np.uint16))
        h = hdu.header
        h["IMAGETYP"] = (kind.capitalize(), "light, dark, flat or bias")
        h["EXPTIME"] = (frame.request.duration_s, "seconds")
        h["DATE-OBS"] = (
            dt.datetime.fromtimestamp(frame.started_at, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            "UTC, start of exposure",
        )
        h["OBJECT"] = target or ""
        h["INSTRUME"] = str(frame.metadata.get("camera", ""))
        if gain is not None:
            h["GAIN"] = gain
        if frame.metadata.get("offset") is not None:
            h["OFFSET"] = frame.metadata["offset"]
        binning = int(frame.metadata.get("binning", 1))
        h["XBINNING"] = binning
        h["YBINNING"] = binning
        h["XPIXSZ"] = (frame.sensor.pixel_size_um * binning, "microns, binned")
        h["YPIXSZ"] = (frame.sensor.pixel_size_um * binning, "microns, binned")
        if frame.metadata.get("sensor_temp_c") is not None:
            h["CCD-TEMP"] = (frame.metadata["sensor_temp_c"], "C")
        if frame.sensor.bayer_pattern:
            h["BAYERPAT"] = frame.sensor.bayer_pattern
            h["XBAYROFF"] = 0
            h["YBAYROFF"] = 0
        for key, value in extra.items():
            if value is not None:
                h[key] = value

        # Written beside its final name and then moved, so a frame cut off
        # by a full disk or a pulled cable never looks like a whole one.
        partial = path.with_suffix(".fits.part")
        hdu.writeto(partial, overwrite=True)
        partial.replace(path)
        logger.info("saved %s", path)
        return path
