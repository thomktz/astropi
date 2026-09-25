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
import itertools
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
from astropi.services.guidemodel import AxisModel, DriftCanceller
from astropi.services.stardetect import DetectedStar, detect_stars, nearest_star

logger = logging.getLogger(__name__)

#: How far a star may have moved between two calibration pulses and
#: still be the same star. One pulse is a few pixels; this is generous.
CALIBRATION_SEARCH_PX = 80.0
#: Movement that says the gears have engaged and the axis is turning:
#: clear of centroid noise, well short of a full calibration step.
BACKLASH_CLEARED_PX = 1.5

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
    #: Pulses allowed to take up declination backlash before the north
    #: leg is measured. Generous: a worn small mount can need several.
    backlash_clear_pulses: int = 12
    #: Guiding is "settled" once error stays under this for `settle_time_s`.
    settle_arcsec: float = 1.5
    settle_time_s: float = 8.0
    rms_window: int = 50
    #: Before guiding, measure the drift with no corrections, cancel it,
    #: and re-measure until nothing is left - then walk the star back.
    #:
    #: Guiding that only ever reacts to position is always one exposure
    #: behind a steady drift. Measured with the loop off, the drift is
    #: just a slope; cancelled as a steady rate and re-measured, whatever
    #: the calibration got wrong shows up as what is left over and is
    #: taken out on the next round.
    null_drift: bool = True
    #: Each round measures until the drift is known this well...
    drift_precision_arcsec_per_min: float = 1.0
    #: ...but for at least this long, and no longer than that.
    drift_min_s: float = 30.0
    drift_max_s: float = 120.0
    #: Rounds of cancel-and-re-measure before settling for what is left.
    drift_rounds: int = 4
    #: Fraction of the error corrected per frame while walking back to
    #: the lock point after the drift is cancelled: gently, so the walk
    #: back does not itself set anything swinging.
    recentre_aggressiveness: float = 0.3
    #: Frames the unguided-drift readout is fitted over.
    model_window: int = 20
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
        #: Each axis's drift with the corrections taken back out, fitted
        #: from what the loop has seen. See `guidemodel`.
        self._ra_model = AxisModel(self._config.model_window)
        self._dec_model = AxisModel(self._config.model_window)
        #: The drift being cancelled, per axis, in arcsec per second -
        #: found by `_null_drift`, delivered as a pulse every frame.
        self._ra_rate = 0.0
        self._dec_rate = 0.0
        #: "measure" (no position corrections), "recentre", or "hold".
        self._mode = "hold"
        self._last_frame_at: float | None = None
        self._ra_carry = 0.0
        self._dec_carry = 0.0
        #: How much of the calibrated effect a pulse has, per axis, as the
        #: drift rounds measured it - or `None` if they did not need to.
        self._ra_response: float | None = None
        self._dec_response: float | None = None
        #: Frames of the drift round in progress: time, RA and Dec error.
        self._round: list[tuple[float, float, float]] = []

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
            ra_model=self._ra_model.fit(),
            dec_model=self._dec_model.fit(),
            cycle_s=self._cycle_seconds(),
            cancelling_arcsec_per_min=(self._ra_rate * 60, self._dec_rate * 60),
            response=(self._ra_response, self._dec_response),
        )

    @property
    def state(self) -> GuidingState:
        return self._state

    async def observe_star(
        self, near: tuple[float, float] | None = None, *, radius_px: float = 120.0
    ) -> DetectedStar | None:
        """One frame, one star, and no correction of any kind.

        What the guiding assistant watches with: the loop's eye without
        the loop. `near` follows the same star between frames as it
        drifts, rather than re-picking the brightest one and measuring
        the distance between two different stars.
        """
        stars = await self._expose_and_detect()
        if not stars:
            return None
        if near is not None:
            return nearest_star(stars, *near, radius_px=radius_px)
        usable = self._clear_of_edges(stars) or stars
        self._latest_star = usable[0]
        return usable[0]

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
            candidates=[(star.x, star.y, star.snr) for star in self._latest_stars[:CANDIDATE_LIMIT]],
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
        # A different star, or the same one with a new target: the running
        # history no longer lines up with the errors that will follow.
        self._clear_models()
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
            # Declination has not moved yet, so its gears may be resting
            # against the far side of their teeth: the first pulses north
            # only take up that slack and move nothing. Measured as part
            # of the leg, they read as a slow axis - or, two pulses in, as
            # one that is not moving at all.
            dec_origin = await self._clear_backlash(GuideDirection.NORTH, near=origin)
            north = await self._calibration_leg(GuideDirection.NORTH, dec_origin, "north")
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
            # What was learned was learned against the old calibration.
            self._clear_models()
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
        """Push the star one step at a time, looking after every push.

        A frame after each pulse rather than one at the end of the leg.
        It costs an exposure per step, and it buys three things: a track
        anyone can watch walk across the frame, a fit through five points
        instead of a line between two - which one bad frame can no longer
        ruin - and the ability to notice an axis that is not moving on
        the second pulse instead of the fifth.
        """
        track = await self._pulse_sequence(direction, origin, phase=phase)
        shift = _fit_step(track)
        steps = self._config.calibration_steps
        total = (shift[0] * steps, shift[1] * steps)
        self._progress(
            f"{phase}_measured",
            message=(f"{math.hypot(*total):.1f} px {direction} ({total[0]:+.1f}, {total[1]:+.1f})"),
            direction=str(direction),
            shift_px=round(math.hypot(*total), 2),
            star_x=track[-1][0],
            star_y=track[-1][1],
            track=[[round(x, 1), round(y, 1)] for x, y in track],
            **self._frame_size(),
        )
        return total

    async def _clear_backlash(
        self, direction: GuideDirection, *, near: tuple[float, float]
    ) -> tuple[float, float]:
        """Pulse until the star starts moving, and return where it now is.

        The number of pulses it took is the backlash, and is reported:
        it is the same slack every reversing correction pays later.
        """
        star = await self._acquire_star(near=near, radius_px=CALIBRATION_SEARCH_PX)
        start = (star.x, star.y)
        limit = self._config.backlash_clear_pulses
        for index in range(limit):
            await self._mount.pulse_guide(direction, self._config.calibration_pulse_ms)
            star = await self._acquire_star(near=(star.x, star.y), radius_px=CALIBRATION_SEARCH_PX)
            moved = math.hypot(star.x - start[0], star.y - start[1])
            self._progress(
                "backlash",
                message=f"Taking up {direction} backlash: {moved:.1f} px after {index + 1} pulses",
                direction=str(direction),
                pulse=index + 1,
                pulses=limit,
                moved_px=round(moved, 2),
                backlash_ms=(index + 1) * self._config.calibration_pulse_ms,
                star_x=round(star.x, 1),
                star_y=round(star.y, 1),
                track=[[round(start[0], 1), round(start[1], 1)], [round(star.x, 1), round(star.y, 1)]],
                **self._frame_size(),
            )
            if moved >= BACKLASH_CLEARED_PX:
                return (star.x, star.y)
        raise AstropiError(
            f"the star has not moved after {limit} pulses {direction} - "
            "check the mount is unparked, tracking and accepting guide commands"
        )

    async def _pulse_sequence(
        self,
        direction: GuideDirection,
        origin: tuple[float, float] | None = None,
        *,
        phase: str = "pulsing",
    ) -> list[tuple[float, float]]:
        steps = self._config.calibration_steps
        track: list[tuple[float, float]] = []
        if origin is not None:
            track.append(origin)

        for index in range(steps):
            self._progress(
                phase,
                message=f"Pulse {index + 1} of {steps} {direction}",
                direction=str(direction),
                pulse=index + 1,
                pulses=steps,
                track=[[round(x, 1), round(y, 1)] for x, y in track],
                **self._frame_size(),
            )
            await self._mount.pulse_guide(direction, self._config.calibration_pulse_ms)
            if origin is None:
                # A return leg: it only has to get back, and nobody is
                # measuring it, so it does not pay for a frame per pulse.
                continue

            star = await self._acquire_star(near=track[-1], radius_px=CALIBRATION_SEARCH_PX)
            track.append((star.x, star.y))
            moved = math.hypot(star.x - track[0][0], star.y - track[0][1])
            self._progress(
                phase,
                message=f"Pulse {index + 1} of {steps} {direction}, star has moved {moved:.1f} px",
                direction=str(direction),
                pulse=index + 1,
                pulses=steps,
                star_x=round(star.x, 1),
                star_y=round(star.y, 1),
                moved_px=round(moved, 2),
                track=[[round(x, 1), round(y, 1)] for x, y in track],
                **self._frame_size(),
            )
            # Two pulses in and nothing has moved: the axis is not
            # turning, and three more pulses will not change that.
            if index >= 1 and moved < 0.5:
                raise AstropiError(
                    f"the star has not moved after {index + 1} pulses {direction} - "
                    "check the mount is unparked, tracking and accepting guide commands"
                )

        return track

    def _frame_size(self) -> dict:
        """The sensor's dimensions, so a client can place the track on it."""
        if self._latest_frame is None:
            return {}
        height, width = self._latest_frame.shape
        return {"frame_width": width, "frame_height": height}

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
        self._clear_models()
        self._lost_frames = 0
        self._settled_since = None
        self._ra_rate = 0.0
        self._dec_rate = 0.0
        self._last_frame_at = None
        self._ra_carry = self._dec_carry = 0.0
        self._ra_response = self._dec_response = None
        self._mode = "measure" if self._config.null_drift else "hold"
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
            if self._mode == "measure":
                await self._null_drift()
            while True:
                await self._guide_once()
                if self._mode == "recentre" and self._samples:
                    latest = self._samples[-1]
                    if (
                        math.hypot(latest.ra_error_arcsec, latest.dec_error_arcsec)
                        < self._config.settle_arcsec
                    ):
                        self._mode = "hold"
                        # The RMS is of guiding, not of the drift measured
                        # before it started.
                        self._samples.clear()
                        self._progress("holding", message="Back on the lock point; guiding")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guide loop failed")
            self._set_state(GuidingState.ERROR)

    async def _null_drift(self) -> None:
        """Measure the drift, cancel it, re-measure until nothing is left.

        No position corrections at all while this runs: the star is left
        to wander so that its motion is the drift and nothing else. Each
        round measures the slope of the error over time on both axes; the
        slope is added to the rate being cancelled; the next round
        measures what is left. A wrong calibration only makes the
        cancelling pulses the wrong size, which the next round sees as
        drift left over and takes out - so it converges regardless.
        """
        config = self._config
        tolerance = config.drift_precision_arcsec_per_min / 60.0
        ra_axis = DriftCanceller(tolerance)
        dec_axis = DriftCanceller(tolerance)
        history: list[dict] = []
        for index in range(config.drift_rounds):
            self._mode = "measure"
            self._round = []
            started = time.time()
            ra = dec = None
            while True:
                await self._guide_once()
                elapsed = time.time() - started
                ra = fit_line(self._round, 1)
                dec = fit_line(self._round, 2)
                self._progress(
                    "drift_measuring",
                    message=f"Round {index + 1}: measuring drift, no corrections",
                    round=index + 1,
                    rounds=config.drift_rounds,
                    elapsed_s=round(elapsed, 1),
                    min_s=config.drift_min_s,
                    max_s=config.drift_max_s,
                    frames=len(self._round),
                    history=history,
                    **_drift_detail(ra, dec, self._ra_rate, self._dec_rate),
                )
                known = ra is not None and dec is not None and ra[1] < tolerance and dec[1] < tolerance
                if elapsed >= config.drift_max_s or (elapsed >= config.drift_min_s and known):
                    break

            if ra is None or dec is None:
                break
            # Left over, per axis: significant if it is both bigger than
            # the tolerance and bigger than what it could be by chance.
            cancelled = _drift_detail(ra, dec, self._ra_rate, self._dec_rate)
            ra_left = ra_axis.update(*ra)
            dec_left = dec_axis.update(*dec) and config.dec_mode is not DecGuideMode.OFF
            self._ra_rate = ra_axis.rate
            if config.dec_mode is not DecGuideMode.OFF:
                self._dec_rate = dec_axis.rate
            self._ra_response = ra_axis.response if ra_axis.response_measured else None
            self._dec_response = dec_axis.response if dec_axis.response_measured else None
            # The round as measured: what was left over while cancelling
            # what it was cancelling, before this round's update.
            history.append({"round": index + 1, **cancelled})
            self._progress(
                "drift_measured",
                message=(
                    f"Round {index + 1}: RA {ra[0] * 60:+.1f}, Dec {dec[0] * 60:+.1f}\u2033/min "
                    + (
                        "left over"
                        if ra_left or dec_left
                        else "- drift cancelled"
                        + "".join(
                            f"; {name} unsteady, averaged"
                            for name, axis in (("RA", ra_axis), ("Dec", dec_axis))
                            if axis.unsteady
                        )
                    )
                ),
                round=index + 1,
                rounds=config.drift_rounds,
                converged=not (ra_left or dec_left),
                history=history,
                # How much of the calibrated effect a pulse turned out to
                # have, from comparing rounds; absent until measured.
                ra_unsteady=ra_axis.unsteady,
                dec_unsteady=dec_axis.unsteady,
                ra_response=round(ra_axis.response, 2) if ra_axis.response_measured else None,
                dec_response=round(dec_axis.response, 2) if dec_axis.response_measured else None,
                **_drift_detail(ra, dec, self._ra_rate, self._dec_rate),
            )
            if not (ra_left or dec_left):
                break

        self._mode = "recentre"
        self._round = []
        self._progress(
            "recentring",
            message="Walking the star back to the lock point",
            history=history,
            ra_response=None if self._ra_response is None else round(self._ra_response, 2),
            dec_response=None if self._dec_response is None else round(self._dec_response, 2),
            **_drift_detail(None, None, self._ra_rate, self._dec_rate),
        )

    async def _guide_once(self) -> None:
        calibration = self._calibration
        lock = self._lock_position
        if calibration is None or lock is None:
            raise AstropiError("guiding started without a calibration or lock position")

        stars = await self._expose_and_detect()
        # While the drift is measured the star is left to wander away from
        # the lock point, so it is looked for where it was last seen.
        near = lock
        if self._mode != "hold" and self._latest_star is not None:
            near = (self._latest_star.x, self._latest_star.y)
        star = nearest_star(stars, *near, radius_px=self._config.search_radius_px)
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
        ra_px, dec_px = _rotate(calibration, dx, dy)
        ra_arcsec = ra_px * calibration.pixel_scale_arcsec
        dec_arcsec = dec_px * calibration.pixel_scale_arcsec

        now = time.time()
        self._ra_model.observe(now, ra_arcsec)
        self._dec_model.observe(now, dec_arcsec)
        ra_fit = self._ra_model.fit()
        dec_fit = self._dec_model.fit()
        if self._mode == "measure":
            self._round.append((now, ra_arcsec, dec_arcsec))

        # The drift being cancelled, as a pulse sized to the time since the
        # last frame - the rate, sliced up. The GTi cannot run declination
        # continuously slower than about 25"/min, so a steady rate of a
        # few arcsec a minute can only be delivered this way anyway.
        elapsed = 0.0 if self._last_frame_at is None else min(now - self._last_frame_at, 30.0)
        self._last_frame_at = now
        # Plus whatever the previous frame's pulse could not carry: pulses
        # are whole milliseconds, and a small rate sliced finely enough
        # would otherwise be rounded away to nothing, every frame.
        ra_cancel = self._ra_rate * elapsed + self._ra_carry
        dec_cancel = self._dec_rate * elapsed + self._dec_carry

        # And part of the position error - none while the drift is being
        # measured, gently while walking back, fully once there. The dead
        # band only gates that position part: errors under it are seeing.
        # The drift part is a rate and is always delivered.
        ra_share, dec_share = self._position_share()
        ra_position = self._position_part(ra_arcsec, ra_share)
        dec_position = self._position_part(dec_arcsec, dec_share)
        ra_push = ra_position + ra_cancel
        dec_push = dec_position + dec_cancel
        # Declination never reverses against a drift it is cancelling.
        # A star past the lock on the far side comes back on its own, by
        # the drift itself, within a frame or two; a reversing pulse
        # instead has to take up the gear backlash first - twice, going
        # and coming back - and everything sent meanwhile, the steady
        # cancelling included, is swallowed by it.
        dec_waiting = (
            self._config.dec_mode is DecGuideMode.AUTO
            and self._dec_rate != 0
            and dec_push * self._dec_rate < 0
        )
        if dec_waiting:
            dec_push = 0.0
        ra_pulse = self._pulse_for(ra_push, calibration.ra_rate_arcsec_per_s)
        dec_pulse = self._pulse_for(dec_push, calibration.dec_rate_arcsec_per_s)

        # Push the star back toward the lock position: correct *against* the
        # error, hence the inverted directions.
        ra_direction = GuideDirection.EAST if ra_push > 0 else GuideDirection.WEST
        ra_withheld = "" if ra_pulse else self._why_no_pulse(ra_arcsec)
        if ra_pulse:
            await self._mount.pulse_guide(ra_direction, ra_pulse)

        dec_direction = GuideDirection.SOUTH if dec_push > 0 else GuideDirection.NORTH
        dec_withheld = ""
        dec_refused = bool(dec_pulse) and not self._dec_allowed(dec_direction)
        if dec_pulse and not dec_refused:
            await self._mount.pulse_guide(dec_direction, dec_pulse)
        else:
            dec_withheld = (
                "past the lock against the drift - letting the drift bring it back"
                if dec_waiting
                else self._why_no_pulse(dec_arcsec)
                if not dec_pulse
                else f"declination guiding is set to {self._config.dec_mode}"
            )
            dec_pulse = 0

        ra_predicted = self._predicted(ra_direction, ra_pulse, calibration.ra_rate_arcsec_per_s)
        dec_predicted = self._predicted(dec_direction, dec_pulse, calibration.dec_rate_arcsec_per_s)
        self._ra_model.pushed(ra_predicted)
        self._dec_model.pushed(dec_predicted)
        # A pulse of the push's own sign delivers minus its predicted
        # change in error. When a position correction was in it, rounding
        # is already fed back by the next frame's error; carrying it too
        # would count it twice. And declination refused by the guide mode
        # is not owed later.
        self._ra_carry = ra_push + ra_predicted if ra_position == 0 else 0.0
        self._dec_carry = dec_push + dec_predicted if dec_position == 0 and not dec_refused else 0.0

        sample = GuideSample(
            timestamp=now,
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
            ra_drift_arcsec_per_min=None if ra_fit is None else ra_fit.drift_arcsec_per_s * 60,
            dec_drift_arcsec_per_min=None if dec_fit is None else dec_fit.drift_arcsec_per_s * 60,
            ra_predicted_arcsec=ra_predicted,
            dec_predicted_arcsec=dec_predicted,
            mode=self._mode,
            star_flux=star.flux,
            star_hfd=star.hfd,
            snr=star.snr,
        )
        self._samples.append(sample)
        if self._mode == "hold":
            self._update_settling(sample)
        self._events.publish(Topic.GUIDING_SAMPLE, **_sample_payload(sample))

    def _why_no_pulse(self, error_arcsec: float) -> str:
        """The reason a correction of zero was a decision, not a failure."""
        if self._mode == "measure":
            return "measuring drift - no position corrections"
        if abs(error_arcsec) < self._config.min_move_arcsec:
            return f"under the {self._config.min_move_arcsec:.2f}\u2033 dead band"
        return "no calibrated rate for this axis"

    @staticmethod
    def _predicted(direction: GuideDirection, pulse_ms: float, rate: float) -> float:
        """The change in error a pulse should cause, per the calibration.

        Pushing east or south moves a positive error back towards zero,
        so those predict the error going down.
        """
        magnitude = pulse_ms / 1000.0 * rate
        pushes_back = direction in (GuideDirection.EAST, GuideDirection.SOUTH)
        return -magnitude if pushes_back else magnitude

    def _clear_models(self) -> None:
        self._ra_model.clear()
        self._dec_model.clear()

    def _position_share(self) -> tuple[float, float]:
        """Fraction of the position error corrected this frame, per axis."""
        if self._mode == "measure":
            return 0.0, 0.0
        if self._mode == "recentre":
            gentle = self._config.recentre_aggressiveness
            return (
                min(gentle, self._config.ra_aggressiveness),
                min(gentle, self._config.dec_aggressiveness),
            )
        return self._config.ra_aggressiveness, self._config.dec_aggressiveness

    def _cycle_seconds(self) -> float | None:
        """Median time between guide frames, over the recent run."""
        stamps = [s.timestamp for s in list(self._samples)[-11:]]
        gaps = sorted(b - a for a, b in itertools.pairwise(stamps) if b > a)
        return gaps[len(gaps) // 2] if gaps else None

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

    def _position_part(self, error_arcsec: float, share: float) -> float:
        if abs(error_arcsec) < self._config.min_move_arcsec:
            return 0.0
        return error_arcsec * share

    def _pulse_for(self, push_arcsec: float, rate_arcsec_per_s: float) -> int:
        if rate_arcsec_per_s <= 0:
            return 0
        seconds = abs(push_arcsec) / rate_arcsec_per_s
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
        shift = (amount_px * math.cos(angle), amount_px * math.sin(angle))
        self._lock_position = (self._lock_position[0] + shift[0], self._lock_position[1] + shift[1])
        self._set_state(GuidingState.DITHERING)
        self._settled_since = None
        # The error jumps by the dither, which no drift or pulse caused.
        if self._calibration is not None:
            ra_shift, dec_shift = _to_axes(self._calibration, *shift)
            self._ra_model.move_reference(ra_shift)
            self._dec_model.move_reference(dec_shift)

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
        "mode": sample.mode,
        "ra_drift_arcsec_per_min": _round3(sample.ra_drift_arcsec_per_min),
        "dec_drift_arcsec_per_min": _round3(sample.dec_drift_arcsec_per_min),
        "ra_predicted_arcsec": round(sample.ra_predicted_arcsec, 3),
        "dec_predicted_arcsec": round(sample.dec_predicted_arcsec, 3),
        "snr": round(sample.snr, 1),
        "hfd": round(sample.star_hfd, 2),
    }


def _fit_step(track: list[tuple[float, float]]) -> tuple[float, float]:
    """Pixels moved per pulse, fitted through every point on the track.

    A line between the first and last point throws away the middle and
    trusts two frames completely. The fit uses all of them, so one bad
    centroid moves the answer by a fifth of its error rather than all of
    it.
    """
    if len(track) < 2:
        return (0.0, 0.0)
    indices = list(range(len(track)))
    mean_index = sum(indices) / len(indices)
    bottom = sum((index - mean_index) ** 2 for index in indices)
    if bottom <= 0:
        return (0.0, 0.0)

    def slope(values: list[float]) -> float:
        mean = sum(values) / len(values)
        return sum((i - mean_index) * (v - mean) for i, v in zip(indices, values, strict=True)) / bottom

    return (slope([point[0] for point in track]), slope([point[1] for point in track]))


def _round3(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _rotate(calibration: GuideCalibration, dx: float, dy: float) -> tuple[float, float]:
    """Sensor pixels onto the mount's axes, still in pixels."""
    angle = math.radians(calibration.angle_deg)
    return (
        dx * math.cos(angle) + dy * math.sin(angle),
        -dx * math.sin(angle) + dy * math.cos(angle),
    )


def _to_axes(calibration: GuideCalibration, dx: float, dy: float) -> tuple[float, float]:
    """Sensor pixels onto the mount's axes, in arcseconds."""
    ra_px, dec_px = _rotate(calibration, dx, dy)
    return ra_px * calibration.pixel_scale_arcsec, dec_px * calibration.pixel_scale_arcsec


def fit_line(rows: list[tuple[float, float, float]], column: int) -> tuple[float, float] | None:
    """Slope of one column against time, and its standard error.

    Measured with no position corrections, so this is simply the drift -
    no correction has been mixed into it that would need taking out.
    """
    if len(rows) < 4:
        return None
    t = [r[0] for r in rows]
    y = [r[column] for r in rows]
    n = len(rows)
    t_mean = sum(t) / n
    y_mean = sum(y) / n
    stt = sum((v - t_mean) ** 2 for v in t)
    if stt <= 0:
        return None
    slope = sum((a - t_mean) * (b - y_mean) for a, b in zip(t, y, strict=True)) / stt
    residual = sum((b - y_mean - slope * (a - t_mean)) ** 2 for a, b in zip(t, y, strict=True))
    return slope, math.sqrt(residual / (n - 2) / stt)


def _drift_detail(
    ra: tuple[float, float] | None,
    dec: tuple[float, float] | None,
    ra_rate: float,
    dec_rate: float,
) -> dict:
    """Drift figures for a progress report, in arcsec per minute."""
    detail = {
        "ra_cancelling": round(ra_rate * 60, 2),
        "dec_cancelling": round(dec_rate * 60, 2),
    }
    if ra is not None:
        detail["ra_drift"] = round(ra[0] * 60, 2)
        detail["ra_drift_error"] = round(ra[1] * 60, 2)
    if dec is not None:
        detail["dec_drift"] = round(dec[0] * 60, 2)
        detail["dec_drift_error"] = round(dec[1] * 60, 2)
    return detail
