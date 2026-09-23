"""Autoguiding.

Implements the `Guider` protocol against any camera and any mount that
offers pulse guiding, so it works identically with the simulator and with
the ASI2600MC Duo's guide sensor driving the GTi.

The loop is deliberately conservative. Guiding corrections fight seeing as
much as tracking error, and a loop that chases every wobble injects more
motion than it removes; hence an aggressiveness below one, a minimum move
threshold, and a cap on how long any single pulse may be.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from astropi.core.errors import AstropiError
from astropi.core.events import EventBus, Topic
from astropi.devices.camera import Camera, ExposureRequest, Frame, FrameKind
from astropi.devices.guider import GuideCalibration, GuideSample, GuidingState, GuidingStatus
from astropi.devices.mount import GuideDirection, Mount
from astropi.services.stardetect import DetectedStar, detect_stars, nearest_star

logger = logging.getLogger(__name__)

#: How long to wait before looking again when the guide camera is busy.
IDLE_POLL_S = 0.4

#: How many detected stars are offered to the client as pickable candidates.
#: Enough to cover the usable ones; the faint tail is not worth guiding on.
CANDIDATE_LIMIT = 25


@dataclass(frozen=True, slots=True)
class GuideFrameInfo:
    """What the guide view needs to draw itself over the latest frame."""

    width: int
    height: int
    captured_at: float
    lock: tuple[float, float] | None
    star: tuple[float, float] | None
    search_radius_px: float
    candidates: list[tuple[float, float, float]]


class DecGuideMode(StrEnum):
    """Which way declination corrections are allowed to go.

    Declination has backlash: reversing direction is partly swallowed by
    the gear teeth before the axis moves at all. When polar misalignment
    drives a consistent drift one way, guiding only against that way never
    reverses and so never pays the backlash - which is why one-directional
    declination guiding is a standard option rather than a curiosity.
    """

    AUTO = "auto"
    NORTH = "north"
    SOUTH = "south"
    OFF = "off"


@dataclass(slots=True)
class GuidingConfig:
    exposure_s: float = 2.0
    gain: int = 250
    dec_mode: DecGuideMode = DecGuideMode.AUTO
    #: Fraction of the measured error corrected each cycle, per axis.
    ra_aggressiveness: float = 0.7
    dec_aggressiveness: float = 0.6
    #: Errors below this are seeing, not tracking error - leave them alone.
    min_move_arcsec: float = 0.15
    #: No single correction may exceed this, so one bad frame cannot bolt.
    max_pulse_ms: int = 1_000
    #: How far the star may move between frames before the lock is lost.
    search_radius_px: float = 25.0
    #: Automatic selection ignores this fraction of the frame at each
    #: edge. A star picked near the edge is one dither away from leaving
    #: the sensor, and drift over a long run walks it out too - at which
    #: point guiding stops with a lost star rather than a useful message.
    edge_margin: float = 0.10
    #: Consecutive failures tolerated before declaring the star lost.
    max_lost_frames: int = 5
    calibration_pulse_ms: int = 900
    calibration_steps: int = 5
    #: Guiding is "settled" once error stays under this for `settle_time_s`.
    settle_arcsec: float = 1.5
    settle_time_s: float = 8.0
    rms_window: int = 50
    #: Keep taking guide frames when the loop is not running, so the guide
    #: view is live rather than showing whatever was on the sensor when
    #: guiding last stopped. This is what makes picking a star, checking
    #: the guide focus and seeing cloud arrive possible before starting.
    #:
    #: Off until asked for, like the main live view: neither sensor should
    #: start exposing because a dashboard was opened.
    preview_enabled: bool = False
    #: Cadence of those idle frames, from the start of one to the next.
    #: Slower than guiding, which has a control loop to feed; this only
    #: has an eye to feed.
    preview_period_s: float = 6.0


class GuidingService:
    """The built-in guide loop."""

    def __init__(
        self,
        camera: Camera,
        mount: Mount,
        events: EventBus,
        config: GuidingConfig | None = None,
        *,
        pixel_scale_arcsec: float | None = None,
    ) -> None:
        self._camera = camera
        self._mount = mount
        self._events = events
        self._config = config or GuidingConfig()
        self._pixel_scale = pixel_scale_arcsec

        self._state = GuidingState.STOPPED
        self._calibration: GuideCalibration | None = None
        self._lock_position: tuple[float, float] | None = None
        self._samples: deque[GuideSample] = deque(maxlen=self._config.rms_window)
        self._task: asyncio.Task[None] | None = None
        self._lost_frames = 0
        self._settled_since: float | None = None

        # A single slot, not a growing store. The loop produces a frame
        # every couple of seconds and only the newest is ever wanted;
        # putting them in the imaging frame store would evict the light
        # frames within a minute.
        self._latest_frame: Frame | None = None
        self._latest_stars: list[DetectedStar] = []
        self._latest_star: DetectedStar | None = None

        # The idle loop, and the lock that keeps it off the sensor while
        # anything else is using it. One owner of the guide camera, so a
        # preview frame can never collide with a calibration pulse.
        self._preview_task: asyncio.Task[None] | None = None
        self._camera_lock = asyncio.Lock()

    # ---------------------------------------------------------------- status

    async def status(self) -> GuidingStatus:
        rms_ra = _rms(s.ra_error_arcsec for s in self._samples)
        rms_dec = _rms(s.dec_error_arcsec for s in self._samples)
        total = math.hypot(rms_ra, rms_dec) if self._samples else None
        return GuidingStatus(
            state=self._state,
            calibration=self._calibration,
            rms_ra_arcsec=rms_ra,
            rms_dec_arcsec=rms_dec,
            rms_total_arcsec=total,
            samples=len(self._samples),
        )

    @property
    def calibration(self) -> GuideCalibration | None:
        return self._calibration

    @property
    def latest_frame(self) -> Frame | None:
        return self._latest_frame

    def frame_info(self) -> GuideFrameInfo | None:
        """Everything needed to draw the guide view's overlays."""
        if self._latest_frame is None:
            return None
        height, width = self._latest_frame.shape
        return GuideFrameInfo(
            width=width,
            height=height,
            captured_at=self._latest_frame.started_at,
            lock=self._lock_position,
            star=None if self._latest_star is None else (self._latest_star.x, self._latest_star.y),
            search_radius_px=self._config.search_radius_px,
            candidates=[
                (star.x, star.y, star.snr)
                for star in self._latest_stars[:CANDIDATE_LIMIT]
            ],
        )

    async def preview(self) -> GuideFrameInfo:
        """Take a single guide frame without guiding.

        So the view works before the loop starts - which is when a star has
        to be chosen, and when it is worth checking the guide camera is
        focused and pointed at something.
        """
        if self._task is not None and not self._task.done():
            raise AstropiError("already guiding; the loop is producing frames")
        await self._expose_and_detect()
        info = self.frame_info()
        if info is None:  # pragma: no cover - expose always sets a frame
            raise AstropiError("no frame captured")
        return info

    def select_star(self, x: float, y: float, *, radius_px: float = 40.0) -> DetectedStar:
        """Lock onto the detected star nearest a point.

        The automatic choice is the brightest star, which is wrong often
        enough to matter: it may be saturated, have a close neighbour, or be
        about to leave the frame. This is the override.
        """
        star = nearest_star(self._latest_stars, x, y, radius_px=radius_px)
        if star is None:
            raise AstropiError(f"no star detected within {radius_px:.0f} px of ({x:.0f}, {y:.0f})")
        self._lock_position = (star.x, star.y)
        self._latest_star = star
        # The error is measured against the lock, so moving it invalidates
        # the settling history that was accumulating against the old one.
        self._settled_since = None
        self._samples.clear()
        return star

    @property
    def config(self) -> GuidingConfig:
        return self._config

    def update_config(self, **changes: object) -> GuidingConfig:
        """Change settings, including mid-run.

        The loop reads its configuration each cycle, so a new exposure or
        aggressiveness takes effect on the next frame rather than needing a
        restart - which matters when the thing you are trying to fix is the
        guiding that is happening right now.
        """
        for field, value in changes.items():
            if value is None:
                continue
            if not hasattr(self._config, field):
                raise AstropiError(f"unknown guiding setting {field!r}")
            setattr(self._config, field, value)
        return self._config

    def invalidate_calibration(self) -> None:
        """Drop the calibration after anything that changes the geometry.

        Rotating the camera, flipping the mount, or moving a long way in
        declination all break the pulse-to-pixel mapping; guiding on a stale
        calibration pushes the star the wrong way.
        """
        self._calibration = None

    # ----------------------------------------------------------- calibration

    def _progress(self, phase: str, **detail: object) -> None:
        """Say what the loop is doing, for anything watching it work.

        Calibration published one word - "calibrating" - and then, half a
        minute later, either a calibration or an error. Everything in
        between, which is where it goes wrong, was invisible.
        """
        self._events.publish(Topic.GUIDING_PROGRESS, phase=phase, **detail)

    async def _require_tracking(self, what: str) -> None:
        """Refuse to guide a mount that is not following the sky.

        Guiding corrects tracking; it cannot replace it. On a stopped
        mount the field walks out of frame at fifteen arcseconds a
        second while the loop answers with corrections of two, and what
        that looks like from the outside is a guide loop that has gone
        mad - a right ascension error climbing past twenty arcseconds
        with the pulse pinned to its ceiling.
        """
        status = await self._mount.status()
        if not status.tracking:
            raise AstropiError(
                f"cannot {what}: the mount is not tracking. Guiding corrects tracking "
                "rather than replacing it - start tracking, then guide."
            )

    async def calibrate(self) -> GuideCalibration:
        """Learn how mount pulses move the star on the sensor.

        Each axis is pulsed several times in one direction and the total
        displacement measured. Several small steps rather than one long one
        so that backlash and a single bad frame are both averaged down.
        """
        await self._require_tracking("calibrate")
        pixel_scale = self._require_pixel_scale()
        self._set_state(GuidingState.CALIBRATING)
        steps = self._config.calibration_steps
        try:
            self._progress(
                "acquiring",
                message="Looking for a star to calibrate on",
                exposure_s=self._config.exposure_s,
                pulses=steps,
                pulse_ms=self._config.calibration_pulse_ms,
            )
            star = await self._acquire_star()
            origin = (star.x, star.y)
            self._progress(
                "acquired",
                message=f"Calibrating on a star at {star.x:.0f}, {star.y:.0f}",
                star_x=star.x,
                star_y=star.y,
                snr=round(star.snr, 1),
                hfd=round(star.hfd, 2),
                candidates=len(self._latest_stars),
            )

            west = await self._calibration_leg(GuideDirection.WEST, origin, "west")
            # Walk back to the start before doing the other axis, so the
            # declination measurement is not taken from a displaced position.
            await self._pulse_sequence(GuideDirection.EAST, phase="east")
            north = await self._calibration_leg(GuideDirection.NORTH, origin, "north")
            await self._pulse_sequence(GuideDirection.SOUTH, phase="south")

            pulse_seconds = self._config.calibration_pulse_ms * self._config.calibration_steps / 1000.0
            ra_shift = math.hypot(*west)
            dec_shift = math.hypot(*north)
            if ra_shift < 2.0 or dec_shift < 2.0:
                self._progress(
                    "failed",
                    message=(
                        f"The star moved {ra_shift:.1f} px west and {dec_shift:.1f} px north - "
                        "too little to measure a rate from"
                    ),
                    ra_shift_px=round(ra_shift, 2),
                    dec_shift_px=round(dec_shift, 2),
                )
                raise AstropiError(
                    f"calibration moved the star only {ra_shift:.1f}/{dec_shift:.1f} px - "
                    "check the mount is unparked, tracking and accepting guide pulses"
                )

            status = await self._mount.status()
            calibration = GuideCalibration(
                ra_rate_arcsec_per_s=ra_shift * pixel_scale / pulse_seconds,
                dec_rate_arcsec_per_s=dec_shift * pixel_scale / pulse_seconds,
                # Angle between the sensor's x axis and the mount's RA axis.
                angle_deg=math.degrees(math.atan2(west[1], west[0])),
                pixel_scale_arcsec=pixel_scale,
                calibrated_at=time.time(),
                dec_at_calibration_deg=status.position.dec_deg,
                west_shift_px=(round(west[0], 2), round(west[1], 2)),
                north_shift_px=(round(north[0], 2), round(north[1], 2)),
            )
            self._calibration = calibration
            self._progress(
                "calibrated",
                message=(
                    f"{calibration.ra_rate_arcsec_per_s:.1f}\u2033/s in RA, "
                    f"{calibration.dec_rate_arcsec_per_s:.1f}\u2033/s in Dec, "
                    f"camera {calibration.angle_deg:.0f}\u00b0 from the mount's axes"
                ),
                ra_rate_arcsec_per_s=round(calibration.ra_rate_arcsec_per_s, 3),
                dec_rate_arcsec_per_s=round(calibration.dec_rate_arcsec_per_s, 3),
                angle_deg=round(calibration.angle_deg, 2),
                pixel_scale_arcsec=round(calibration.pixel_scale_arcsec, 3),
                ra_shift_px=round(ra_shift, 2),
                dec_shift_px=round(dec_shift, 2),
                west_shift_px=[round(west[0], 2), round(west[1], 2)],
                north_shift_px=[round(north[0], 2), round(north[1], 2)],
                # Positive when declination came out anticlockwise of
                # right ascension on the sensor, negative when the view
                # is mirrored. A guide loop that assumes one and gets the
                # other corrects declination the wrong way.
                handedness=round(
                    (west[0] * north[1] - west[1] * north[0]) / max(ra_shift * dec_shift, 1e-9), 3
                ),
            )
            self._set_state(GuidingState.STOPPED)
            return calibration
        except Exception as error:
            self._progress("failed", message=str(error))
            self._set_state(GuidingState.ERROR)
            raise

    async def _calibration_leg(
        self, direction: GuideDirection, origin: tuple[float, float], phase: str
    ) -> tuple[float, float]:
        await self._pulse_sequence(direction, phase=phase)
        star = await self._acquire_star(near=origin, radius_px=400.0)
        shift = (star.x - origin[0], star.y - origin[1])
        self._progress(
            f"{phase}_measured",
            message=(
                f"{math.hypot(*shift):.1f} px {direction} "
                f"({shift[0]:+.1f}, {shift[1]:+.1f})"
            ),
            direction=str(direction),
            shift_px=round(math.hypot(*shift), 2),
            star_x=star.x,
            star_y=star.y,
        )
        return shift

    async def _pulse_sequence(self, direction: GuideDirection, *, phase: str = "pulsing") -> None:
        steps = self._config.calibration_steps
        for index in range(steps):
            self._progress(
                phase,
                message=f"Pulse {index + 1} of {steps} {direction}",
                direction=str(direction),
                pulse=index + 1,
                pulses=steps,
            )
            await self._mount.pulse_guide(direction, self._config.calibration_pulse_ms)

    # ------------------------------------------------------------------ loop

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        await self._require_tracking("guide")
        if self._calibration is None:
            await self.calibrate()
        self._progress("locking", message="Choosing a star to guide on")
        star = await self._acquire_star()
        self._lock_position = (star.x, star.y)
        self._progress(
            "locked",
            message=f"Locked on a star at {star.x:.0f}, {star.y:.0f}",
            star_x=star.x,
            star_y=star.y,
            snr=round(star.snr, 1),
            hfd=round(star.hfd, 2),
        )
        self._samples.clear()
        self._lost_frames = 0
        self._settled_since = None
        self._set_state(GuidingState.SETTLING)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._set_state(GuidingState.STOPPED)

    # --------------------------------------------------------- idle preview

    @property
    def preview_running(self) -> bool:
        return self._preview_task is not None and not self._preview_task.done()

    async def start_preview(self) -> None:
        """Keep the guide view live while the loop is stopped.

        The guide sensor is otherwise dark until guiding starts, which is
        backwards: the moment you most need to see through it is before
        the loop runs - choosing a star, checking its focus, or working out
        why calibration failed.
        """
        if self.preview_running:
            return
        self._preview_task = asyncio.create_task(self._preview_run())

    async def stop_preview(self) -> None:
        task = self._preview_task
        self._preview_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _preview_run(self) -> None:
        while True:
            try:
                await self._preview_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never take the loop down for one bad frame: the sensor
                # may be busy calibrating, or a cable may have been moved.
                logger.exception("guide preview frame failed")
                await asyncio.sleep(IDLE_POLL_S)

    async def _preview_tick(self) -> None:
        # Guiding and calibrating both produce frames of their own, and
        # both have a claim on the sensor that a preview does not.
        guiding = self._task is not None and not self._task.done()
        if not self._config.preview_enabled or guiding or self._state is GuidingState.CALIBRATING:
            await asyncio.sleep(IDLE_POLL_S)
            return

        started = time.monotonic()
        await self._expose_and_detect()
        remaining = self._config.preview_period_s - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _run(self) -> None:
        try:
            while True:
                await self._guide_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guide loop failed")
            self._set_state(GuidingState.ERROR)

    async def _guide_once(self) -> None:
        calibration = self._calibration
        lock = self._lock_position
        if calibration is None or lock is None:
            raise AstropiError("guiding started without a calibration or lock position")

        stars = await self._expose_and_detect()
        star = nearest_star(stars, *lock, radius_px=self._config.search_radius_px)
        if star is None:
            self._lost_frames += 1
            if self._lost_frames >= self._config.max_lost_frames:
                self._set_state(GuidingState.LOST)
            # Said out loud. A frame with no star at the lock point
            # published nothing at all, so the loop went silent - no
            # sample, no graph, no change to any number on screen - and
            # from the outside that is indistinguishable from a guide
            # loop that has stopped running.
            self._progress(
                "searching",
                message=(
                    f"No star within {self._config.search_radius_px:.0f} px of the lock "
                    f"point ({self._lost_frames} of {self._config.max_lost_frames})"
                ),
                lost_frames=self._lost_frames,
                max_lost_frames=self._config.max_lost_frames,
                search_radius_px=self._config.search_radius_px,
                candidates=len(stars),
            )
            return
        self._lost_frames = 0
        self._latest_star = star

        dx = star.x - lock[0]
        dy = star.y - lock[1]
        # Rotate the sensor-space error onto the mount's own axes; the camera
        # is not aligned with them, and that angle is what calibration found.
        angle = math.radians(calibration.angle_deg)
        ra_px = dx * math.cos(angle) + dy * math.sin(angle)
        dec_px = -dx * math.sin(angle) + dy * math.cos(angle)

        ra_arcsec = ra_px * calibration.pixel_scale_arcsec
        dec_arcsec = dec_px * calibration.pixel_scale_arcsec

        ra_pulse = self._pulse_for(
            ra_arcsec, calibration.ra_rate_arcsec_per_s, self._config.ra_aggressiveness
        )
        dec_pulse = self._pulse_for(
            dec_arcsec, calibration.dec_rate_arcsec_per_s, self._config.dec_aggressiveness
        )

        # Push the star back toward the lock position: correct *against* the
        # measured error, hence the inverted directions.
        ra_direction = GuideDirection.EAST if ra_arcsec > 0 else GuideDirection.WEST
        ra_withheld = "" if ra_pulse else self._why_no_pulse(ra_arcsec)
        if ra_pulse:
            await self._mount.pulse_guide(ra_direction, ra_pulse)

        dec_direction = GuideDirection.SOUTH if dec_arcsec > 0 else GuideDirection.NORTH
        dec_withheld = ""
        if dec_pulse and self._dec_allowed(dec_direction):
            await self._mount.pulse_guide(dec_direction, dec_pulse)
        else:
            dec_withheld = (
                self._why_no_pulse(dec_arcsec)
                if not dec_pulse
                else f"declination guiding is set to {self._config.dec_mode}"
            )
            dec_pulse = 0

        sample = GuideSample(
            timestamp=time.time(),
            star_x=star.x,
            star_y=star.y,
            lock_x=lock[0],
            lock_y=lock[1],
            ra_error_px=ra_px,
            dec_error_px=dec_px,
            ra_error_arcsec=ra_arcsec,
            dec_error_arcsec=dec_arcsec,
            ra_pulse_ms=float(ra_pulse),
            dec_pulse_ms=float(dec_pulse),
            ra_direction=str(ra_direction) if ra_pulse else "",
            dec_direction=str(dec_direction) if dec_pulse else "",
            ra_withheld=ra_withheld,
            dec_withheld=dec_withheld,
            star_flux=star.flux,
            star_hfd=star.hfd,
            snr=star.snr,
        )
        self._samples.append(sample)
        self._update_settling(sample)
        self._events.publish(Topic.GUIDING_SAMPLE, **_sample_payload(sample))

    def _why_no_pulse(self, error_arcsec: float) -> str:
        """The reason a correction of zero was a decision, not a failure."""
        if abs(error_arcsec) < self._config.min_move_arcsec:
            return f"under the {self._config.min_move_arcsec:.2f}\u2033 dead band"
        return "no calibrated rate for this axis"

    def _dec_allowed(self, direction: GuideDirection) -> bool:
        """Whether a declination correction may go this way."""
        mode = self._config.dec_mode
        if mode is DecGuideMode.OFF:
            return False
        if mode is DecGuideMode.NORTH:
            return direction is GuideDirection.NORTH
        if mode is DecGuideMode.SOUTH:
            return direction is GuideDirection.SOUTH
        return True

    def _pulse_for(self, error_arcsec: float, rate_arcsec_per_s: float, aggressiveness: float) -> int:
        if abs(error_arcsec) < self._config.min_move_arcsec or rate_arcsec_per_s <= 0:
            return 0
        seconds = abs(error_arcsec) * aggressiveness / rate_arcsec_per_s
        return min(int(seconds * 1000), self._config.max_pulse_ms)

    def _update_settling(self, sample: GuideSample) -> None:
        error = math.hypot(sample.ra_error_arcsec, sample.dec_error_arcsec)
        if error > self._config.settle_arcsec:
            self._settled_since = None
            if self._state is GuidingState.GUIDING:
                self._set_state(GuidingState.SETTLING)
            self._report_settling(error, 0.0)
            return
        if self._settled_since is None:
            self._settled_since = time.time()
        held = time.time() - self._settled_since
        if held >= self._config.settle_time_s and self._state is not GuidingState.GUIDING:
            self._set_state(GuidingState.GUIDING)
            self._progress(
                "guiding",
                message=f"Settled within {self._config.settle_arcsec:.1f}\u2033; guiding",
                error_arcsec=round(error, 3),
                settle_arcsec=self._config.settle_arcsec,
                held_s=round(held, 1),
                settle_time_s=self._config.settle_time_s,
            )
            return
        self._report_settling(error, held)

    def _report_settling(self, error: float, held: float) -> None:
        """How close the star is holding, and for how long.

        Settling is a wait with two conditions and neither of them was on
        screen: the error has to stay under a threshold, and it has to do
        it for long enough. Watching a state word sit on "settling" says
        nothing about whether it is nearly there or nowhere near.
        """
        if self._state is GuidingState.GUIDING:
            return
        self._progress(
            "settling",
            message=f"Holding within {self._config.settle_arcsec:.1f}\u2033 for {held:.0f}s",
            error_arcsec=round(error, 3),
            settle_arcsec=self._config.settle_arcsec,
            held_s=round(held, 1),
            settle_time_s=self._config.settle_time_s,
        )

    # ---------------------------------------------------------------- dither

    async def dither(self, amount_px: float, *, settle_px: float = 1.5, settle_time_s: float = 10.0) -> None:
        """Shift the lock position, then wait for guiding to recover.

        Moving the lock point rather than pulsing the mount directly lets the
        existing loop do the work, and leaves the dither visible in the
        telemetry as an error spike the loop corrects.
        """
        if self._lock_position is None:
            raise AstropiError("cannot dither while not guiding")

        angle = (time.time() * 1000) % (2 * math.pi)
        self._lock_position = (
            self._lock_position[0] + amount_px * math.cos(angle),
            self._lock_position[1] + amount_px * math.sin(angle),
        )
        self._set_state(GuidingState.DITHERING)
        self._settled_since = None

        deadline = time.time() + max(settle_time_s * 6, 60.0)
        settle_arcsec = settle_px * (self._calibration.pixel_scale_arcsec if self._calibration else 1.0)
        settled_at: float | None = None
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if not self._samples:
                continue
            latest = self._samples[-1]
            error = math.hypot(latest.ra_error_arcsec, latest.dec_error_arcsec)
            if error <= settle_arcsec:
                settled_at = settled_at or time.time()
                if time.time() - settled_at >= settle_time_s:
                    self._set_state(GuidingState.GUIDING)
                    return
            else:
                settled_at = None
        # Time out into GUIDING rather than failing the whole imaging run:
        # a slightly unsettled frame is better than an aborted sequence.
        logger.warning("dither did not settle within the timeout; continuing")
        self._set_state(GuidingState.GUIDING)

    # ----------------------------------------------------------------- utils

    async def _expose_and_detect(self) -> list[DetectedStar]:
        async with self._camera_lock:
            frame = await self._camera.expose(
                ExposureRequest(
                    duration_s=self._config.exposure_s,
                    gain=self._config.gain,
                    kind=FrameKind.GUIDE,
                )
            )
        stars = await asyncio.to_thread(
            detect_stars, frame.data, max_stars=30, bit_depth=frame.sensor.bit_depth
        )
        self._latest_frame = frame
        self._latest_stars = stars
        return stars

    def _clear_of_edges(self, stars: list[DetectedStar]) -> list[DetectedStar]:
        """Stars far enough inside the frame to still be there later."""
        frame = self._latest_frame
        if frame is None:
            return stars
        height, width = frame.shape
        margin_x = width * self._config.edge_margin
        margin_y = height * self._config.edge_margin
        return [
            star
            for star in stars
            if margin_x <= star.x <= width - margin_x and margin_y <= star.y <= height - margin_y
        ]

    async def _acquire_star(
        self, *, near: tuple[float, float] | None = None, radius_px: float | None = None
    ) -> DetectedStar:
        stars = await self._expose_and_detect()
        if not stars:
            raise AstropiError("no guide star found - try a longer exposure or more gain")
        if near is None:
            # Automatic selection only. A manual pick is the operator's
            # choice and is honoured wherever they click.
            usable = self._clear_of_edges(stars)
            if not usable:
                percent = self._config.edge_margin * 100
                raise AstropiError(
                    f"found {len(stars)} star(s), but none clear of the outer {percent:.0f}% "
                    "of the frame - nudge the mount to bring one inward, or expose longer"
                )
            return usable[0]
        star = nearest_star(stars, *near, radius_px=radius_px or self._config.search_radius_px)
        if star is None:
            raise AstropiError("lost the guide star during calibration")
        return star

    def _require_pixel_scale(self) -> float:
        if self._pixel_scale is not None:
            return self._pixel_scale
        sensor = self._camera.sensor
        raise AstropiError(
            f"guide camera pixel scale is unknown for a {sensor.width}x{sensor.height} sensor; "
            "set it from the optical train or run a plate solve first"
        )

    def set_pixel_scale(self, pixel_scale_arcsec: float) -> None:
        self._pixel_scale = pixel_scale_arcsec

    def _set_state(self, state: GuidingState) -> None:
        self._state = state
        self._events.publish(Topic.GUIDING_STATE, state=str(state))


def _rms(values) -> float | None:
    collected = [v for v in values]
    if not collected:
        return None
    return math.sqrt(sum(v * v for v in collected) / len(collected))


def _sample_payload(sample: GuideSample) -> dict:
    return {
        "timestamp": sample.timestamp,
        # Where the star and the lock are on the sensor, so the guide view's
        # overlay moves with the same data the error graph is drawn from.
        "star_x": round(sample.star_x, 2),
        "star_y": round(sample.star_y, 2),
        "lock_x": round(sample.lock_x, 2),
        "lock_y": round(sample.lock_y, 2),
        "ra_error_arcsec": round(sample.ra_error_arcsec, 3),
        "dec_error_arcsec": round(sample.dec_error_arcsec, 3),
        "ra_pulse_ms": sample.ra_pulse_ms,
        "dec_pulse_ms": sample.dec_pulse_ms,
        "ra_direction": sample.ra_direction,
        "dec_direction": sample.dec_direction,
        "ra_withheld": sample.ra_withheld,
        "dec_withheld": sample.dec_withheld,
        "snr": round(sample.snr, 1),
        "hfd": round(sample.star_hfd, 2),
    }
