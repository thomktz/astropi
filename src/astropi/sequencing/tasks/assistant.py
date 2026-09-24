"""The guiding assistant, as a task.

Stops correcting and watches instead. It takes minutes, it drives the
mount when it measures backlash, and it is the sort of thing an operator
abandons halfway when cloud arrives - which is three reasons for it to be
a task rather than a service call.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from astropi.core.errors import AstropiError
from astropi.core.timekeeping import hour_angle_deg
from astropi.devices.mount import GuideDirection
from astropi.sequencing.task import Task
from astropi.services.guideassist import (
    AssistantReport,
    BacklashMeasurement,
    DriftSample,
    analyse,
)

if TYPE_CHECKING:
    from astropi.runtime import Observatory

#: Pulses per leg when measuring backlash, and how long each one runs.
BACKLASH_STEPS = 6
BACKLASH_PULSE_MS = 800


class GuidingAssistantTask(Task):
    kind = "guide_assistant"

    def __init__(
        self,
        observatory: Observatory,
        *,
        seconds: float = 120.0,
        measure_backlash: bool = True,
    ) -> None:
        super().__init__(name="Guiding assistant")
        self._observatory = observatory
        self._seconds = seconds
        self._measure_backlash = measure_backlash

    async def run(self) -> AssistantReport:
        guider = self._observatory.require_guider()
        mount = self._observatory.mount()

        status = await mount.status()
        if not status.tracking:
            raise AstropiError(
                "the mount is not tracking - the assistant measures what the sky does to a "
                "mount that is following it, not to one that is parked"
            )
        if str(guider.state) not in {"stopped", "lost", "error"}:
            raise AstropiError("stop guiding first: the assistant measures the uncorrected mount")

        pixel_scale = self._observatory.guide_pixel_scale_arcsec()
        self.report(
            "drifting",
            fraction=0.0,
            message=f"Watching the star drift for {self._seconds:.0f}s, correcting nothing",
            seconds=self._seconds,
            measuring_backlash=self._measure_backlash,
        )

        samples = await self._watch(guider, pixel_scale)
        if len(samples) < 6:
            raise AstropiError(
                f"only {len(samples)} usable frames - the star was lost too often to measure "
                "anything. Try a longer exposure or a brighter star."
            )

        backlash = None
        if self._measure_backlash:
            backlash = await self._measure_dec_backlash(guider, mount, pixel_scale)

        position = (await mount.status()).position
        report = analyse(
            samples,
            declination_deg=position.dec_deg,
            hour_angle_deg=hour_angle_deg(position.ra_deg, self._observatory.site.longitude_deg),
            backlash=backlash,
            current_min_move_arcsec=guider.config.min_move_arcsec,
        )
        self.report(
            "done",
            fraction=1.0,
            message=(
                f"Seeing {max(report.ra.seeing_rms_arcsec, report.dec.seeing_rms_arcsec):.2f}″, "
                f"declination drifting {report.dec.drift_arcsec_per_min:+.2f}″ a minute"
            ),
            **_report_payload(report),
        )
        return report

    async def _watch(self, guider, pixel_scale: float) -> list[DriftSample]:
        """Record where the star is, frame after frame, correcting nothing."""
        samples: list[DriftSample] = []
        origin: tuple[float, float] | None = None
        last: tuple[float, float] | None = None
        deadline = time.monotonic() + self._seconds
        lost = 0

        while time.monotonic() < deadline:
            # Followed, not re-picked: re-choosing the brightest star each
            # frame measures the distance between two different stars the
            # moment the brightest one changes.
            star = await guider.observe_star(last)
            if star is None:
                lost += 1
                self.report(
                    "drifting",
                    message=f"No star in that frame ({lost} so far)",
                    lost_frames=lost,
                )
                continue
            if origin is None:
                origin = (star.x, star.y)
            last = (star.x, star.y)

            # Sensor pixels, not mount axes: this runs before any
            # calibration exists, and the drift is the same size whichever
            # way round the camera is.
            samples.append(
                DriftSample(
                    timestamp=time.time(),
                    ra_arcsec=(star.x - origin[0]) * pixel_scale,
                    dec_arcsec=(star.y - origin[1]) * pixel_scale,
                )
            )
            elapsed = self._seconds - (deadline - time.monotonic())
            self.report(
                "drifting",
                fraction=min(0.85, 0.85 * elapsed / self._seconds),
                message=f"{len(samples)} frames, {elapsed:.0f}s of {self._seconds:.0f}s",
                frames=len(samples),
                elapsed_s=round(elapsed, 1),
                drift_x_arcsec=round(samples[-1].ra_arcsec, 2),
                drift_y_arcsec=round(samples[-1].dec_arcsec, 2),
            )

        return samples

    async def _measure_dec_backlash(self, guider, mount, pixel_scale: float) -> BacklashMeasurement | None:
        """Drive declination one way, then the other, and watch the gap.

        The first leg takes up whatever slack is there and then moves the
        axis; the second leg spends its beginning paying the slack back.
        The difference between what the two legs travelled is what the
        gears swallowed.
        """
        self.report(
            "backlash",
            fraction=0.9,
            message="Reversing declination to measure the backlash",
        )

        async def leg(direction: GuideDirection) -> float:
            before = await guider.observe_star()
            if before is None:
                return math.nan
            for _ in range(BACKLASH_STEPS):
                await mount.pulse_guide(direction, BACKLASH_PULSE_MS)
            after = await guider.observe_star((before.x, before.y), radius_px=400.0)
            if after is None:
                return math.nan
            return math.hypot(after.x - before.x, after.y - before.y) * pixel_scale

        # North twice: the first clears the slack, the second is a clean
        # measurement of what the pulses are worth.
        await leg(GuideDirection.NORTH)
        clean = await leg(GuideDirection.NORTH)
        reversed_leg = await leg(GuideDirection.SOUTH)
        await leg(GuideDirection.NORTH)  # put it back where it started

        if math.isnan(clean) or math.isnan(reversed_leg):
            self.report("backlash", message="Lost the star during the reversal; no backlash figure")
            return None

        lost = max(0.0, clean - reversed_leg)
        seconds_per_arcsec = (BACKLASH_STEPS * BACKLASH_PULSE_MS / 1000.0) / max(clean, 1e-6)
        return BacklashMeasurement(
            arcsec=round(lost, 2),
            milliseconds=round(lost * seconds_per_arcsec * 1000.0, 0),
            # One reversal, so the uncertainty is the frame-to-frame
            # wobble rather than a spread over repeats. Honest about
            # being a single measurement.
            uncertainty_arcsec=round(abs(clean - reversed_leg) * 0.25, 2),
        )


def _report_payload(report: AssistantReport) -> dict:
    """The report, flattened for the task detail the dashboard reads."""
    return {
        "frames": report.samples,
        "seconds": round(report.seconds, 1),
        "ra_drift_arcsec_per_min": round(report.ra.drift_arcsec_per_min, 3),
        "dec_drift_arcsec_per_min": round(report.dec.drift_arcsec_per_min, 3),
        "ra_seeing_arcsec": round(report.ra.seeing_rms_arcsec, 3),
        "dec_seeing_arcsec": round(report.dec.seeing_rms_arcsec, 3),
        "ra_peak_to_peak_arcsec": round(report.ra.peak_to_peak_arcsec, 2),
        "dec_peak_to_peak_arcsec": round(report.dec.peak_to_peak_arcsec, 2),
        "ra_max_rate_arcsec_per_min": round(report.ra.max_rate_arcsec_per_min, 2),
        "polar_error_arcmin": report.polar_error_arcmin,
        "polar_error_confidence": report.polar_error_confidence,
        "backlash_arcsec": None if report.backlash is None else report.backlash.arcsec,
        "backlash_ms": None if report.backlash is None else report.backlash.milliseconds,
        "recommendations": report.recommendations,
        "suggested_min_move_arcsec": report.suggested_min_move_arcsec,
        "suggested_dec_mode": report.suggested_dec_mode,
    }
