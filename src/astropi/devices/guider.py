"""Guider contract.

A protocol rather than a concrete class because guiding can legitimately
come from two places: the built-in loop in `astropi.services.guiding`, which
drives a guide camera and the mount's pulse-guide primitive directly, or an
external PHD2 process reached over its JSON-RPC socket. Sequences ask for
"settled guiding" without caring which one is running.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class GuidingState(StrEnum):
    STOPPED = "stopped"
    CALIBRATING = "calibrating"
    GUIDING = "guiding"
    SETTLING = "settling"
    DITHERING = "dithering"
    LOST = "lost"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class GuideSample:
    """One iteration of the guide loop.

    Errors are reported in arcseconds as well as pixels: pixels are what was
    measured, arcseconds are what is comparable between rigs and what the
    RMS figure everyone quotes actually means.
    """

    timestamp: float
    #: Where the star and its lock point sit on the sensor, in pixels.
    star_x: float
    star_y: float
    lock_x: float
    lock_y: float
    ra_error_px: float
    dec_error_px: float
    ra_error_arcsec: float
    dec_error_arcsec: float
    ra_pulse_ms: float
    dec_pulse_ms: float
    star_flux: float
    star_hfd: float
    snr: float


@dataclass(frozen=True, slots=True)
class GuideCalibration:
    """Maps mount pulses onto sensor axes.

    Rates are arcseconds of image motion per second of pulse; `angle_deg` is
    the rotation between the camera's axes and the mount's. Both change when
    the camera is rotated or the mount flips, which is why calibration is
    stored and invalidated rather than assumed constant.
    """

    ra_rate_arcsec_per_s: float
    dec_rate_arcsec_per_s: float
    angle_deg: float
    pixel_scale_arcsec: float
    calibrated_at: float
    dec_at_calibration_deg: float
    #: Where the star actually went on the sensor, in pixels, for the two
    #: legs that were measured. Kept rather than reduced to a rate and an
    #: angle, because those two numbers cannot answer "did declination
    #: come out perpendicular to right ascension, and which way round" -
    #: and that question is exactly what a declination axis guiding
    #: backwards looks like from the outside.
    west_shift_px: tuple[float, float] = (0.0, 0.0)
    north_shift_px: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True, slots=True)
class GuidingStatus:
    state: GuidingState
    calibration: GuideCalibration | None = None
    rms_ra_arcsec: float | None = None
    rms_dec_arcsec: float | None = None
    rms_total_arcsec: float | None = None
    samples: int = 0


@runtime_checkable
class Guider(Protocol):
    async def status(self) -> GuidingStatus: ...

    async def calibrate(self) -> GuideCalibration: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def dither(self, amount_px: float, *, settle_px: float, settle_time_s: float) -> None:
        """Offset the guide star, then wait for tracking to settle again.

        Blocking until settled is the point: an imaging sequence must not
        open the shutter while the mount is still recovering from the nudge.
        """
        ...
