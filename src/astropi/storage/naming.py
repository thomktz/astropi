"""How frames are named on the SSD.

One folder per target per night, with a subfolder per frame type, the
layout Siril's and PixInsight's scripts look for:

    <root>/<YYYY-MM-DD>_<Target>/LIGHT/<Target>_LIGHT_300s_G100_O50_-10C_20261006-013012_0001.fits

and a dark library beside the nights, keyed on what a dark has to match:

    <root>/DARKS_LIBRARY/300s_G100_O50_-10C/DARK_300s_G100_O50_-10C_20261006-013012_0001.fits

The date is the evening's, so frames shot after midnight stay in the
night they belong to.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum

DARK_LIBRARY = "DARKS_LIBRARY"

#: The filters offered before any custom one is added, with their short
#: names for file names. A one-shot colour camera has no wheel, so this is
#: whatever is screwed onto the camera tonight.
FILTER_TOKENS = {
    "None": "NoFilter",
    "UV/IR cut": "UVIR",
    "L-eNhance / dual-band": "LeNhance",
    "L-eXtreme": "LeXtreme",
    "Light pollution": "LPR",
}
#: Frame types whose names say which filter: what a flat must match.
FILTERED = ("LIGHT", "FLAT", "DARKFLAT")


class ImageType(StrEnum):
    LIGHT = "LIGHT"
    DARK = "DARK"
    FLAT = "FLAT"
    DARKFLAT = "DARKFLAT"
    BIAS = "BIAS"

    @property
    def fits_value(self) -> str:
        """IMAGETYP as Siril, PixInsight and DeepSkyStacker read it."""
        return {
            ImageType.LIGHT: "Light Frame",
            ImageType.DARK: "Dark Frame",
            ImageType.FLAT: "Flat Field",
            ImageType.DARKFLAT: "Dark Flat",
            ImageType.BIAS: "Bias Frame",
        }[self]


def night_of(moment: dt.datetime) -> str:
    """The date the evening started: a night past midnight stays one night."""
    return (moment.astimezone() - dt.timedelta(hours=12)).date().isoformat()


def target_slug(name: str | None) -> str:
    """A target name fit for a path: spaces to dashes, no slashes or brackets."""
    slug = re.sub(r"[^A-Za-z0-9.+-]+", "-", (name or "").strip()).strip("-.")
    return re.sub(r"-{2,}", "-", slug) or "no-target"


def format_exposure(seconds: float) -> str:
    """300s, 0.85s, 0.001s - no trailing zeros, no exponent."""
    return f"{seconds:.4f}".rstrip("0").rstrip(".") + "s"


def settings_label(exposure_s: float, gain: object, offset: object, temp_c: float | None) -> str:
    """`300s_G100_O50_-10C`: what a calibration frame has to match."""
    parts = [format_exposure(exposure_s), f"G{gain if gain is not None else 'def'}"]
    parts.append(f"O{offset if offset is not None else 'def'}")
    if temp_c is not None:
        parts.append(f"{round(temp_c):d}C")
    return "_".join(parts)


def filter_token(name: str) -> str:
    """`LeNhance` for a known filter; letters and digits of anything else."""
    return FILTER_TOKENS.get(name) or re.sub(r"[^A-Za-z0-9]+", "", name) or "NoFilter"


def session_folder(night: str, target: str | None) -> str:
    return f"{night}_{target_slug(target)}"


def frame_stem(
    image_type: ImageType,
    *,
    target: str | None,
    exposure_s: float,
    gain: object,
    offset: object,
    temp_c: float | None,
    started: dt.datetime,
    filter_name: str | None = None,
) -> str:
    """Everything in the file name but the sequence number.

    Library darks belong to no target, so they start at the type.
    """
    prefix = f"{target_slug(target)}_" if target else ""
    stamp = started.astimezone().strftime("%Y%m%d-%H%M%S")
    filtered = f"_{filter_token(filter_name)}" if filter_name and image_type in FILTERED else ""
    return f"{prefix}{image_type}{filtered}_{settings_label(exposure_s, gain, offset, temp_c)}_{stamp}"
