"""Simulated equatorial mount.

Models the errors that the rest of the software exists to deal with:

* a polar axis that is not on the pole, which makes GoTos miss and tracking
  drift - the thing plate-solve centring and polar alignment must fix;
* periodic error from the worm gear, a slow sinusoid in hour angle that
  guiding has to chase;
* declination backlash, so a guide correction that reverses direction is
  partly swallowed before the axis moves.

The mount *reports* where it believes it is pointing, which is not where it
actually is. That gap is deliberate and is the whole point: it is what a
plate solve discovers and a sync corrects.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time
from dataclasses import dataclass

from astropi.core.errors import DeviceBusyError, SafetyError
from astropi.core.events import EventBus, Topic
from astropi.core.geometry import AltAz, RaDec, normalize_deg, wrap_symmetric_deg
from astropi.core.pointing import (
    PolarAxis,
    misaligned_pole,
    true_pole,
    vector_to_alt_az,
    vector_to_radec,
)
from astropi.core.site import ObservingSite
from astropi.core.timekeeping import SIDEREAL_RATE_DEG_PER_S, local_sidereal_time_deg
from astropi.devices.base import Capability, ConnectionState, DeviceDescriptor, DeviceRole
from astropi.devices.mount import (
    GuideDirection,
    MountState,
    MountStatus,
    PierSide,
    TrackingRate,
)

PARK_HOUR_ANGLE_DEG = 0.0
PARK_DEC_DEG = 90.0


@dataclass(slots=True)
class SimulatedMountConfig:
    """Defaults describe a decently-but-not-perfectly set up portable rig."""

    polar_alt_error_deg: float = 0.35
    polar_az_error_deg: float = -0.22
    slew_rate_deg_per_s: float = 4.0
    #: Peak-to-peak worm error and its period, in arcseconds and seconds.
    periodic_error_arcsec: float = 18.0
    periodic_error_period_s: float = 479.0
    #: Declination backlash. A reversing guide correction is swallowed
    #: until this much travel has been taken up - typical of a small
    #: portable mount, and the reason dec guiding is often one-way.
    dec_backlash_arcsec: float = 10.0
    seeing_arcsec: float = 1.6
    #: Ceiling on simulated slew duration, so a long move is not a long wait.
    max_slew_seconds: float = 8.0
    #: Wall-clock seconds per simulated second of guide pulse.
    #:
    #: Compresses time without changing physics: a pulse still moves the
    #: axis by its nominal duration, it just waits less. It has to match
    #: the camera's own time scale, or the guide loop runs at a different
    #: cadence than it calibrated at and over-corrects.
    time_scale: float = 1.0
    #: Degrees of axis motion per second of pulse-guide, at the guide rate.
    guide_rate_deg_per_s: float = SIDEREAL_RATE_DEG_PER_S * 0.5
    min_altitude_deg: float = 0.0


class SimulatedMount:
    """A mount you can develop against with nothing plugged in."""

    def __init__(
        self,
        site: ObservingSite,
        events: EventBus,
        config: SimulatedMountConfig | None = None,
        *,
        seed: int | None = None,
    ) -> None:
        self._site = site
        self._events = events
        self._config = config or SimulatedMountConfig()
        self._random = random.Random(seed)

        self._true_pole: PolarAxis = true_pole(site.latitude_deg)
        self._mount_pole: PolarAxis = misaligned_pole(
            site.latitude_deg,
            self._config.polar_alt_error_deg,
            self._config.polar_az_error_deg,
        )

        # Mechanical axis angles. Everything observable is derived from these.
        self._ha_axis = PARK_HOUR_ANGLE_DEG
        self._dec_axis = PARK_DEC_DEG
        self._last_tick = time.time()

        # What the mount believes about itself, which drifts from the truth.
        self._sync_offset = RaDec(0.0, 0.0)
        self._target: RaDec | None = None

        self._state = MountState.PARKED
        self._tracking = False
        self._tracking_rate = TrackingRate.SIDEREAL
        self._connection = ConnectionState.DISCONNECTED
        self._slew_task: asyncio.Task[None] | None = None
        self._last_dec_direction = 0
        self._backlash_debt = 0.0
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- device

    @property
    def descriptor(self) -> DeviceDescriptor:
        return DeviceDescriptor(
            id="sim-mount",
            role=DeviceRole.MOUNT,
            name="Simulated equatorial mount",
            driver="simulator",
            capabilities=frozenset(
                {
                    Capability.SLEW,
                    Capability.SYNC,
                    Capability.PARK,
                    Capability.TRACKING_RATES,
                    Capability.PULSE_GUIDE,
                }
            ),
        )

    @property
    def connection_state(self) -> ConnectionState:
        return self._connection

    async def connect(self) -> None:
        self._connection = ConnectionState.CONNECTING
        await asyncio.sleep(0.2)
        self._last_tick = time.time()
        self._connection = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        await self.abort_slew()
        self._connection = ConnectionState.DISCONNECTED

    # ----------------------------------------------------------- simulation

    @property
    def site(self) -> ObservingSite:
        return self._site

    def _lst(self, at: float | None = None) -> float:
        return local_sidereal_time_deg(self._site.longitude_deg, at)

    def _advance(self, now: float | None = None) -> None:
        """Roll the hour-angle axis forward for elapsed tracking time."""
        now = time.time() if now is None else now
        elapsed = now - self._last_tick
        self._last_tick = now
        if self._tracking and elapsed > 0:
            self._ha_axis += SIDEREAL_RATE_DEG_PER_S * elapsed * _rate_multiplier(self._tracking_rate)

    def _periodic_error_deg(self) -> float:
        """Worm error: a sinusoid in hour angle, plus a little seeing."""
        phase = 2 * math.pi * (time.time() % self._config.periodic_error_period_s)
        phase /= self._config.periodic_error_period_s
        worm = (self._config.periodic_error_arcsec / 2.0) * math.sin(phase) / 3600.0
        seeing = self._random.gauss(0.0, self._config.seeing_arcsec) / 3600.0
        return worm + seeing

    def true_position(self, at: float | None = None) -> RaDec:
        """Where the telescope is *really* looking.

        Only the simulated camera may call this - it stands in for physical
        reality. Application code must get its position from a plate solve,
        exactly as it would with real hardware.
        """
        self._advance(at)
        direction = self._mount_pole.pointing(self._ha_axis + self._periodic_error_deg(), self._dec_axis)
        return vector_to_radec(direction, self._lst(at), self._true_pole)

    def reported_position(self, at: float | None = None) -> RaDec:
        """Where the mount believes it is looking, from its own model."""
        self._advance(at)
        believed = self._true_pole.pointing(self._ha_axis, self._dec_axis)
        raw = vector_to_radec(believed, self._lst(at), self._true_pole)
        return raw.offset_by(self._sync_offset.ra_deg, self._sync_offset.dec_deg)

    def true_altitude_az(self, at: float | None = None) -> AltAz:
        self._advance(at)
        direction = self._mount_pole.pointing(self._ha_axis, self._dec_axis)
        alt, az = vector_to_alt_az(direction)
        return AltAz(alt_deg=alt, az_deg=az)

    @property
    def polar_error(self) -> tuple[float, float]:
        """The truth the polar alignment routine is trying to discover."""
        return self._config.polar_alt_error_deg, self._config.polar_az_error_deg

    def set_polar_error(self, alt_error_deg: float, az_error_deg: float) -> None:
        """Adjust the simulated polar axis, as turning the knobs would."""
        self._config.polar_alt_error_deg = alt_error_deg
        self._config.polar_az_error_deg = az_error_deg
        self._mount_pole = misaligned_pole(self._site.latitude_deg, alt_error_deg, az_error_deg)

    # -------------------------------------------------------------- control

    async def status(self) -> MountStatus:
        position = self.reported_position()
        return MountStatus(
            state=self._state,
            position=position,
            horizontal=self.true_altitude_az(),
            target=self._target,
            tracking=self._tracking,
            tracking_rate=self._tracking_rate,
            pier_side=PierSide.UNKNOWN,
            slewing=self._state is MountState.SLEWING,
        )

    async def slew_to(self, target: RaDec) -> None:
        if self._state is MountState.SLEWING:
            raise DeviceBusyError("mount is already slewing")

        # Refuse a move below the horizon limit rather than grinding into a
        # tripod leg - the same check a real driver owes its operator.
        corrected = target.offset_by(-self._sync_offset.ra_deg, -self._sync_offset.dec_deg)
        desired_ha = wrap_symmetric_deg(self._lst() - corrected.ra_deg)
        preview = self._mount_pole.pointing(desired_ha, corrected.dec_deg)
        altitude, _ = vector_to_alt_az(preview)
        if altitude < self._config.min_altitude_deg:
            raise SafetyError(
                f"target is {altitude:.1f}° above the horizon, "
                f"below the {self._config.min_altitude_deg:.1f}° limit"
            )

        self._target = target
        self._state = MountState.SLEWING
        self._slew_task = asyncio.create_task(self._run_slew(corrected, desired_ha))

    async def _run_slew(self, corrected: RaDec, initial_ha: float) -> None:
        try:
            self._advance()
            distance = max(
                abs(wrap_symmetric_deg(initial_ha - self._ha_axis)),
                abs(corrected.dec_deg - self._dec_axis),
            )
            duration = distance / self._config.slew_rate_deg_per_s
            # Slews are simulated as elapsed wall-clock time rather than
            # stepped, so that "is it there yet" polling behaves the way it
            # will against a real mount.
            await asyncio.sleep(min(duration, self._config.max_slew_seconds))
            self._advance()
            # Recompute the hour angle on arrival. A GoTo targets a sky
            # coordinate, not a mechanical angle, and sidereal time has moved
            # on during the slew - landing on the angle computed at departure
            # would miss by a quarter degree per minute spent slewing.
            self._ha_axis = wrap_symmetric_deg(self._lst() - corrected.ra_deg)
            self._dec_axis = corrected.dec_deg
            self._backlash_debt = self._config.dec_backlash_arcsec / 3600.0
            self._last_dec_direction = 0
            # Tracking starts by itself on arrival, which is what every GoTo
            # mount does - having slewed to a coordinate, staying on it is
            # the only useful thing left to do, and a mount that stopped
            # would let the target drift out of frame immediately.
            self._tracking = True
            self._state = MountState.TRACKING
            self._publish()
        except asyncio.CancelledError:
            self._state = MountState.IDLE
            raise

    async def wait_for_slew(self, *, timeout_s: float = 120.0) -> None:
        task = self._slew_task
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)

    async def abort_slew(self) -> None:
        task = self._slew_task
        self._slew_task = None
        if task is not None and not task.done():
            task.cancel()
            # An aborted slew is expected to raise, and CancelledError is a
            # BaseException - suppressing Exception alone would let it escape.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._state is MountState.SLEWING:
            self._state = MountState.TRACKING if self._tracking else MountState.IDLE

    async def sync_to(self, actual: RaDec) -> None:
        """Fold a known-true position into the mount's pointing model."""
        believed = self.reported_position()
        self._sync_offset = RaDec(
            normalize_deg(self._sync_offset.ra_deg + (actual.ra_deg - believed.ra_deg) + 180.0) - 180.0,
            self._sync_offset.dec_deg + (actual.dec_deg - believed.dec_deg),
        )
        self._publish()

    async def set_tracking(self, enabled: bool, rate: TrackingRate = TrackingRate.SIDEREAL) -> None:
        self._advance()
        self._tracking = enabled
        self._tracking_rate = rate
        if self._state is not MountState.SLEWING:
            self._state = MountState.TRACKING if enabled else MountState.IDLE
        self._publish()

    async def park(self) -> None:
        await self.abort_slew()
        self._advance()
        self._ha_axis = PARK_HOUR_ANGLE_DEG
        self._dec_axis = PARK_DEC_DEG
        self._tracking = False
        self._target = None
        self._state = MountState.PARKED
        self._publish()

    async def unpark(self) -> None:
        if self._state is MountState.PARKED:
            self._state = MountState.IDLE
        self._publish()

    async def pulse_guide(self, direction: GuideDirection, duration_ms: int) -> None:
        async with self._lock:
            self._advance()
            travel = self._config.guide_rate_deg_per_s * (duration_ms / 1000.0)

            if direction in (GuideDirection.EAST, GuideDirection.WEST):
                sign = 1.0 if direction is GuideDirection.WEST else -1.0
                self._ha_axis += sign * travel
            else:
                sign = 1.0 if direction is GuideDirection.NORTH else -1.0
                travel = self._consume_backlash(int(sign), travel)
                self._dec_axis = max(-90.0, min(90.0, self._dec_axis + sign * travel))

            # Pulses are short; sleeping keeps the guide loop's real-time
            # behaviour honest, scaled so a compressed session stays coherent.
            await asyncio.sleep(min(duration_ms / 1000.0, 2.0) * self._config.time_scale)

    def _consume_backlash(self, direction: int, travel: float) -> float:
        """Swallow part of a reversing move, as real gear teeth do."""
        if direction != self._last_dec_direction and self._last_dec_direction != 0:
            self._backlash_debt = self._config.dec_backlash_arcsec / 3600.0
        self._last_dec_direction = direction
        if self._backlash_debt <= 0:
            return travel
        absorbed = min(self._backlash_debt, travel)
        self._backlash_debt -= absorbed
        return travel - absorbed

    def _publish(self) -> None:
        position = self.reported_position()
        horizontal = self.true_altitude_az()
        self._events.publish(
            Topic.MOUNT_POSITION,
            state=str(self._state),
            ra_deg=position.ra_deg,
            dec_deg=position.dec_deg,
            alt_deg=horizontal.alt_deg,
            az_deg=horizontal.az_deg,
            tracking=self._tracking,
        )


def _rate_multiplier(rate: TrackingRate) -> float:
    """Tracking rates relative to sidereal."""
    return {
        TrackingRate.SIDEREAL: 1.0,
        TrackingRate.KING: 1.0,
        TrackingRate.LUNAR: 0.9661,
        TrackingRate.SOLAR: 0.9973,
    }[rate]
