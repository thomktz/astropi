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
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from astropi.core.errors import SolveFailedError
from astropi.core.geometry import RaDec
from astropi.devices.camera import Camera, ExposureRequest, FrameKind
from astropi.devices.mount import Mount
from astropi.sequencing.task import Task
from astropi.services.catalog import Target
from astropi.services.platesolve import PlateSolveService, SolveHint, SolveResult

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
        catalog_target: Target | None = None,
        tolerance_arcmin: float = DEFAULT_TOLERANCE_ARCMIN,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        exposure_s: float = DEFAULT_EXPOSURE_S,
    ) -> None:
        super().__init__(name=name or "GoTo and centre")
        self._observatory = observatory
        self._target = target
        self._catalog_target = catalog_target
        self._tolerance_deg = tolerance_arcmin / 60.0
        self._max_iterations = max_iterations
        self._exposure_s = exposure_s

    async def run(self) -> CenteringResult:
        mount = self._observatory.mount()
        camera = self._observatory.camera()
        solver = self._observatory.plate_solver

        steps: list[CenteringStep] = []
        # Reported as it grows, so a client shows the run converging
        # rather than reconstructing it from a list of sentences.
        history: list[dict] = []
        # How long each stage of the current pass took. Timed here rather
        # than in the browser: this code knows exactly where the
        # boundaries are, a client watching step names has to infer them,
        # and one that joined halfway cannot know at all.
        stage_seconds: dict[str, float] = {}
        # Record it before moving, so the dashboard names what the rig is
        # doing from the first second of a slew rather than at the end.
        self._observatory.set_active_target(
            self._catalog_target or self._observatory.target_for_coord(self._target)
        )
        # Everything the progress window needs about where this is going,
        # said once at the start: it is fixed for the whole run, and a
        # client should not have to parse it back out of a sentence.
        #
        # That includes how far the slew has to travel, measured before it
        # starts. A progress bar needs a denominator, and the only honest
        # one is the distance that was actually asked for: a client
        # watching the remaining distance shrink cannot know what it
        # started from, least of all one that opened halfway through.
        before = (await mount.status()).position
        self.report(
            "slewing",
            fraction=0.0,
            message=f"Slewing to {self._target}",
            slew_distance_deg=round(before.separation_deg(self._target), 4),
            target_ra_deg=self._target.ra_deg,
            target_dec_deg=self._target.dec_deg,
            target_name=(self._catalog_target.display_name if self._catalog_target else None),
            tolerance_arcmin=round(self._tolerance_deg * 60.0, 3),
            max_passes=self._max_iterations,
            exposure_s=self._exposure_s,
        )
        await mount.unpark()
        slew_started = time.monotonic()
        await mount.slew_to(self._target)
        await mount.wait_for_slew()
        stage_seconds["slew"] = round(time.monotonic() - slew_started, 2)

        # Tracking comes on by itself when the slew lands. Saying so beats
        # leaving the operator to spot that a state they never asked for has
        # changed.
        if (await mount.status()).tracking:
            self.report("slewed", message="Arrived; tracking at sidereal rate")

        for iteration in range(1, self._max_iterations + 1):
            fraction = iteration / (self._max_iterations + 1)
            self.report(
                "solving",
                stage_seconds=dict(stage_seconds),
                fraction=fraction,
                message=f"Exposing {self._exposure_s:g}s for solve {iteration}",
                iteration=iteration,
                max_passes=self._max_iterations,
            )

            def exposed(
                seconds: float,
                # Bound at definition rather than captured: the loop
                # rebinds all three, and a closure that read them later
                # would report the next pass's numbers for this one.
                *,
                timings: dict[str, float] = stage_seconds,
                fraction: float = fraction,
                iteration: int = iteration,
            ) -> None:
                # Said as soon as the shutter closes, rather than after the
                # solve: the panel showing how long the exposure took only
                # once the *next* stage finished made the readout lag the
                # thing it describes by a whole stage.
                timings["expose"] = seconds
                self.report(
                    "solving",
                    fraction=fraction,
                    message=f"Solving frame {iteration}",
                    iteration=iteration,
                    max_passes=self._max_iterations,
                    stage_seconds=dict(timings),
                )

            try:
                result, exposure_seconds = await self._solve(
                    camera, solver, mount, on_exposed=exposed
                )
                solved = result.center
                stage_seconds["expose"] = exposure_seconds
                stage_seconds["solve"] = round(result.solve_time_s, 2)
            except SolveFailedError as error:
                # A failed solve is not fatal on its own - clouds pass. Retry
                # while iterations remain rather than abandoning the target.
                self.report(
                    "solve_failed",
                    stage_seconds=dict(stage_seconds),
                    message=f"Solve {iteration} failed: {error}",
                    iteration=iteration,
                    max_passes=self._max_iterations,
                )
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
            history.append(
                {
                    "pass": iteration,
                    "error_arcmin": round(error_deg * 60.0, 3),
                    "stars": result.stars_detected,
                    "seconds": round(result.solve_time_s, 2),
                    "within": within,
                }
            )
            self.report(
                "solved",
                stage_seconds=dict(stage_seconds),
                fraction=fraction,
                message=f"Solved {solved}, {error_deg * 60:.2f}' from target",
                passes_done=list(history),
                iteration=iteration,
                max_passes=self._max_iterations,
                error_arcmin=round(error_deg * 60.0, 3),
                within_tolerance=within,
                solved_ra_deg=solved.ra_deg,
                solved_dec_deg=solved.dec_deg,
                stars=result.stars_detected,
                solver=result.solver,
                solve_seconds=round(result.solve_time_s, 2),
                pixel_scale_arcsec=round(result.pixel_scale_arcsec, 3),
                rotation_deg=round(result.field_rotation_deg, 2),
            )

            # Sync either way: even when already centred, telling the mount
            # the truth improves its model for the next target of the night.
            sync_started = time.monotonic()
            await mount.sync_to(solved)
            stage_seconds["sync"] = round(time.monotonic() - sync_started, 2)

            if within:
                self.report(
                    "centred",
                    stage_seconds=dict(stage_seconds),
                    fraction=1.0,
                    message=f"Centred to {error_deg * 60:.2f}' in {iteration} pass(es)",
                    passes_done=list(history),
                    iteration=iteration,
                    error_arcmin=round(error_deg * 60.0, 3),
                    passes=iteration,
                )
                return CenteringResult(True, self._target, solved, error_deg * 60.0, steps)

            # The correction slew is a slew like the first one, and it was
            # invisible: the step stayed on "solved" throughout, so a
            # client watching stages saw the time land on the sync.
            self.report(
                "slewing",
                fraction=fraction,
                message=f"Correcting by {error_deg * 60:.2f}'",
                iteration=iteration,
                max_passes=self._max_iterations,
                stage_seconds=dict(stage_seconds),
                slew_distance_deg=round(error_deg, 4),
            )
            slew_started = time.monotonic()
            await mount.slew_to(self._target)
            await mount.wait_for_slew()
            stage_seconds = {"slew": round(time.monotonic() - slew_started, 2)}

        last = steps[-1] if steps else None
        self.report(
            "not_converged",
            stage_seconds=dict(stage_seconds),
            message=f"Did not centre within {self._max_iterations} passes",
            passes_done=list(history),
        )
        return CenteringResult(
            False,
            self._target,
            last.solved if last else None,
            last.error_arcmin if last else None,
            steps,
        )

    async def _solve(
        self,
        camera: Camera,
        solver: PlateSolveService,
        mount: Mount,
        *,
        on_exposed: Callable[[float], None] | None = None,
    ) -> tuple[SolveResult, float]:
        exposure_started = time.monotonic()
        frame = await camera.expose(
            ExposureRequest(duration_s=self._exposure_s, kind=FrameKind.PREVIEW)
        )
        # The whole cost of getting the picture: the shutter, the readout
        # and the download, which on a 26 megapixel sensor is most of it.
        exposure_seconds = round(time.monotonic() - exposure_started, 2)
        if on_exposed is not None:
            on_exposed(exposure_seconds)
        # Keep it. In an image-first dashboard the frames a centring loop
        # solves are the most useful thing on screen - they are what shows
        # the rig converging - and dropping them left the viewer empty
        # during the one operation worth watching.
        self._observatory.frames.add(frame)
        status = await mount.status()
        # Hand the solver the mount's belief: a local search around a rough
        # guess is seconds, a blind all-sky search is minutes.
        hint = SolveHint(
            center=status.position,
            radius_deg=10.0,
            pixel_scale_arcsec=frame.metadata.get("pixel_scale_arcsec"),
        )
        return await solver.solve(frame, hint), exposure_seconds
