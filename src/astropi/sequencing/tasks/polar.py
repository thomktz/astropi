"""Polar alignment as a task.

Wraps `astropi.services.polaralign` so the sweep reports progress and can be
cancelled mid-way - which matters, because the operator is standing at the
mount with a hand on the knobs while it runs.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astropi.core.geometry import RaDec
from astropi.devices.camera import ExposureRequest, FrameKind
from astropi.sequencing.task import Task
from astropi.services.platesolve import SolveHint
from astropi.services.polaralign import (
    DEFAULT_SEPARATION_DEG,
    PolarAlignmentError,
)

if TYPE_CHECKING:
    from astropi.runtime import Observatory


class PolarAlignTask(Task):
    kind = "polar_align"

    def __init__(
        self,
        observatory: Observatory,
        *,
        points: int = 3,
        separation_deg: float = DEFAULT_SEPARATION_DEG,
        exposure_s: float = 4.0,
        start: RaDec | None = None,
    ) -> None:
        super().__init__(name="Polar alignment")
        self._observatory = observatory
        self._points = points
        self._separation_deg = separation_deg
        self._exposure_s = exposure_s
        self._start = start

    async def run(self) -> PolarAlignmentError:
        mount = self._observatory.mount()
        camera = self._observatory.camera()
        service = self._observatory.polar_alignment
        service.reset()

        targets = service.sweep_targets(
            self._start, points=self._points, separation_deg=self._separation_deg
        )
        self.report(
            "starting",
            fraction=0.0,
            message=f"Sweeping {self._points} points {self._separation_deg:g}° apart in hour angle",
        )
        await mount.unpark()

        for index, target in enumerate(targets, start=1):
            fraction = (index - 1) / self._points
            self.report("slewing", fraction=fraction, message=f"Point {index}: slewing to {target}")
            await mount.slew_to(target)
            await mount.wait_for_slew()
            # Let the gears relax before exposing: a frame taken while the
            # mount is still settling solves to a position it has already
            # left, and that tilts the fitted plane.
            await asyncio.sleep(1.5)

            self.report("solving", fraction=fraction, message=f"Point {index}: solving")
            frame = await camera.expose(
                ExposureRequest(duration_s=self._exposure_s, kind=FrameKind.PREVIEW)
            )
            self._observatory.frames.add(frame)
            status = await mount.status()
            solved = await self._observatory.plate_solver.solve(
                frame, SolveHint(center=status.position)
            )
            service.record(solved.center)
            self.report(
                "measured",
                fraction=index / self._points,
                message=f"Point {index}: {solved.center}",
            )

        error = service.compute()
        self.report(
            "measured",
            fraction=1.0,
            message=f"Polar error {error.total_error_arcmin:.1f}'",
            altitude_error_arcmin=round(error.altitude_error_arcmin, 2),
            azimuth_error_arcmin=round(error.azimuth_error_arcmin, 2),
            total_error_arcmin=round(error.total_error_arcmin, 2),
            instructions=error.instructions(self._observatory.site.hemisphere),
        )
        return error
