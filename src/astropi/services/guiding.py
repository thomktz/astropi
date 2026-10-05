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
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from astropi.core.errors import AstropiError, DeviceError
from astropi.core.events import EventBus, Topic
from astropi.devices.base import Capability
from astropi.devices.camera import Camera, ExposureRequest, Frame, FrameKind
from astropi.devices.guider import GuideCalibration, GuideSample, GuidingState, GuidingStatus
from astropi.devices.mount import GuideDirection, Mount
from astropi.services.guidemodel import PixelFit, PixelModel, Response
from astropi.services.stardetect import DetectedStar, detect_stars, nearest_star

logger = logging.getLogger(__name__)

#: How far a star may have moved between two calibration pulses and
#: still be the same star. One pulse is a few pixels; this is generous.
CALIBRATION_SEARCH_PX = 80.0
#: Movement that says the gears have engaged and the axis is turning:
#: clear of centroid noise, well short of a full calibration step.
BACKLASH_CLEARED_PX = 1.5

#: A calibration is corrected for the drift guiding measures only if it is
#: this recent and the mount still points within this far of where it was
#: made: drift from polar misalignment changes across the sky.
CALIBRATION_DRIFT_MAX_AGE_S = 1200.0
CALIBRATION_SAME_SKY_DEG = 1.0
#: The most a drift correction may change a calibration leg by, as a
#: fraction of it. More is not drift; it is something wrong with one or
#: the other, and the calibration is left as it was.
CALIBRATION_DRIFT_MAX_CHANGE = 0.3

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
    #: Fraction of the measured error corrected each cycle, per mount axis.
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
    #: Each calibration leg makes at least this many moves...
    calibration_steps: int = 5
    #: ...and keeps going until the star has moved this far, so a slow
    #: axis - right ascension near the pole moves the star at the cosine
    #: of the declination - is measured over a real distance rather than
    #: a pixel or two of it...
    calibration_distance_px: float = 15.0
    #: ...but no more than this many.
    calibration_max_steps: int = 30
    #: Pulses allowed to take up declination backlash before the north
    #: leg is measured. Generous: a worn small mount can need several.
    backlash_clear_pulses: int = 12
    #: Guiding is "settled" once error stays under this for `settle_time_s`.
    settle_arcsec: float = 1.5
    settle_time_s: float = 8.0
    rms_window: int = 50
    #: Cancel the drift, not only the position error.
    #:
    #: Guiding that only reacts to position is always one exposure behind
    #: a steady drift. The drift is fitted in sensor pixels over the last
    #: `model_window_s` seconds (see `guidemodel.PixelModel`): measured with
    #: no corrections at all until it is known well enough, then
    #: cancelled - refitted every frame from then on - while the star is
    #: walked gently back to the lock point.
    null_drift: bool = True
    #: Measure until the drift is known this well on both axes...
    drift_precision_arcsec_per_min: float = 1.0
    #: ...but for at least this long, and no longer than that.
    drift_min_s: float = 120.0
    drift_max_s: float = 300.0
    #: Guide with the mount's finer controls where it has them: right
    #: ascension by a tracking-rate offset held until the next frame, and
    #: declination by whole motor steps - instead of timed pulses, whose
    #: real effect at tens of milliseconds is set by serial round trips
    #: and motor ramps rather than by the milliseconds asked for.
    fine_guiding: bool = True
    #: Largest RA rate offset guiding may hold, in axis arcsec/s.
    max_ra_offset_arcsec_per_s: float = 7.5
    #: Most declination steps one frame may send.
    max_dec_steps: int = 40
    #: RA rate offset used to calibrate, held for `calibration_pulse_ms`
    #: per step; declination steps are sized to move about as far.
    calibration_ra_offset_arcsec_per_s: float = 7.5
    #: Seconds of frames the drift and the corrections' effect are fitted
    #: over. About one turn of the RA worm, so the worm's swing - fitted
    #: alongside the drift when its period is known - is seen whole.
    model_window_s: float = 480.0
    #: Fit the RA worm's periodic error as a sinusoid, and cancel it as it
    #: comes rather than chase it once it has arrived. Unmodelled, it is
    #: most of what an 8-minute window shows in RA: a straight line
    #: through a sinusoid tilts with the phase it starts at, so the drift
    #: wandered with the worm by up to 0.7 px/min and the star swung
    #: with it. In simulation, 0.99 px RMS became 0.14 px.
    fit_worm: bool = True
    #: The worm's period, in seconds. `None` asks the mount.
    worm_period_s: float | None = None
    #: How large a worm swing is believable before the frames show it,
    #: in arcsec on the sky: the width of the prior holding its size at
    #: zero. Loose; it only stops a short window trading drift for worm.
    worm_prior_arcsec: float = 10.0
    #: How firmly the fitted corrections' effect is held at the
    #: calibration, as a fraction of its size: the window moves it only as
    #: far as its own evidence shows it should.
    response_prior_fraction: float = 0.2
    #: Fraction of the error corrected per frame while walking back to
    #: the lock point once the drift is being cancelled: gently, so the
    #: walk back does not itself set anything swinging.
    recentre_aggressiveness: float = 0.3
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
        worm_period_s: Callable[[], float | None] | None = None,
    ) -> None:
        self._camera = camera
        self._mount = mount
        self._events = events
        self._config = config or GuidingConfig()
        self._pixel_scale = pixel_scale_arcsec
        #: Where the worm's period comes from, when the configuration does
        #: not say: by default the mount, which knows its own gearing.
        self._worm_period_source = worm_period_s or (lambda: getattr(mount, "worm_period_s", None))

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
        #: Drift and the corrections' effect, in sensor pixels, over the
        #: last frames; built from the calibration when guiding starts.
        self._model: PixelModel | None = None
        self._fit: PixelFit | None = None
        #: "measure" (no corrections at all), "recentre", or "hold".
        self._mode = "hold"
        self._last_frame_at: float | None = None
        #: Corrections delivered since the last frame, in calibration units.
        self._pending_ra = 0.0
        self._pending_dec = 0.0
        #: The RA rate offset the mount is holding for guiding, axis "/s.
        self._ra_offset = 0.0
        #: Whether the calibration under way uses the fine controls.
        self._calibrating_fine = False
        #: Steps asked for and counted, on the last declination step move.
        self._last_steps: tuple[int, int] | None = None
        #: When `_acquire_star` last found its star, Unix seconds.
        self._last_star_at = 0.0

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
            cycle_s=self._cycle_seconds(),
            model=_model_out(
                self._fit, self._calibration, self.worm_period_s() if self._config.fit_worm else None
            ),
            cancelling=self._cancelling(),
            ra_rate_offset=self._ra_offset,
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
        usable = [star for star in usable if not star.saturated] or usable
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
        # The model is built once per run; these reach into it.
        model, calibration = self._model, self._calibration
        if model is not None:
            model.window_s = self._config.model_window_s
            model.worm_period_s = self.worm_period_s() if self._config.fit_worm else None
            if calibration is not None:
                model.worm_prior_px = self._config.worm_prior_arcsec / calibration.pixel_scale_arcsec
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
        # Calibrated with the controls guiding will use - a rate offset
        # and motor steps where the mount has them - because what a 900 ms
        # pulse does says little about what a 20 ms one does.
        self._calibrating_fine = self._fine_available()
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

            west, west_moves, west_at = await self._calibration_leg(GuideDirection.WEST, origin, "west")
            # Walk back to the start before doing the other axis, so the
            # declination measurement is not taken from a displaced position.
            await self._pulse_sequence(GuideDirection.EAST, phase="east", count=west_moves)
            # Declination has not moved yet, so its gears may be resting
            # against the far side of their teeth: the first pulses north
            # only take up that slack and move nothing. Measured as part
            # of the leg, they read as a slow axis - or, two pulses in, as
            # one that is not moving at all.
            dec_origin = await self._clear_backlash(GuideDirection.NORTH, near=origin)
            north, north_moves, north_at = await self._calibration_leg(
                GuideDirection.NORTH, dec_origin, "north"
            )
            await self._pulse_sequence(GuideDirection.SOUTH, phase="south", count=north_moves)

            ra_seconds = self._config.calibration_pulse_ms * west_moves / 1000.0
            dec_seconds = self._config.calibration_pulse_ms * north_moves / 1000.0
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
            angle = math.atan2(west[1], west[0])
            # Which way north took the star, along the declination axis
            # the rotation assumes. Negative when the image is mirrored.
            north_along = -north[0] * math.sin(angle) + north[1] * math.cos(angle)
            ra_rate = ra_shift * pixel_scale / ra_seconds
            fine = self._calibrating_fine
            dec_steps = self._calibration_dec_steps() * north_moves
            # Per unit of what each leg sent: axis arcsec and steps with the
            # fine controls, milliseconds of pulse without.
            ra_units = (
                self._config.calibration_ra_offset_arcsec_per_s * ra_seconds
                if fine
                else self._config.calibration_pulse_ms * west_moves
            )
            dec_units = dec_steps if fine else self._config.calibration_pulse_ms * north_moves
            calibration = GuideCalibration(
                ra_response_px=(west[0] / ra_units, west[1] / ra_units),
                dec_response_px=(north[0] / max(dec_units, 1), north[1] / max(dec_units, 1)),
                ra_unit="arcsec" if fine else "ms",
                dec_unit="step" if fine else "ms",
                ra_rate_arcsec_per_s=ra_rate,
                dec_rate_arcsec_per_s=dec_shift * pixel_scale / dec_seconds,
                # Angle between the sensor's x axis and the mount's RA axis.
                angle_deg=math.degrees(angle),
                mode="fine" if fine else "pulse",
                ra_sky_per_axis=ra_rate / self._config.calibration_ra_offset_arcsec_per_s if fine else None,
                dec_arcsec_per_step=dec_shift * pixel_scale / dec_steps if fine and dec_steps else None,
                dec_north_sign=1.0 if north_along >= 0 else -1.0,
                pixel_scale_arcsec=pixel_scale,
                calibrated_at=time.time(),
                dec_at_calibration_deg=status.position.dec_deg,
                west_shift_px=(round(west[0], 2), round(west[1], 2)),
                north_shift_px=(round(north[0], 2), round(north[1], 2)),
                ra_at_calibration_deg=status.position.ra_deg,
                west_leg_at=west_at,
                north_leg_at=north_at,
            )
            self._calibration = calibration
            # What was learned was learned against the old calibration.
            self._clear_models()
            self._progress(
                "calibrated",
                message=(
                    (
                        f"RA moves the star {calibration.ra_sky_per_axis:.3f}\u2033 per \u2033 of axis, "
                        f"one Dec step is {calibration.dec_arcsec_per_step:.3f}\u2033, "
                        if calibration.mode == "fine"
                        else f"{calibration.ra_rate_arcsec_per_s:.1f}\u2033/s in RA, "
                        f"{calibration.dec_rate_arcsec_per_s:.1f}\u2033/s in Dec, "
                    )
                    + f"camera {calibration.angle_deg:.0f}\u00b0 from the mount's axes"
                    + ("" if calibration.dec_north_sign > 0 else ", image mirrored")
                ),
                ra_rate_arcsec_per_s=round(calibration.ra_rate_arcsec_per_s, 3),
                dec_rate_arcsec_per_s=round(calibration.dec_rate_arcsec_per_s, 3),
                angle_deg=round(calibration.angle_deg, 2),
                mode=calibration.mode,
                ra_sky_per_axis=None
                if calibration.ra_sky_per_axis is None
                else round(calibration.ra_sky_per_axis, 4),
                dec_arcsec_per_step=None
                if calibration.dec_arcsec_per_step is None
                else round(calibration.dec_arcsec_per_step, 4),
                dec_north_sign=calibration.dec_north_sign,
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
    ) -> tuple[tuple[float, float], int, tuple[float, float]]:
        """Push the star one step at a time, looking after every push.

        A frame after each pulse rather than one at the end of the leg.
        It costs an exposure per step, and it buys three things: a track
        anyone can watch walk across the frame, a fit through five points
        instead of a line between two - which one bad frame can no longer
        ruin - and the ability to notice an axis that is not moving on
        the second pulse instead of the fifth.
        """
        times: list[float] = []
        track = await self._pulse_sequence(direction, origin, phase=phase, times=times)
        shift = _fit_step(track)
        moves = len(track) - 1
        total = (shift[0] * moves, shift[1] * moves)
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
        # When the leg's first and last frames were taken: the sky drifted
        # for that long while it was measured, and the shift carries it.
        return total, moves, (times[0], times[-1])

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
            await self._nudge(direction)
            star = await self._acquire_star(near=(star.x, star.y), radius_px=CALIBRATION_SEARCH_PX)
            moved = math.hypot(star.x - start[0], star.y - start[1])
            self._progress(
                "backlash",
                message=(
                    f"Taking up {direction} backlash: {moved:.1f} px after {index + 1} {self._nudge_noun()}"
                ),
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
        count: int | None = None,
        times: list[float] | None = None,
    ) -> list[tuple[float, float]]:
        """Moves in one direction: measured ones until the star has gone
        far enough, or exactly `count` unmeasured ones to walk it back.

        `times`, when given, collects when each point on the track was seen."""
        times = [] if times is None else times
        config = self._config
        least = count if count is not None else config.calibration_steps
        most = count if count is not None else max(config.calibration_max_steps, least)
        steps = least
        track: list[tuple[float, float]] = []
        if origin is not None:
            # The origin is always the star just acquired.
            track.append(origin)
            times.append(self._last_star_at)

        index = -1
        while index + 1 < most:
            index += 1
            self._progress(
                phase,
                message=f"{self._nudge_label(direction)} ({index + 1} of {steps})",
                direction=str(direction),
                pulse=index + 1,
                pulses=steps,
                track=[[round(x, 1), round(y, 1)] for x, y in track],
                **self._frame_size(),
            )
            await self._nudge(direction)
            if origin is None:
                # A return leg: it only has to get back, and nobody is
                # measuring it, so it does not pay for a frame per pulse.
                continue

            star = await self._acquire_star(near=track[-1], radius_px=CALIBRATION_SEARCH_PX)
            track.append((star.x, star.y))
            times.append(self._last_star_at)
            moved = math.hypot(star.x - track[0][0], star.y - track[0][1])
            # How many moves this leg will take, from how far each goes.
            if moved > 0:
                needed = math.ceil(config.calibration_distance_px / (moved / (index + 1)))
                steps = max(least, min(most, needed))
            self._progress(
                phase,
                message=(
                    f"{self._nudge_label(direction)} ({index + 1} of {steps}): "
                    f"star has moved {moved:.1f} px{self._steps_counted()}"
                ),
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
            if index + 1 >= least and moved >= config.calibration_distance_px:
                break

        return track

    def _fine_available(self) -> bool:
        capabilities = self._mount.descriptor.capabilities
        return (
            self._config.fine_guiding
            and Capability.GUIDE_RATE_OFFSET in capabilities
            and Capability.AXIS_STEPS in capabilities
        )

    def _calibration_dec_steps(self) -> int:
        """Declination steps per calibration move: about as far as RA's."""
        step = getattr(self._mount, "dec_step_arcsec", 0.0) or 0.0
        if step <= 0:
            return 0
        travel = self._config.calibration_ra_offset_arcsec_per_s * self._config.calibration_pulse_ms / 1000.0
        return max(1, round(travel / step))

    async def _nudge(self, direction: GuideDirection) -> None:
        """One calibration move, with the controls guiding will use."""
        if not self._calibrating_fine:
            await self._mount.pulse_guide(direction, self._config.calibration_pulse_ms)
            return
        if direction in (GuideDirection.EAST, GuideDirection.WEST):
            offset = self._config.calibration_ra_offset_arcsec_per_s
            await self._mount.set_ra_rate_offset(offset if direction is GuideDirection.WEST else -offset)
            try:
                await asyncio.sleep(self._config.calibration_pulse_ms / 1000.0)
            finally:
                await self._mount.set_ra_rate_offset(0.0)
            return
        asked = self._calibration_dec_steps()
        counted = await self._mount.step_dec(direction, asked)
        self._last_steps = (asked, counted)

    def _nudge_noun(self) -> str:
        return "moves" if self._calibrating_fine else "pulses"

    def _nudge_label(self, direction: GuideDirection) -> str:
        if not self._calibrating_fine:
            return f"Pulse {direction}, {self._config.calibration_pulse_ms} ms"
        if direction in (GuideDirection.EAST, GuideDirection.WEST):
            offset = self._config.calibration_ra_offset_arcsec_per_s
            sign = "+" if direction is GuideDirection.WEST else "-"
            return f"RA {sign}{offset:.1f}\u2033/s for {self._config.calibration_pulse_ms / 1000:.1f} s"
        return f"{self._calibration_dec_steps()} steps {direction}"

    def _steps_counted(self) -> str:
        """The mount's own count of the last step move, against what was asked."""
        if not self._calibrating_fine or self._last_steps is None:
            return ""
        asked, counted = self._last_steps
        self._last_steps = None
        return f", counter moved {counted} of {asked} steps"

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
        self._last_frame_at = None
        self._pending_ra = self._pending_dec = 0.0
        self._model = self._new_model()
        self._fit = None
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
        await self._release_ra_offset()
        self._set_state(GuidingState.STOPPED)

    async def _release_ra_offset(self) -> None:
        """Back to plain tracking: an offset left behind is a drift."""
        if self._ra_offset == 0.0:
            return
        self._ra_offset = 0.0
        try:
            await self._mount.set_ra_rate_offset(0.0)
        except Exception:
            logger.exception("could not return RA to its tracking rate")

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
                await self._measure_drift()
            while True:
                await self._guide_once()
                fit = self._fit
                back = fit is not None and math.hypot(*fit.position) < self._settle_px()
                if self._mode == "recentre" and back:
                    self._mode = "hold"
                    # The RMS is of guiding, not of the drift measured
                    # before it started.
                    self._samples.clear()
                    self._progress("holding", message="Back on the lock point; guiding")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("guide loop failed")
            await self._release_ra_offset()
            if isinstance(error, DeviceError):
                # A mount command failed partway, so whatever it left
                # running is unknown: stopped, not left to it. A guide loop
                # that died mid-step and walked away is how a declination
                # axis spun unwatched for half an hour.
                try:
                    await self._mount.abort_slew()
                except Exception:
                    logger.exception("could not stop the mount after the guide loop failed")
            self._progress("failed", message=f"Guiding stopped: {error}")
            self._set_state(GuidingState.ERROR)

    def _new_model(self) -> PixelModel | None:
        calibration = self._calibration
        if calibration is None:
            return None
        return PixelModel(
            Response(ra=calibration.ra_response_px, dec=calibration.dec_response_px),
            window_s=self._config.model_window_s,
            prior_fraction=self._config.response_prior_fraction,
            worm_period_s=self.worm_period_s() if self._config.fit_worm else None,
            worm_prior_px=self._config.worm_prior_arcsec / calibration.pixel_scale_arcsec,
        )

    def worm_period_s(self) -> float | None:
        """The RA worm's period: as configured, or as the mount says."""
        if self._config.worm_period_s:
            return self._config.worm_period_s
        try:
            period = self._worm_period_source()
        except Exception:
            logger.exception("could not ask for the worm's period")
            return None
        return period if period and period > 0 else None

    def _settle_px(self) -> float:
        scale = self._calibration.pixel_scale_arcsec if self._calibration else 1.0
        return self._config.settle_arcsec / scale

    async def _measure_drift(self) -> None:
        """Watch the star with no corrections until its drift is known.

        Every frame refits the drift over the window; this only decides
        when it is known well enough to start cancelling it - as well as
        asked, or as well as it is going to get.
        """
        config = self._config
        scale = self._calibration.pixel_scale_arcsec if self._calibration else 1.0
        precision = config.drift_precision_arcsec_per_min / scale / 60.0
        started = time.time()
        errors: list[tuple[float, float]] = []
        while True:
            await self._guide_once()
            now = time.time()
            elapsed = now - started
            fit = self._fit
            known = False
            if fit is not None:
                error = max(fit.drift_error)
                errors.append((now, error))
                earlier = next((e for t, e in errors if t >= now - 10.0), errors[0][1])
                known = error < precision or (elapsed >= 30.0 and error > 0.95 * earlier)
            self._progress(
                "drift_measuring",
                message="Measuring the drift - no corrections",
                elapsed_s=round(elapsed, 1),
                min_s=config.drift_min_s,
                max_s=config.drift_max_s,
                samples=len(self._samples),
                precision_px_per_min=round(precision * 60, 3),
                **_fit_detail(fit),
            )
            if elapsed >= config.drift_max_s or (elapsed >= config.drift_min_s and known):
                break
        await self._unbias_calibration()
        self._mode = "recentre"
        self._progress(
            "recentring",
            message="Cancelling the drift; walking the star back to the lock point",
            **_fit_detail(self._fit),
        )

    async def _unbias_calibration(self) -> None:
        """Take the drift back out of the calibration it was measured under.

        Each calibration leg takes half a minute or so, and the sky drifts
        all the while: the shift it measures is the move plus that drift,
        which on a few px/min is a few percent of the length and a degree
        or two of angle. The drift was not known then; it is now. Only the
        drift of the same patch of sky is any use for this, so only a
        recent calibration from where the mount still points is touched.
        """
        calibration, fit = self._calibration, self._fit
        if calibration is None or fit is None or calibration.drift_corrected:
            return
        legs = calibration.west_leg_at, calibration.north_leg_at
        if legs[0] is None or legs[1] is None:
            return
        if fit.time - calibration.calibrated_at > CALIBRATION_DRIFT_MAX_AGE_S:
            return
        if math.hypot(*fit.drift) <= 2 * math.hypot(*fit.drift_error):
            return
        position = (await self._mount.status()).position
        ra_then = calibration.ra_at_calibration_deg
        moved = abs(position.dec_deg - calibration.dec_at_calibration_deg)
        if ra_then is not None:
            moved = max(moved, abs((position.ra_deg - ra_then + 180.0) % 360.0 - 180.0))
        if moved > CALIBRATION_SAME_SKY_DEG:
            return

        def during(start: float, end: float) -> tuple[float, float]:
            dx, dy = fit.drift[0] * (end - start), fit.drift[1] * (end - start)
            if fit.worm is not None:
                wx, wy = fit.worm.change_px(start, end)
                dx, dy = dx + wx, dy + wy
            return dx, dy

        corrected = _without_drift(calibration, during(*legs[0]), during(*legs[1]))
        if corrected is None:
            return
        self._calibration = corrected
        if self._model is not None:
            self._model.calibration = Response(ra=corrected.ra_response_px, dec=corrected.dec_response_px)
            self._fit = self._model.fit()
        ra_change = math.hypot(*corrected.ra_response_px) / math.hypot(*calibration.ra_response_px)
        dec_change = math.hypot(*corrected.dec_response_px) / math.hypot(*calibration.dec_response_px)
        turned = corrected.angle_deg - calibration.angle_deg
        self._progress(
            "calibration_corrected",
            message=(
                f"Calibration corrected for the drift during it: RA {100 * (ra_change - 1):+.1f}%, "
                f"Dec {100 * (dec_change - 1):+.1f}%, angle {turned:+.1f}\u00b0"
            ),
            ra_change=round(ra_change, 4),
            dec_change=round(dec_change, 4),
            angle_change_deg=round(turned, 2),
            west_shift_px=list(corrected.west_shift_px),
            north_shift_px=list(corrected.north_shift_px),
        )

    def _cancelling(self) -> bool:
        return self._config.null_drift and self._mode != "measure" and self._fit is not None

    async def _guide_once(self) -> None:
        calibration = self._calibration
        lock = self._lock_position
        if calibration is None or lock is None:
            raise AstropiError("guiding started without a calibration or lock position")
        if self._model is None:
            self._model = self._new_model()
        model = self._model
        assert model is not None

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
        # Also in the mount's axes and arcseconds, for the error chart and
        # the RMS figures everyone else quotes.
        ra_px, dec_px = _rotate(calibration, dx, dy)
        ra_arcsec = ra_px * calibration.pixel_scale_arcsec
        dec_arcsec = dec_px * calibration.pixel_scale_arcsec

        now = time.time()
        elapsed = 0.0 if self._last_frame_at is None else min(now - self._last_frame_at, 30.0)
        self._last_frame_at = now
        # What was delivered since the last frame: the steps and pulses
        # counted as they went, and the RA rate offset for as long as it ran.
        ra_sent = self._pending_ra
        if calibration.mode == "fine":
            ra_sent += self._ra_offset * elapsed
        model.sent(ra_sent, self._pending_dec)
        self._pending_ra = self._pending_dec = 0.0
        model.observe(now, dx, dy)
        fit = model.fit()
        self._fit = fit

        done = await self._correct(calibration, fit, elapsed)

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
            ra_pulse_ms=done.ra_pulse_ms,
            dec_pulse_ms=done.dec_pulse_ms,
            ra_direction=done.ra_direction,
            dec_direction=done.dec_direction,
            ra_withheld=done.ra_withheld,
            dec_withheld=done.dec_withheld,
            ra_rate_offset=done.ra_rate_offset,
            dec_steps=done.dec_steps,
            ra_predicted_arcsec=done.ra_predicted,
            dec_predicted_arcsec=done.dec_predicted,
            mode=self._mode,
            star_flux=star.flux,
            star_hfd=star.hfd,
            snr=star.snr,
            saturated=star.saturated,
            fit=_fit_detail(fit),
        )
        self._samples.append(sample)
        if self._mode == "hold":
            self._update_settling(sample)
        self._events.publish(Topic.GUIDING_SAMPLE, **_sample_payload(sample))

    async def _correct(
        self, calibration: GuideCalibration, fit: PixelFit | None, elapsed: float
    ) -> _Correction:
        """Cancel the drift and walk back the offset, in pixels.

        The move wanted before the next frame is the drift and worm swing
        expected over it, plus part of the (fitted, so seeing-averaged)
        offset, all in sensor pixels. The two fitted responses turn that
        into the RA and Dec corrections that produce it - a 2x2 solve -
        which go out as a rate offset and whole steps, or as pulses.
        """
        config = self._config
        fine = calibration.mode == "fine"
        interval = elapsed if elapsed > 0 else config.exposure_s
        measuring = self._mode == "measure" or fit is None
        if measuring:
            if fine and self._ra_offset:
                self._ra_offset = await self._mount.set_ra_rate_offset(0.0)
            reason = "measuring drift - no corrections"
            return _Correction(ra_withheld=reason, dec_withheld=reason)
        assert fit is not None

        response = self._trusted_response(fit, calibration)
        # What the sky will do before the next frame - the drift, and the
        # worm's swing - cancelled as it happens.
        motion = fit.motion(interval) if self._cancelling() else (0.0, 0.0)
        ahead_ra, ahead_dec = response.solve(-motion[0], -motion[1]) or (0.0, 0.0)
        # And part of where the star is, each axis by its own share: solved
        # into RA and Dec first, because the shares are per mount axis and
        # the position is in sensor pixels.
        position = fit.position
        dead_band = config.min_move_arcsec / calibration.pixel_scale_arcsec
        if math.hypot(*position) < dead_band:
            position = (0.0, 0.0)
        back_ra, back_dec = response.solve(-position[0], -position[1]) or (0.0, 0.0)
        ra_share, dec_share = self._position_share()
        ra_units = ahead_ra + ra_share * back_ra
        dec_units = ahead_dec + dec_share * back_dec
        # Which way declination has to go, steadily, to cancel the drift -
        # the one direction it may move while that drift is being cancelled.
        drift_dec = (response.solve(-fit.drift[0], -fit.drift[1]) or (0.0, 0.0))[1]
        drift_is_real = math.hypot(*fit.drift) > 2 * math.hypot(*fit.drift_error)

        done = _Correction()
        # Right ascension.
        if fine:
            limit = config.max_ra_offset_arcsec_per_s
            offset = max(-limit, min(limit, ra_units / interval))
            if abs(offset - self._ra_offset) > 1e-5:
                self._ra_offset = await self._mount.set_ra_rate_offset(offset)
            done.ra_rate_offset = self._ra_offset
            done.ra_direction = ("west" if self._ra_offset > 0 else "east") if self._ra_offset else ""
            ra_delivered = self._ra_offset * interval
        else:
            ms = min(round(abs(ra_units)), config.max_pulse_ms)
            direction = GuideDirection.WEST if ra_units > 0 else GuideDirection.EAST
            if ms:
                await self._mount.pulse_guide(direction, ms)
                self._pending_ra += ms if ra_units > 0 else -ms
                done.ra_pulse_ms = float(ms)
                done.ra_direction = str(direction)
            else:
                done.ra_withheld = "under a millisecond"
            ra_delivered = (ms if ra_units > 0 else -ms) if ms else 0.0

        # Declination: whole units, only in the drift-cancelling direction
        # while a drift is being cancelled.
        count = min(round(abs(dec_units)), config.max_dec_steps if fine else config.max_pulse_ms)
        direction = GuideDirection.NORTH if dec_units > 0 else GuideDirection.SOUTH
        against = (
            config.dec_mode is DecGuideMode.AUTO
            and self._cancelling()
            and drift_is_real
            and dec_units * drift_dec < 0
        )
        dec_delivered = 0.0
        if count == 0:
            done.dec_withheld = f"under one {calibration.dec_unit}"
        elif against:
            done.dec_withheld = "past the lock against the drift - letting the drift bring it back"
        elif not self._dec_allowed(direction):
            done.dec_withheld = f"declination guiding is set to {config.dec_mode}"
        elif fine:
            moved = await self._mount.step_dec(direction, count)
            signed = moved if direction is GuideDirection.NORTH else -moved
            self._pending_dec += signed
            done.dec_steps = signed
            done.dec_direction = str(direction) if moved else ""
            dec_delivered = float(signed)
        else:
            await self._mount.pulse_guide(direction, count)
            signed = count if direction is GuideDirection.NORTH else -count
            self._pending_dec += signed
            done.dec_pulse_ms = float(count)
            done.dec_direction = str(direction)
            dec_delivered = float(signed)

        # What those should do, in the mount's axes and arcsec, for the chart.
        move_x = response.ra[0] * ra_delivered + response.dec[0] * dec_delivered
        move_y = response.ra[1] * ra_delivered + response.dec[1] * dec_delivered
        ra_move, dec_move = _to_axes(calibration, move_x, move_y)
        done.ra_predicted, done.dec_predicted = ra_move, dec_move
        return done

    def _trusted_response(self, fit: PixelFit, calibration: GuideCalibration) -> Response:
        """The fitted response, unless it has wandered implausibly far.

        A column more than half as long again or half as short, or turned
        by more than 30 degrees from the calibration, is the fit being
        fooled - a gust, a lost frame - not the mount changing; the loop
        falls back to the calibration rather than steer by it.
        """
        cal = Response(ra=calibration.ra_response_px, dec=calibration.dec_response_px)
        for fitted, calibrated in ((fit.response.ra, cal.ra), (fit.response.dec, cal.dec)):
            length, reference = math.hypot(*fitted), math.hypot(*calibrated)
            if reference <= 0:
                return cal
            ratio = length / reference
            cosine = (fitted[0] * calibrated[0] + fitted[1] * calibrated[1]) / max(length * reference, 1e-12)
            if not (0.5 <= ratio <= 1.5) or cosine < math.cos(math.radians(30)):
                return cal
        return fit.response

    def _clear_models(self) -> None:
        if self._model is not None:
            self._model.clear()
        self._fit = None

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
        if self._model is not None:
            self._model.move_reference(*shift)

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
        self._last_star_at = time.time()
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
            # The brightest unclipped star. The brightest of all is usually
            # saturated, and a saturated star's centre moves in jumps of
            # most of a pixel - which the loop then dutifully corrects.
            unclipped = [star for star in usable if not star.saturated]
            if not unclipped:
                raise AstropiError(
                    f"every usable star is saturated ({len(usable)} of them) - lower the guide "
                    "gain or exposure, or pick a fainter star by hand"
                )
            return unclipped[0]
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
        "fit": sample.fit,
        "saturated": sample.saturated,
        "ra_rate_offset": round(sample.ra_rate_offset, 4),
        "dec_steps": sample.dec_steps,
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


def _without_drift(
    calibration: GuideCalibration, west_drift: tuple[float, float], north_drift: tuple[float, float]
) -> GuideCalibration | None:
    """The calibration as it would have come out with no drift during its
    legs, given how far the sky moved the star during each - or `None`
    when that is too much of the leg to be believed."""
    old_west, old_north = calibration.west_shift_px, calibration.north_shift_px
    old_ra, old_dec = math.hypot(*old_west), math.hypot(*old_north)
    ra_length, dec_length = math.hypot(*calibration.ra_response_px), math.hypot(*calibration.dec_response_px)
    if min(old_ra, old_dec, ra_length, dec_length) <= 0:
        return None
    if (
        math.hypot(*west_drift) > CALIBRATION_DRIFT_MAX_CHANGE * old_ra
        or math.hypot(*north_drift) > CALIBRATION_DRIFT_MAX_CHANGE * old_dec
    ):
        return None
    west = (old_west[0] - west_drift[0], old_west[1] - west_drift[1])
    north = (old_north[0] - north_drift[0], old_north[1] - north_drift[1])
    # Units each leg sent, from what the calibration made of them.
    ra_units, dec_units = old_ra / ra_length, old_dec / dec_length
    ra_response = (
        calibration.ra_response_px[0] - west_drift[0] / ra_units,
        calibration.ra_response_px[1] - west_drift[1] / ra_units,
    )
    dec_response = (
        calibration.dec_response_px[0] - north_drift[0] / dec_units,
        calibration.dec_response_px[1] - north_drift[1] / dec_units,
    )
    ra_scale = math.hypot(*ra_response) / ra_length
    dec_scale = math.hypot(*dec_response) / dec_length
    angle = math.atan2(west[1], west[0])
    north_along = -north[0] * math.sin(angle) + north[1] * math.cos(angle)
    return replace(
        calibration,
        ra_response_px=ra_response,
        dec_response_px=dec_response,
        ra_rate_arcsec_per_s=calibration.ra_rate_arcsec_per_s * ra_scale,
        dec_rate_arcsec_per_s=calibration.dec_rate_arcsec_per_s * dec_scale,
        angle_deg=math.degrees(angle),
        ra_sky_per_axis=None
        if calibration.ra_sky_per_axis is None
        else calibration.ra_sky_per_axis * ra_scale,
        dec_arcsec_per_step=None
        if calibration.dec_arcsec_per_step is None
        else calibration.dec_arcsec_per_step * dec_scale,
        dec_north_sign=1.0 if north_along >= 0 else -1.0,
        west_shift_px=(round(west[0], 2), round(west[1], 2)),
        north_shift_px=(round(north[0], 2), round(north[1], 2)),
        drift_corrected=True,
    )


def _round3(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


@dataclass(slots=True)
class _Correction:
    """What one frame sent to the mount, however it was sent."""

    ra_pulse_ms: float = 0.0
    dec_pulse_ms: float = 0.0
    ra_direction: str = ""
    dec_direction: str = ""
    ra_withheld: str = ""
    dec_withheld: str = ""
    ra_predicted: float = 0.0
    dec_predicted: float = 0.0
    ra_rate_offset: float = 0.0
    dec_steps: int = 0


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


def _fit_detail(fit: PixelFit | None) -> dict:
    """The pixel model, for a progress report or a sample, per minute."""
    if fit is None:
        return {}
    worm = fit.worm
    return {
        "worm_period_s": None if worm is None else round(worm.period_s, 1),
        "worm_px": None if worm is None else round(worm.amplitude_px, 3),
        "worm_error_px": None
        if worm is None
        else round(worm.amplitude_error * math.hypot(*worm.direction), 3),
        "x": round(fit.position[0], 3),
        "y": round(fit.position[1], 3),
        "drift_x": round(fit.drift[0] * 60, 3),
        "drift_y": round(fit.drift[1] * 60, 3),
        "drift_x_error": round(fit.drift_error[0] * 60, 3),
        "drift_y_error": round(fit.drift_error[1] * 60, 3),
        "ra_response": [round(v, 4) for v in fit.response.ra],
        "dec_response": [round(v, 4) for v in fit.response.dec],
        "ra_response_error": [round(v, 4) for v in fit.response_error.ra],
        "dec_response_error": [round(v, 4) for v in fit.response_error.dec],
        "scatter": round(fit.scatter, 3),
        "frames": fit.frames,
        "span_s": round(fit.span_s, 1),
    }


def _model_out(
    fit: PixelFit | None, calibration: GuideCalibration | None, worm_period_s: float | None
) -> dict | None:
    """The pixel model for the status endpoint, with what it started from."""
    if calibration is None:
        return None
    detail = _fit_detail(fit)
    detail.setdefault("worm_period_s", worm_period_s)
    detail.update(
        ra_calibrated=list(calibration.ra_response_px),
        dec_calibrated=list(calibration.dec_response_px),
        ra_unit=calibration.ra_unit,
        dec_unit=calibration.dec_unit,
    )
    return detail
