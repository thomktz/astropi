"""Calibration frames: flats, darks and dark flats.

Each is a run of identical frames written to the night's `calibration`
folder, and each has one thing that makes it right or useless:

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

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

from astropi.core.errors import AstropiError
from astropi.devices.camera import ExposureRequest, FrameKind, Roi
from astropi.sequencing.task import Task

if TYPE_CHECKING:
    from astropi.runtime import Observatory


class CalibrationKind(StrEnum):
    FLATS = "flats"
    DARKS = "darks"
    DARK_FLATS = "dark_flats"


#: Longest flat exposure tried. A flat panel or a dusk sky that needs more
#: than this is too dim to flat with.
MAX_FLAT_S = 30.0
MIN_FLAT_S = 0.001
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
    target_level: float = 0.5
    #: Flats only: how close to the target counts as there.
    tolerance: float = 0.1


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
            paths.append(await self._observatory.save_capture(frame, folder=f"calibration/{plan.kind}"))
            self._observatory.frames.add(frame)

        self.report("done", fraction=1.0, message=f"{plan.count} frames saved, {exposure:g}s each")
        return {"exposure_s": exposure, "count": plan.count, "paths": paths}

    async def _find_flat_exposure(self) -> float:
        """Scale the exposure until the median sits at the target level.

        Test frames are a 1024-pixel square from the middle of the sensor:
        enough pixels for a steady median, a fraction of the download. The
        offset pedestal makes the scaling slightly short of proportional,
        which the next try corrects; it settles in two or three.
        """
        plan = self._plan
        camera = self._observatory.camera()
        sensor = camera.sensor
        full = float((1 << sensor.bit_depth) - 1)
        target = plan.target_level * full
        side = min(1024, sensor.width, sensor.height)
        roi = Roi((sensor.width - side) // 2, (sensor.height - side) // 2, side, side)

        stored = self._observatory.state.get("last_flat")
        exposure = float(stored.get("exposure_s", 1.0)) if stored else 1.0
        for attempt in range(1, MAX_TRIES + 1):
            frame = await camera.expose(
                ExposureRequest(
                    duration_s=exposure,
                    gain=plan.gain,
                    offset=plan.offset,
                    binning=plan.binning,
                    roi=roi,
                    kind=FrameKind.FLAT,
                )
            )
            median = float(np.median(frame.data))
            level = median / full
            self.report(
                "finding exposure",
                message=(
                    f"Test {attempt}: {exposure:.3g}s gives {level:.0%} (aiming for {plan.target_level:.0%})"
                ),
                exposure_s=exposure,
                level=level,
            )
            if abs(median - target) <= plan.tolerance * full:
                # Whole milliseconds, so dark flats can match it exactly
                # and the file names stay readable.
                return max(MIN_FLAT_S, round(exposure, 3))
            if median >= 0.95 * full:
                # Saturated: the median says nothing about how far over.
                exposure /= 4
            else:
                exposure *= target / max(median, 1.0)
            if exposure < MIN_FLAT_S or exposure > MAX_FLAT_S:
                limit = "too bright even at 1 ms" if exposure < MIN_FLAT_S else "too dim - over 30 s"
                raise AstropiError(f"cannot find a flat exposure: the light is {limit}")
        raise AstropiError(f"the flat level did not settle in {MAX_TRIES} tries - is the light steady?")
