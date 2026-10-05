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
    #: Which way each correction was sent, as the loop decided it. Named
    #: rather than left to be re-derived from the sign of the error: two
    #: implementations of one convention is one too many, and the whole
    #: point of showing it is to be able to check it.
    ra_direction: str = ""
    dec_direction: str = ""
    #: Why a correction was not sent, when one was not: below the dead
    #: band, or refused by the declination mode.
    ra_withheld: str = ""
    dec_withheld: str = ""
    #: The pixel model after this frame: smoothed position, drift and
    #: the corrections' effect, all in sensor pixels. See `guidemodel`.
    fit: dict | None = None
    #: The change in error this sample's pulses should cause, per the
    #: calibration. The next sample shows what they actually did.
    ra_predicted_arcsec: float = 0.0
    dec_predicted_arcsec: float = 0.0
    #: With the fine controls: the RA rate offset held after this frame,
    #: in axis arcsec/s, and the declination steps sent (north positive).
    ra_rate_offset: float = 0.0
    dec_steps: int = 0
    #: The guide star's core was clipped: its position moves in jumps.
    saturated: bool = False
    #: What the loop was doing: measuring drift with no corrections,
    #: walking the star back, or holding it.
    mode: str = "hold"


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
    #: "pulse", or "fine": right ascension by rate offset and declination
    #: by motor steps, measured with those same controls.
    mode: str = "pulse"
    #: Sky arcsec the star moves per arcsec of RA axis rotation - the
    #: cosine of the declination, measured rather than assumed.
    ra_sky_per_axis: float | None = None
    #: Sky arcsec one declination motor step moves the star.
    dec_arcsec_per_step: float | None = None
    #: +1 when north moves the star the way the declination axis on the
    #: sensor points, -1 when the optics mirror it.
    dec_north_sign: float = 1.0
    #: What one unit of each correction moves the star, as (dx, dy) on the
    #: sensor - the calibration, in the terms the guide loop works in. A
    #: unit is an arcsec of RA axis and one declination step with the fine
    #: controls, a millisecond of pulse without; positive is west and north.
    ra_response_px: tuple[float, float] = (0.0, 0.0)
    dec_response_px: tuple[float, float] = (0.0, 0.0)
    ra_unit: str = "ms"
    dec_unit: str = "ms"
    #: Where the mount pointed, alongside `dec_at_calibration_deg`.
    ra_at_calibration_deg: float | None = None
    #: When each measured leg's first and last frames were taken, Unix
    #: seconds: the sky drifted for that long while the leg was measured.
    west_leg_at: tuple[float, float] | None = None
    north_leg_at: tuple[float, float] | None = None
    #: Whether the drift during the legs has been taken back out, once
    #: guiding had measured it.
    drift_corrected: bool = False


@dataclass(frozen=True, slots=True)
class GuidingStatus:
    state: GuidingState
    calibration: GuideCalibration | None = None
    rms_ra_arcsec: float | None = None
    rms_dec_arcsec: float | None = None
    rms_total_arcsec: float | None = None
    samples: int = 0
    #: Time between guide frames - the loop's real cadence.
    cycle_s: float | None = None
    #: The pixel model: drift in x and y, what one unit of each correction
    #: does, and what the calibration said it would. `None` uncalibrated.
    model: dict | None = None
    #: Whether the drift is being cancelled - it is not while it is still
    #: being measured, or when drift cancelling is switched off.
    cancelling: bool = False
    #: The RA rate offset held right now, axis arcsec/s.
    ra_rate_offset: float = 0.0


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
