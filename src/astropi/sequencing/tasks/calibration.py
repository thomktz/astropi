"""Calibration frames: flats, darks and dark flats.

Each is a run of identical frames written to the target's DARK, FLAT or
DARKFLAT folder, and each has one thing that makes it right or useless:

- **Flats** must sit around the middle of the sensor's range. Too dark and
  they add noise; too bright and the sensor stops being linear. So the
  exposure is found, not asked for: short test frames from the middle of
  the sensor, scaled until the level lands, then the run at that exposure.
- **Dark flats** must match the flats' exposure and gain exactly, so they
  take the exposure the last flats used rather than one typed in.
- **Darks** must match the lights' exposure, gain and temperature, which
  the panel offers from the last light frame saved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

from astropi.core.errors import AstropiError
from astropi.devices.camera import ExposureRequest, FrameKind, Roi
from astropi.sequencing.task import Task
from astropi.storage.naming import ImageType

if TYPE_CHECKING:
    from astropi.runtime import Observatory


class CalibrationKind(StrEnum):
    FLATS = "flats"
    DARKS = "darks"
    DARK_FLATS = "dark_flats"


#: Flat exposures are kept between these. Below a tenth of a second the
#: shutterless sensor's rolling readout and an LED panel's flicker show as
#: bands; over ten, the panel is too dim to flat with.
MAX_FLAT_S = 10.0
MIN_FLAT_S = 0.1
#: Shorter than this works, but a panel's PWM flicker can still leave the
#: frames uneven; the operator is told to dim the panel.
FLICKER_WARN_S = 0.5
#: Where a flat's median should sit, as a share of full scale (about
#: 26000 ADU on a 16-bit sensor), and how close, relative to it, counts.
FLAT_TARGET_LEVEL = 0.4
FLAT_TOLERANCE = 0.1
#: Test frames before giving up on finding the level.
MAX_TRIES = 8


@dataclass(slots=True)
class CalibrationPlan:
    kind: CalibrationKind
    count: int
    gain: int | None = None
    offset: int | None = None
    binning: int = 1
    #: Darks only: the exposure to match.
    exposure_s: float | None = None
    #: Flats only: where the median should land, as a share of full scale.
    target_level: float = FLAT_TARGET_LEVEL
    #: Flats only: how close to the target counts as there, relative to it.
    tolerance: float = FLAT_TOLERANCE


class CalibrationTask(Task):
    kind = "calibration"

    def __init__(self, observatory: Observatory, plan: CalibrationPlan) -> None:
        names = {
            CalibrationKind.FLATS: "Flats",
            CalibrationKind.DARKS: "Darks",
            CalibrationKind.DARK_FLATS: "Dark flats",
        }
        self._label = names[plan.kind]
        super().__init__(name=f"{self._label}, {plan.count} frames")
        self._observatory = observatory
        self._plan = plan

    async def run(self) -> dict:
        plan = self._plan
        if plan.kind is CalibrationKind.FLATS:
            exposure = await self._find_flat_exposure()
            self._observatory.state.put(
                "last_flat",
                {"exposure_s": exposure, "gain": plan.gain, "offset": plan.offset, "binning": plan.binning},
            )
            frame_kind = FrameKind.FLAT
        elif plan.kind is CalibrationKind.DARK_FLATS:
            flat = self._observatory.state.get("last_flat")
            if not flat:
                raise AstropiError("take flats first: dark flats use the flats' exposure")
            exposure = float(flat["exposure_s"])
            frame_kind = FrameKind.DARK
        else:
            if not plan.exposure_s:
                raise AstropiError("darks need the exposure of the lights they are for")
            exposure = plan.exposure_s
            frame_kind = FrameKind.DARK

        camera = self._observatory.camera()
        paths = []
        for index in range(1, plan.count + 1):
            self.report(
                "exposing",
                fraction=(index - 1) / plan.count,
                message=f"{self._label} {index}/{plan.count}, {exposure:g}s",
                frame=index,
                total=plan.count,
                exposure_s=exposure,
            )
            frame = await camera.expose(
                ExposureRequest(
                    duration_s=exposure,
                    gain=plan.gain,
                    offset=plan.offset,
                    binning=plan.binning,
                    kind=frame_kind,
                )
            )
            image_type = {
                CalibrationKind.FLATS: ImageType.FLAT,
                CalibrationKind.DARKS: ImageType.DARK,
                CalibrationKind.DARK_FLATS: ImageType.DARKFLAT,
            }[plan.kind]
            paths.append(await self._observatory.save_capture(frame, image_type=image_type))
            self._observatory.frames.add(frame)

        self.report("done", fraction=1.0, message=f"{plan.count} frames saved, {exposure:g}s each")
        return {"exposure_s": exposure, "count": plan.count, "paths": paths}

    async def _find_flat_exposure(self) -> float:
        plan = self._plan
        stored = self._observatory.state.get("last_flat")
        return await find_flat_exposure(
            self._observatory.camera(),
            lambda message, **detail: self.report("finding exposure", message=message, **detail),
            gain=plan.gain,
            offset=plan.offset,
            binning=plan.binning,
            start_s=float(stored.get("exposure_s", 1.0)) if stored else 1.0,
            target_level=plan.target_level,
            tolerance=plan.tolerance,
        )


async def find_flat_exposure(
    camera,
    report: Callable[..., None],
    *,
    gain: int | None,
    offset: int | None,
    binning: int = 1,
    start_s: float = 1.0,
    target_level: float = FLAT_TARGET_LEVEL,
    tolerance: float = FLAT_TOLERANCE,
) -> float:
    """Scale the exposure until the median sits at the target level.

    Test frames are a 1024-pixel square from the middle of the sensor:
    enough pixels for a steady median, a fraction of the download. The
    offset pedestal makes the scaling slightly short of proportional,
    which the next try corrects; it settles in two or three. Held between
    MIN_FLAT_S and MAX_FLAT_S; a panel too bright at the shortest or too
    dim at the longest is an error that says which.
    """
    sensor = camera.sensor
    full = float((1 << sensor.bit_depth) - 1)
    target = target_level * full
    side = min(1024, sensor.width, sensor.height)
    roi = Roi((sensor.width - side) // 2, (sensor.height - side) // 2, side, side)

    exposure = min(MAX_FLAT_S, max(MIN_FLAT_S, start_s))
    for attempt in range(1, MAX_TRIES + 1):
        frame = await camera.expose(
            ExposureRequest(
                duration_s=exposure,
                gain=gain,
                offset=offset,
                binning=binning,
                roi=roi,
                kind=FrameKind.FLAT,
            )
        )
        median = float(np.median(frame.data))
        report(
            f"Test {attempt}: {exposure:.3g}s gives median {median:.0f} ADU, "
            f"{median / full:.0%} (aiming for {target:.0f}, {target_level:.0%})",
            exposure_s=exposure,
            level=median / full,
        )
        if abs(median - target) <= tolerance * target:
            # Whole milliseconds, so dark flats can match it exactly
            # and the file names stay readable.
            return round(exposure, 3)
        # Saturated, the median says nothing about how far over: quarter it.
        wanted = exposure / 4 if median >= 0.95 * full else exposure * target / max(median, 1.0)
        if wanted < MIN_FLAT_S:
            if exposure <= MIN_FLAT_S:
                raise AstropiError(
                    f"the flat light is too bright even at {MIN_FLAT_S:g}s - dim the panel or add a layer"
                )
            wanted = MIN_FLAT_S
        elif wanted > MAX_FLAT_S:
            if exposure >= MAX_FLAT_S:
                raise AstropiError(f"the flat light is too dim even at {MAX_FLAT_S:g}s - brighten the panel")
            wanted = MAX_FLAT_S
        exposure = wanted
    raise AstropiError(f"the flat level did not settle in {MAX_TRIES} tries - is the light steady?")
