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

from astropi.core.errors import AstropiError
from astropi.core.events import EventBus, Topic
from astropi.devices.camera import Camera, ExposureRequest, Frame, FrameKind
from astropi.devices.guider import GuideCalibration, GuideSample, GuidingState, GuidingStatus
from astropi.devices.mount import GuideDirection, Mount
from astropi.services.stardetect import DetectedStar, detect_stars, nearest_star

logger = logging.getLogger(__name__)

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


@dataclass(slots=True)
class GuidingConfig:
    exposure_s: float = 2.0
    gain: int = 250
    #: Fraction of the measured error corrected each cycle, per axis.
    ra_aggressiveness: float = 0.7
    dec_aggressiveness: float = 0.6
    #: Errors below this are seeing, not tracking error - leave them alone.
    min_move_arcsec: float = 0.15
    #: No single correction may exceed this, so one bad frame cannot bolt.
    max_pulse_ms: int = 1_000
    #: How far the star may move between frames before the lock is lost.
    search_radius_px: float = 25.0
    #: Consecutive failures tolerated before declaring the star lost.
    max_lost_frames: int = 5
    calibration_pulse_ms: int = 900
    calibration_steps: int = 5
    #: Guiding is "settled" once error stays under this for `settle_time_s`.
    settle_arcsec: float = 1.5
    settle_time_s: float = 8.0
    rms_window: int = 50


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

    def invalidate_calibration(self) -> None:
        """Drop the calibration after anything that changes the geometry.

        Rotating the camera, flipping the mount, or moving a long way in
        declination all break the pulse-to-pixel mapping; guiding on a stale
        calibration pushes the star the wrong way.
        """
        self._calibration = None

    # ----------------------------------------------------------- calibration

    async def calibrate(self) -> GuideCalibration:
        """Learn how mount pulses move the star on the sensor.

        Each axis is pulsed several times in one direction and the total
        displacement measured. Several small steps rather than one long one
        so that backlash and a single bad frame are both averaged down.
        """
        pixel_scale = self._require_pixel_scale()
        self._set_state(GuidingState.CALIBRATING)
        try:
            star = await self._acquire_star()
            origin = (star.x, star.y)

            west = await self._calibration_leg(GuideDirection.WEST, origin)
            # Walk back to the start before doing the other axis, so the
            # declination measurement is not taken from a displaced position.
            await self._pulse_sequence(GuideDirection.EAST)
            north = await self._calibration_leg(GuideDirection.NORTH, origin)
            await self._pulse_sequence(GuideDirection.SOUTH)

            pulse_seconds = self._config.calibration_pulse_ms * self._config.calibration_steps / 1000.0
            ra_shift = math.hypot(*west)
            dec_shift = math.hypot(*north)
            if ra_shift < 2.0 or dec_shift < 2.0:
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
            )
            self._calibration = calibration
            self._set_state(GuidingState.STOPPED)
            return calibration
        except Exception:
            self._set_state(GuidingState.ERROR)
            raise

    async def _calibration_leg(
        self, direction: GuideDirection, origin: tuple[float, float]
    ) -> tuple[float, float]:
        await self._pulse_sequence(direction)
        star = await self._acquire_star(near=origin, radius_px=400.0)
        return (star.x - origin[0], star.y - origin[1])

    async def _pulse_sequence(self, direction: GuideDirection) -> None:
        for _ in range(self._config.calibration_steps):
            await self._mount.pulse_guide(direction, self._config.calibration_pulse_ms)

    # ------------------------------------------------------------------ loop

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if self._calibration is None:
            await self.calibrate()
        star = await self._acquire_star()
        self._lock_position = (star.x, star.y)
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
        if ra_pulse:
            await self._mount.pulse_guide(
                GuideDirection.EAST if ra_arcsec > 0 else GuideDirection.WEST, ra_pulse
            )
        if dec_pulse:
            await self._mount.pulse_guide(
                GuideDirection.SOUTH if dec_arcsec > 0 else GuideDirection.NORTH, dec_pulse
            )

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
            star_flux=star.flux,
            star_hfd=star.hfd,
            snr=star.snr,
        )
        self._samples.append(sample)
        self._update_settling(sample)
        self._events.publish(Topic.GUIDING_SAMPLE, **_sample_payload(sample))

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
            return
        if self._settled_since is None:
            self._settled_since = time.time()
        elif (
            time.time() - self._settled_since >= self._config.settle_time_s
            and self._state is not GuidingState.GUIDING
        ):
            self._set_state(GuidingState.GUIDING)

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

    async def _acquire_star(
        self, *, near: tuple[float, float] | None = None, radius_px: float | None = None
    ) -> DetectedStar:
        stars = await self._expose_and_detect()
        if not stars:
            raise AstropiError("no guide star found - try a longer exposure or more gain")
        if near is None:
            return stars[0]
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
        "snr": round(sample.snr, 1),
        "hfd": round(sample.star_hfd, 2),
    }
