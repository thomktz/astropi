"""GoTo with plate-solve centring.

A mount's GoTo lands close, not exact - polar misalignment, cone error,
backlash and an imperfect pointing model all contribute. Rather than ask the
operator to star-align first, this closes the loop: point, photograph, solve
for where the camera really is, sync the mount to that truth, and go again.
Each pass folds the observed error into the mount's model, so it converges
in two or three iterations.

The first successful solve also replaces the session's star alignment - the
sync *is* the alignment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from astropi.core.errors import SolveFailedError
from astropi.core.geometry import RaDec
from astropi.devices.camera import Camera, ExposureRequest, FrameKind
from astropi.devices.mount import Mount
from astropi.sequencing.task import Task
from astropi.services.platesolve import PlateSolveService, SolveHint

if TYPE_CHECKING:
    from astropi.runtime import Observatory

#: Consider the target centred once within this angular distance.
DEFAULT_TOLERANCE_ARCMIN = 1.0
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_EXPOSURE_S = 4.0


@dataclass(slots=True)
class CenteringStep:
    iteration: int
    solved: RaDec
    error_arcmin: float
    corrected: bool


@dataclass(slots=True)
class CenteringResult:
    converged: bool
    target: RaDec
    final: RaDec | None
    error_arcmin: float | None
    steps: list[CenteringStep]


class GotoAndCenterTask(Task):
    kind = "goto_center"

    def __init__(
        self,
        observatory: Observatory,
        target: RaDec,
        *,
        name: str | None = None,
        tolerance_arcmin: float = DEFAULT_TOLERANCE_ARCMIN,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        exposure_s: float = DEFAULT_EXPOSURE_S,
    ) -> None:
        super().__init__(name=name or "GoTo and centre")
        self._observatory = observatory
        self._target = target
        self._tolerance_deg = tolerance_arcmin / 60.0
        self._max_iterations = max_iterations
        self._exposure_s = exposure_s

    async def run(self) -> CenteringResult:
        mount = self._observatory.mount()
        camera = self._observatory.camera()
        solver = self._observatory.plate_solver

        steps: list[CenteringStep] = []
        self.report("slewing", fraction=0.0, message=f"Slewing to {self._target}")
        await mount.unpark()
        await mount.slew_to(self._target)
        await mount.wait_for_slew()

        for iteration in range(1, self._max_iterations + 1):
            fraction = iteration / (self._max_iterations + 1)
            self.report(
                "solving",
                fraction=fraction,
                message=f"Exposing {self._exposure_s:g}s for solve {iteration}",
                iteration=iteration,
            )

            try:
                solved = await self._solve(camera, solver, mount)
            except SolveFailedError as error:
                # A failed solve is not fatal on its own - clouds pass. Retry
                # while iterations remain rather than abandoning the target.
                self.report("solve_failed", message=f"Solve {iteration} failed: {error}")
                if iteration == self._max_iterations:
                    return CenteringResult(False, self._target, None, None, steps)
                await asyncio.sleep(1.0)
                continue

            error_deg = solved.separation_deg(self._target)
            within = error_deg <= self._tolerance_deg
            steps.append(
                CenteringStep(
                    iteration=iteration,
                    solved=solved,
                    error_arcmin=error_deg * 60.0,
                    corrected=not within,
                )
            )
            self.report(
                "solved",
                fraction=fraction,
                message=f"Solved {solved}, {error_deg * 60:.2f}' from target",
                error_arcmin=round(error_deg * 60.0, 3),
                solved_ra_deg=solved.ra_deg,
                solved_dec_deg=solved.dec_deg,
            )

            # Sync either way: even when already centred, telling the mount
            # the truth improves its model for the next target of the night.
            await mount.sync_to(solved)

            if within:
                self.report(
                    "centred",
                    fraction=1.0,
                    message=f"Centred to {error_deg * 60:.2f}' in {iteration} pass(es)",
                )
                return CenteringResult(True, self._target, solved, error_deg * 60.0, steps)

            await mount.slew_to(self._target)
            await mount.wait_for_slew()

        last = steps[-1] if steps else None
        self.report("not_converged", message=f"Did not centre within {self._max_iterations} passes")
        return CenteringResult(
            False,
            self._target,
            last.solved if last else None,
            last.error_arcmin if last else None,
            steps,
        )

    async def _solve(self, camera: Camera, solver: PlateSolveService, mount: Mount) -> RaDec:
        frame = await camera.expose(
            ExposureRequest(duration_s=self._exposure_s, kind=FrameKind.PREVIEW)
        )
        status = await mount.status()
        # Hand the solver the mount's belief: a local search around a rough
        # guess is seconds, a blind all-sky search is minutes.
        hint = SolveHint(
            center=status.position,
            radius_deg=10.0,
            pixel_scale_arcsec=frame.metadata.get("pixel_scale_arcsec"),
        )
        return (await solver.solve(frame, hint)).center
