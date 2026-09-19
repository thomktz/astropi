"""Autofocus by V-curve.

Star size against focuser position is a V - sharp at best focus, widening
either side. The routine samples across that curve, fits each arm, and moves
to where they intersect, which is more robust than trusting the single
lowest sample: seeing makes any one measurement noisy, but the arms are
built from many.

The final move always approaches from the same direction so that focuser
backlash is taken up consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from astropi.core.errors import AstropiError
from astropi.devices.camera import ExposureRequest, FrameKind
from astropi.sequencing.task import Task
from astropi.services.stardetect import detect_stars, mean_hfd

if TYPE_CHECKING:
    from astropi.runtime import Observatory


@dataclass(slots=True)
class FocusSample:
    position: int
    hfd: float
    stars: int


@dataclass(slots=True)
class FocusResult:
    best_position: int
    best_hfd: float
    samples: list[FocusSample]


class AutofocusTask(Task):
    kind = "autofocus"

    def __init__(
        self,
        observatory: Observatory,
        *,
        steps: int = 9,
        step_size: int = 350,
        exposure_s: float = 3.0,
        backlash_steps: int = 400,
    ) -> None:
        super().__init__(name="Autofocus")
        self._observatory = observatory
        self._steps = steps
        self._step_size = step_size
        self._exposure_s = exposure_s
        self._backlash_steps = backlash_steps

    async def run(self) -> FocusResult:
        focuser = self._observatory.focuser()
        camera = self._observatory.camera()

        start = (await focuser.status()).position
        half = self._steps // 2
        positions = [start + (i - half) * self._step_size for i in range(self._steps)]

        # Start below the lowest sample and always move upward, so every
        # measurement is taken with the backlash wound up the same way.
        await focuser.move_to(positions[0] - self._backlash_steps)
        await focuser.wait_for_move()

        samples: list[FocusSample] = []
        for index, position in enumerate(positions):
            self.report(
                "sampling",
                fraction=index / len(positions),
                message=f"Position {position}",
                position=position,
            )
            await focuser.move_to(position)
            await focuser.wait_for_move()

            frame = await camera.expose(
                ExposureRequest(duration_s=self._exposure_s, kind=FrameKind.PREVIEW, binning=2)
            )
            stars = detect_stars(frame.data, max_stars=40, bit_depth=frame.sensor.bit_depth)
            hfd = mean_hfd(stars)
            if hfd is None:
                self.report("sampling", message=f"No stars at {position}, skipping")
                continue
            samples.append(FocusSample(position=position, hfd=hfd, stars=len(stars)))
            self.report(
                "sampled",
                fraction=(index + 1) / len(positions),
                message=f"{position}: HFD {hfd:.2f} from {len(stars)} stars",
                hfd=round(hfd, 3),
            )

        if len(samples) < 3:
            raise AstropiError(f"only {len(samples)} usable focus samples; need at least 3")

        best = _fit_v_curve(samples)
        self.report("moving", message=f"Moving to fitted best focus {best}")
        await focuser.move_to(best - self._backlash_steps)
        await focuser.wait_for_move()
        await focuser.move_to(best)
        await focuser.wait_for_move()

        frame = await camera.expose(
            ExposureRequest(duration_s=self._exposure_s, kind=FrameKind.PREVIEW, binning=2)
        )
        final_hfd = mean_hfd(detect_stars(frame.data, bit_depth=frame.sensor.bit_depth)) or 0.0
        self.report("done", fraction=1.0, message=f"Focused at {best}, HFD {final_hfd:.2f}")
        return FocusResult(best_position=best, best_hfd=final_hfd, samples=samples)


def _fit_v_curve(samples: list[FocusSample]) -> int:
    """Intersect straight-line fits to each arm of the V.

    Robust to the noisy sample at the bottom, which is exactly where seeing
    does the most damage and where a naive minimum would be picked.
    """
    ordered = sorted(samples, key=lambda s: s.position)
    minimum = min(range(len(ordered)), key=lambda i: ordered[i].hfd)

    left = ordered[: minimum + 1]
    right = ordered[minimum:]
    if len(left) < 2 or len(right) < 2:
        return ordered[minimum].position

    left_slope, left_intercept = _linear_fit(left)
    right_slope, right_intercept = _linear_fit(right)
    if abs(left_slope - right_slope) < 1e-9:
        return ordered[minimum].position

    crossing = (right_intercept - left_intercept) / (left_slope - right_slope)
    # Never extrapolate outside the sampled range: a shallow fit on one arm
    # can otherwise place "best focus" somewhere never measured.
    low, high = ordered[0].position, ordered[-1].position
    return round(max(low, min(high, crossing)))


def _linear_fit(samples: list[FocusSample]) -> tuple[float, float]:
    n = len(samples)
    sum_x = sum(s.position for s in samples)
    sum_y = sum(s.hfd for s in samples)
    sum_xx = sum(s.position * s.position for s in samples)
    sum_xy = sum(s.position * s.hfd for s in samples)
    denominator = n * sum_xx - sum_x * sum_x
    if abs(denominator) < 1e-9:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    return slope, (sum_y - slope * sum_x) / n
