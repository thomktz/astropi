"""Captured frames on disk, as FITS.

Every deliberate capture - the Capture button and every frame of a run -
is written here as it arrives. The in-memory frame store is only for
looking at recent frames; this is the record of the night.

Laid out the way stacking software expects to find it - see `naming`:

    <root>/<night>_<Target>/<LIGHT|DARK|FLAT|DARKFLAT>/<Target>_<TYPE>_<settings>_<time>_<n>.fits

`night` is the date the evening started, so a session that runs past
midnight stays in one folder. Nights saved before this layout,
`<night>/<target>/light_*.fits`, are still found by `lights`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from pathlib import Path

import numpy as np

from astropi.core.errors import AstropiError
from astropi.devices.camera import Frame, FrameKind
from astropi.storage.naming import DARK_LIBRARY, ImageType, frame_stem, night_of, session_folder

logger = logging.getLogger(__name__)


class ArchiveError(AstropiError):
    """A frame could not be written, so it would be lost."""


def _mount_point(path: Path) -> Path:
    path = path.resolve()
    while not os.path.ismount(path):
        path = path.parent
    return path


def _ascii(text: str) -> str:
    text = text.replace("\u00b0", "d").replace("\u2032", "'").replace("\u2033", '"')
    return "".join(c for c in text if 32 <= ord(c) < 127)


_TYPE_OF_KIND = {
    FrameKind.LIGHT: ImageType.LIGHT,
    FrameKind.DARK: ImageType.DARK,
    FrameKind.FLAT: ImageType.FLAT,
    FrameKind.BIAS: ImageType.BIAS,
}


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
        image_type: ImageType | None = None,
        directory: Path | None = None,
        night: str | None = None,
        temp_c: float | None = None,
        filter_name: str | None = None,
    ) -> Path:
        """Write a frame where stacking software expects it.

        `directory` puts it somewhere other than its night's target folder
        (the dark library); `night` keeps a session's frames in the
        session's folder even when its darks are shot on a later night;
        `temp_c` is the cooler's set point, named in the file instead of the
        sensor reading, which wanders by a fraction of a degree; `filter_name`
        goes in the FILTER header and in light and flat names.
        """
        return await asyncio.to_thread(
            self._save, frame, target, header or {}, image_type, directory, night, temp_c, filter_name
        )

    def _save(
        self,
        frame: Frame,
        target: str | None,
        extra: dict[str, object],
        image_type: ImageType | None,
        directory: Path | None,
        night: str | None,
        temp_c: float | None,
        filter_name: str | None,
    ) -> Path:
        from astropy.io import fits

        self.check()
        started = dt.datetime.fromtimestamp(frame.started_at).astimezone()
        image_type = image_type or _TYPE_OF_KIND.get(frame.request.kind, ImageType.LIGHT)
        if directory is None:
            directory = self.root / session_folder(night or night_of(started), target) / str(image_type)
        directory.mkdir(parents=True, exist_ok=True)

        library = directory.parent.name == DARK_LIBRARY
        if library:
            # Library darks belong to no target, in the name or the header.
            target = None
        gain = frame.metadata.get("gain")
        sensor_c = frame.metadata.get("sensor_temp_c")
        stem = frame_stem(
            image_type,
            target=None if library else (target or "no-target"),
            exposure_s=frame.request.duration_s,
            gain=gain,
            offset=frame.metadata.get("offset"),
            temp_c=temp_c if temp_c is not None else sensor_c,
            started=started,
            filter_name=filter_name,
        )
        index = 1 + sum(1 for _ in directory.glob("*.fits"))
        path = directory / f"{stem}_{index:04d}.fits"

        hdu = fits.PrimaryHDU(np.ascontiguousarray(frame.data, dtype=np.uint16))
        h = hdu.header
        h["IMAGETYP"] = image_type.fits_value
        h["EXPTIME"] = (frame.request.duration_s, "seconds")
        h["DATE-OBS"] = (
            dt.datetime.fromtimestamp(frame.started_at, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            "UTC, start of exposure",
        )
        # FITS headers are ASCII only: a target named by its coordinates
        # carries degree signs, and an exception here would lose the frame.
        h["OBJECT"] = _ascii(target or "")
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
        if filter_name:
            h["FILTER"] = _ascii(filter_name)
        if temp_c is not None:
            h["SET-TEMP"] = (temp_c, "C, cooler set point")
        if frame.sensor.bayer_pattern:
            h["BAYERPAT"] = frame.sensor.bayer_pattern
            h["XBAYROFF"] = 0
            h["YBAYROFF"] = 0
        for key, value in extra.items():
            if value is not None:
                h[key] = _ascii(value) if isinstance(value, str) else value
        if "sim_true_ra_deg" in frame.metadata:
            # The simulator's ground truth, so a simulated frame can be
            # solved again later when it is used as a framing reference.
            h["SIMRA"] = frame.metadata["sim_true_ra_deg"]
            h["SIMDEC"] = frame.metadata["sim_true_dec_deg"]
            h["SIMROT"] = frame.metadata.get("rotation_deg", 0.0)

        # Written beside its final name and then moved, so a frame cut off
        # by a full disk or a pulled cable never looks like a whole one.
        partial = path.with_suffix(".fits.part")
        hdu.writeto(partial, overwrite=True)
        partial.replace(path)
        logger.info("saved %s", path)
        return path

    def lights(self) -> list[dict[str, object]]:
        """Saved light frames, grouped by night and target, newest first."""
        groups: dict[tuple[str, str], list[Path]] = {}
        if not self.root.exists():
            return []
        for path in self.root.glob("*/LIGHT/*.fits"):
            night, _, target = path.parent.parent.name.partition("_")
            groups.setdefault((night, target.replace("-", " ")), []).append(path)
        # The layout before folders were named <night>_<Target>.
        for path in self.root.glob("*/*/light_*.fits"):
            groups.setdefault((path.parent.parent.name, path.parent.name.replace("_", " ")), []).append(path)
        out = []
        for (night, target), paths in groups.items():
            paths.sort(key=lambda p: p.stat().st_mtime)
            out.append(
                {
                    "night": night,
                    "target": target,
                    "count": len(paths),
                    "latest": str(paths[-1].relative_to(self.root)),
                    "frames": [str(p.relative_to(self.root)) for p in paths[-50:]],
                }
            )
        out.sort(key=lambda group: (group["night"], group["latest"]), reverse=True)
        return out

    def resolve(self, relative: str) -> Path:
        """A path inside the archive, refusing anything that escapes it."""
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents or path.suffix.lower() not in (".fits", ".fit"):
            raise ArchiveError(f"{relative} is not a frame in the archive")
        if not path.exists():
            raise ArchiveError(f"{relative} does not exist")
        return path

    def load(self, relative: str) -> tuple[Frame, dict[str, object]]:
        """A saved frame and its header, ready to be plate solved."""
        from astropy.io import fits

        from astropi.devices.camera import ExposureRequest, FrameKind, SensorInfo

        path = self.resolve(relative)
        with fits.open(path) as hdul:
            data = np.asarray(hdul[0].data, dtype=np.uint16)
            header = dict(hdul[0].header)
        binning = int(header.get("XBINNING", 1))
        pixel = float(header.get("XPIXSZ", 3.76)) / binning
        focal = float(header.get("FOCALLEN", 400.0))
        metadata: dict[str, object] = {
            "gain": header.get("GAIN"),
            "binning": binning,
            "pixel_scale_arcsec": 206.264806 * pixel * binning / focal,
        }
        if "SIMRA" in header:
            metadata.update(
                sim_true_ra_deg=float(header["SIMRA"]),
                sim_true_dec_deg=float(header["SIMDEC"]),
                rotation_deg=float(header.get("SIMROT", 0.0)),
            )
        frame = Frame(
            data=data,
            request=ExposureRequest(duration_s=float(header.get("EXPTIME", 1.0)), kind=FrameKind.LIGHT),
            sensor=SensorInfo(
                width=data.shape[1],
                height=data.shape[0],
                pixel_size_um=pixel,
                bit_depth=16,
                has_color_filter_array=bool(header.get("BAYERPAT")),
                bayer_pattern=header.get("BAYERPAT") or None,
            ),
            metadata=metadata,
        )
        return frame, header
