"""A real Sky-Watcher mount, driven over its serial port.

Written against the motor controller directly rather than through INDI or
the SynScan app protocol: the Star Adventurer GTi exposes a USB serial port
that speaks Synta's axis protocol, and going straight at it means no daemon
to install on the Pi and no second process to keep alive in the field.

The controller knows nothing about the sky. It counts motor steps on two
axes, so everything celestial - hour angle, declination, tracking rate,
which way is west - is computed here and handed down as counts and step
periods. That is the whole point of the `Mount` protocol: this file is the
only place in the application that knows a Star Adventurer exists.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from astropi.core.errors import DeviceError, NotConnectedError
from astropi.core.events import EventBus, Topic
from astropi.core.geometry import AltAz, RaDec
from astropi.core.site import ObservingSite
from astropi.core.timekeeping import (
    SIDEREAL_RATE_DEG_PER_S,
    hour_angle_deg,
    local_sidereal_time_deg,
)
from astropi.devices.backends.synta.protocol import (
    SerialTransport,
    SyntaError,
    SyntaLink,
    Transport,
)
from astropi.devices.base import Capability, ConnectionState, DeviceDescriptor, DeviceRole
from astropi.devices.mount import (
    GuideDirection,
    MountState,
    MountStatus,
    PierSide,
    TrackingRate,
)

logger = logging.getLogger(__name__)

AXIS_RA = 1
AXIS_DEC = 2

#: Tracking rates as multiples of sidereal.
RATE_MULTIPLIERS = {
    TrackingRate.SIDEREAL: 1.0,
    TrackingRate.LUNAR: 0.9661,
    TrackingRate.SOLAR: 0.9973,
    TrackingRate.KING: 0.9998,
}


@dataclass(slots=True)
class SyntaMountConfig:
    port: str = "/dev/ttyACM0"
    baud: int = 9600
    device_id: str = "synta-mount"
    name: str = "Sky-Watcher mount"
    #: Guide pulse speed, as a multiple of sidereal.
    guide_rate: float = 0.5
    #: Goto speed, as a multiple of sidereal. 800 is what these
    #: controllers run at when nobody tells them otherwise - about 3.3
    #: degrees a second, and flat out. A motor asked for more torque than
    #: it has skips steps instead of moving, which is both audible and
    #: silent in the worst way: the counts keep incrementing while the
    #: axis stays put, so the mount's idea of where it is points at
    #: nothing. Half of maximum leaves room for a cold night and an
    #: unbalanced load.
    slew_rate: float = 400.0
    #: Use the controller's high-speed mode for fast slews.
    #:
    #: Off, until this mount's high-speed ratio is known to be honest.
    #: High speed is not a bigger number in the same units: the firmware
    #: switches to coarser stepping, and both the step period and the
    #: goto target then have to be divided by the ratio it reports. This
    #: mount reports 1, which would mean the mode does nothing at all -
    #: unlikely enough that trusting it risks driving an axis at sixteen
    #: times the speed asked for, which is a stall and a graunch.
    use_high_speed: bool = False
    #: How far before the target the controller starts slowing down, in
    #: counts. Sky-Watcher's own drivers set this on every goto; without
    #: it the axis arrives at full speed and stops dead.
    brake_counts: int = 3500
    #: At or above this rate, hand the move to the controller's own goto,
    #: which runs at its maximum and ramps itself. Below it, the move is
    #: driven at a constant rate and stopped here - see `_start_move`.
    native_goto_rate: float = 600.0
    #: Extra margin on the stopping point of a cruise, on top of the
    #: distance the axis covers between two polls.
    approach_deg: float = 0.05
    #: How often a cruising axis is checked. This interval times the
    #: speed is how far it travels blind, which is why the stop is
    #: decided ahead of the target rather than at it.
    cruise_poll_s: float = 0.1
    #: Refuse to point below this altitude. Pointing a telescope below the
    #: horizon means pointing it at the tripod, the pier or the wall, and
    #: a mount will do it without complaint. Lower it deliberately (-90
    #: disables the check) to exercise a mount indoors, where the sky it
    #: thinks it is looking at is fictional anyway.
    min_altitude_deg: float = 0.0
    #: How far either axis may sit from its home position. A bad sync can
    #: put a target hundreds of degrees away; this is the backstop.
    max_axis_deg: float = 185.0
    #: How long a goto may take before it is called a failure.
    slew_timeout_s: float = 180.0
    #: How close to home counts as being home, when working out on
    #: connect whether the mount is parked.
    home_tolerance_deg: float = 0.25
    #: Status is polled by the socket once a second per viewer, and four
    #: times a second while an axis is moving; this keeps the serial line
    #: from being asked the same question by each of them. Shorter than
    #: that fast poll, or every other update during a slew would be the
    #: previous one served again and the position would move in steps.
    status_cache_s: float = 0.15


class SyntaMount:
    """The `Mount` contract, spoken to a Sky-Watcher motor controller."""

    def __init__(
        self,
        site: ObservingSite,
        events: EventBus,
        config: SyntaMountConfig | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._site = site
        self._events = events
        self._config = config or SyntaMountConfig()
        #: Injected by the tests; real use opens the port on connect.
        self._transport = transport

        self._link: SyntaLink | None = None
        self._connection = ConnectionState.DISCONNECTED
        self._counts_per_rev: dict[int, int] = {}
        self._sidereal_period: dict[int, int] = {}
        self._timer_hz: dict[int, int] = {}
        #: What high-speed mode multiplies the stepping rate by. Read from
        #: the mount, because getting it wrong scales every fast slew.
        self._high_speed_ratio = 1
        self._version = 0

        # Where the sky is relative to the axes. Zero means "the mount was
        # at home pointing at the pole", which is true at power-on and
        # true again after a sync corrects it.
        self._ha_offset_deg = 0.0
        self._dec_offset_deg = 0.0

        self._parked = True
        self._tracking = False
        self._rate = TrackingRate.SIDEREAL
        self._target: RaDec | None = None
        self._slewing = False
        #: Whether the move in flight should end with the mount tracking.
        #: A goto should; going home should not, and neither should a
        #: nudge, which only puts back whatever was running before it.
        self._track_on_arrival = False
        self._cached: tuple[float, MountStatus] | None = None
        #: Axes being driven at a constant rate towards a count, because
        #: the controller's own goto will not run slowly.
        #: Axis -> (target counts, direction of travel).
        self._cruise: dict[int, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- device

    @property
    def descriptor(self) -> DeviceDescriptor:
        return DeviceDescriptor(
            id=self._config.device_id,
            role=DeviceRole.MOUNT,
            name=self._config.name,
            driver=f"synta serial ({self._config.port})",
            capabilities=frozenset(
                {
                    Capability.SLEW,
                    Capability.SYNC,
                    Capability.PARK,
                    Capability.TRACKING_RATES,
                    Capability.PULSE_GUIDE,
                }
            ),
            # Whatever the controller reported for its firmware. Kept as
            # hex because that is how Sky-Watcher quote it.
            details={"firmware": f"{self._version:06X}"} if self._version else {},
        )

    @property
    def connection_state(self) -> ConnectionState:
        return self._connection

    async def connect(self) -> None:
        self._connection = ConnectionState.CONNECTING
        try:
            transport = self._transport or await asyncio.to_thread(
                SerialTransport, self._config.port, self._config.baud
            )
            link = SyntaLink(transport)
            await asyncio.to_thread(self._interrogate, link)
        except Exception as error:
            self._connection = ConnectionState.ERROR
            raise DeviceError(f"could not talk to the mount on {self._config.port}: {error}") from error

        self._link = link
        self._connection = ConnectionState.CONNECTED
        # Asked, not assumed. The application restarts far more often than
        # the mount moves, and a restart that declares a mount parked
        # while it sits at the declination of Andromeda is describing its
        # own defaults rather than the rig.
        self._parked = await self._is_home()
        logger.info(
            "mount on %s: firmware %06X, %s counts/rev RA, %s counts/rev Dec",
            self._config.port,
            self._version,
            f"{self._counts_per_rev[AXIS_RA]:,}",
            f"{self._counts_per_rev[AXIS_DEC]:,}",
        )
        await self._publish()

    async def _is_home(self) -> bool:
        """Whether both axes are at the position the mount powers up in."""
        link = self._require_link()
        async with self._lock:
            ra_counts, dec_counts, _, _ = await asyncio.to_thread(self._read_axes, link)
        tolerance = self._config.home_tolerance_deg
        return (
            abs(self._axis_degrees(AXIS_RA, ra_counts)) < tolerance
            and abs(self._axis_degrees(AXIS_DEC, dec_counts)) < tolerance
        )

    def _interrogate(self, link: SyntaLink) -> None:
        """Learn the gearing, then wake the axes up.

        A freshly powered controller reports "not initialised" and refuses
        every motion command until `:F`, which is why this is part of
        connecting rather than part of the first slew.
        """
        self._version = link.version(AXIS_RA)
        self._high_speed_ratio = max(1, link.high_speed_ratio(AXIS_RA))
        for axis in (AXIS_RA, AXIS_DEC):
            self._counts_per_rev[axis] = link.counts_per_revolution(axis)
            self._timer_hz[axis] = link.timer_frequency(axis)
            self._sidereal_period[axis] = link.sidereal_period(axis)
            if not link.status(axis).initialised:
                link.initialise(axis)

    async def disconnect(self) -> None:
        link = self._link
        self._link = None
        self._connection = ConnectionState.DISCONNECTED
        if link is None:
            return
        # Stopped, not abandoned: a disconnect that leaves the motors
        # running is how a mount ends up against its own tripod.
        try:
            await asyncio.to_thread(self._stop_both, link)
        finally:
            await asyncio.to_thread(link.close)

    def _stop_both(self, link: SyntaLink) -> None:
        for axis in (AXIS_RA, AXIS_DEC):
            try:
                link.stop(axis)
            except SyntaError:
                logger.exception("could not stop axis %d", axis)

    # ------------------------------------------------------------- geometry

    def _require_link(self) -> SyntaLink:
        if self._link is None or self._connection is not ConnectionState.CONNECTED:
            raise NotConnectedError("the mount is not connected")
        return self._link

    def _axis_degrees(self, axis: int, counts: int) -> float:
        return counts * 360.0 / self._counts_per_rev[axis]

    def _axis_counts(self, axis: int, degrees: float) -> int:
        return round(degrees * self._counts_per_rev[axis] / 360.0)

    def _sky_from_axes(self, ra_axis_deg: float, dec_axis_deg: float, when: float) -> RaDec:
        """Where the telescope is pointing, from where the motors are.

        The model is deliberately the simplest one that can be corrected:
        the RA axis reads hour angle and the Dec axis reads the angle away
        from the pole, each with an offset that `sync_to` sets. Cone error,
        flexure and polar misalignment all land in those two offsets, which
        is exactly what a plate solve plus a sync is for.
        """
        hour_angle = ra_axis_deg + self._ha_offset_deg
        # Wrapped before it is folded. Without the wrap, an axis angle and
        # a sync offset that between them exceed 270 degrees produce a
        # declination outside +/-90 - which is not a pointing, it is a
        # ValueError out of the status endpoint.
        declination = _wrap180(90.0 - dec_axis_deg + self._dec_offset_deg)
        # Past the pole is the same direction seen from the other side.
        if declination > 90.0:
            declination = 180.0 - declination
            hour_angle += 180.0
        elif declination < -90.0:
            declination = -180.0 - declination
            hour_angle += 180.0
        lst = local_sidereal_time_deg(self._site.longitude_deg, when)
        return RaDec(ra_deg=(lst - hour_angle) % 360.0, dec_deg=declination)

    def _axes_from_sky(self, target: RaDec, when: float) -> tuple[float, float]:
        hour_angle = hour_angle_deg(target.ra_deg, self._site.longitude_deg, when)
        return (
            _wrap180(hour_angle - self._ha_offset_deg),
            90.0 - (target.dec_deg - self._dec_offset_deg),
        )

    # --------------------------------------------------------------- status

    async def status(self) -> MountStatus:
        cached = self._cached
        now = time.time()
        if cached is not None and now - cached[0] < self._config.status_cache_s:
            return cached[1]

        link = self._require_link()
        async with self._lock:
            ra_counts, dec_counts, ra_status, dec_status = await asyncio.to_thread(
                self._read_axes, link
            )

        position = self._sky_from_axes(
            self._axis_degrees(AXIS_RA, ra_counts),
            self._axis_degrees(AXIS_DEC, dec_counts),
            now,
        )
        # A goto has its own state; constant-rate motion is tracking, and
        # the mount reports both as "running".
        gotoing = (ra_status.running and not ra_status.slewing) or (
            dec_status.running and not dec_status.slewing
        )
        self._slewing = gotoing
        if gotoing:
            state = MountState.SLEWING
        elif self._parked:
            state = MountState.PARKED
        elif self._tracking:
            state = MountState.TRACKING
        else:
            state = MountState.IDLE

        status = MountStatus(
            state=state,
            position=position,
            horizontal=self._horizontal(position, now),
            target=self._target,
            tracking=self._tracking,
            tracking_rate=self._rate,
            # The GTi has no pier-side sensor, and guessing one from the
            # axis angle would be a guess reported as a fact.
            pier_side=PierSide.UNKNOWN,
            slewing=gotoing,
        )
        self._cached = (now, status)
        return status

    def _read_axes(self, link: SyntaLink):
        return (
            link.position(AXIS_RA),
            link.position(AXIS_DEC),
            link.status(AXIS_RA),
            link.status(AXIS_DEC),
        )

    def _horizontal(self, position: RaDec, when: float) -> AltAz:
        hour_angle = math.radians(hour_angle_deg(position.ra_deg, self._site.longitude_deg, when))
        dec = math.radians(position.dec_deg)
        lat = math.radians(self._site.latitude_deg)
        sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(hour_angle)
        altitude = math.asin(max(-1.0, min(1.0, sin_alt)))
        azimuth = math.atan2(
            -math.cos(dec) * math.cos(lat) * math.sin(hour_angle),
            math.sin(dec) - math.sin(altitude) * math.sin(lat),
        )
        return AltAz(alt_deg=math.degrees(altitude), az_deg=math.degrees(azimuth) % 360.0)

    # ---------------------------------------------------------------- moving

    async def slew_to(self, target: RaDec) -> None:
        link = self._require_link()
        if self._parked:
            raise DeviceError("the mount is parked")

        now = time.time()
        self._check_reachable(target, now)
        ra_target_deg, dec_target_deg = self._axes_from_sky(target, now)
        async with self._lock:
            current = await asyncio.to_thread(self._read_axes, link)
            ra_now = self._axis_degrees(AXIS_RA, current[0])
            dec_now = self._axis_degrees(AXIS_DEC, current[1])

            # The short way round, always. Both axes turn freely through
            # home, so there is never a reason to take the long way.
            moves = {
                AXIS_RA: _wrap180(ra_target_deg - ra_now),
                AXIS_DEC: _wrap180(dec_target_deg - dec_now),
            }

            self._target = target
            self._slewing = True
            # Having slewed to a coordinate, staying on it is the only
            # useful thing left to do - and a mount that arrives and then
            # stands still lets the target drift straight back out of the
            # frame at fifteen arcminutes a minute. The simulator has
            # always done this; the real one was not, and the centring
            # loop was measuring its own drift as a pointing error.
            self._track_on_arrival = True
            await asyncio.to_thread(self._start_goto, link, moves)

        self._cached = None
        await self._publish()

    def _check_reachable(self, target: RaDec, when: float) -> None:
        """Refuse what the mount would happily do to itself.

        A mount has no idea where the ground is. Both of these have the
        same cause - a sync or a site that is wrong - and the same
        symptom if they are not caught: an expensive noise.
        """
        altitude = self._horizontal(target, when).alt_deg
        if altitude < self._config.min_altitude_deg:
            raise DeviceError(
                f"refusing to point at altitude {altitude:.1f} degrees, below the "
                f"{self._config.min_altitude_deg:.0f} degree limit - that is the ground. "
                "Check the site and the sync, or lower the limit deliberately."
            )

        _, dec_axis = self._axes_from_sky(target, when)
        if abs(_wrap180(dec_axis)) > self._config.max_axis_deg:
            raise DeviceError(
                f"refusing a declination axis target of {dec_axis:.0f} degrees - "
                f"beyond the {self._config.max_axis_deg:.0f} degree limit. "
                "The mount has probably been synced to the wrong star."
            )

    def _start_goto(self, link: SyntaLink, moves: dict[int, float]) -> None:
        """Stop, aim, go - the order the controller insists on.

        Changing motion mode while an axis is running is rejected with
        "motor not stopped", so every goto begins by stopping even when
        nothing is moving.
        """
        rate = max(1.0, self._config.slew_rate)
        fast = self._config.use_high_speed
        self._cruise = {}
        for axis, degrees in moves.items():
            counts = self._axis_counts(axis, degrees)
            link.stop(axis)
            self._await_stopped(link, axis)
            if counts == 0:
                continue
            if rate < self._config.native_goto_rate:
                self._start_cruise(link, axis, counts, rate)
                continue
            period = self._goto_period(axis, rate, fast=fast)
            link.set_motion_mode(axis, goto=True, fast=fast, backward=counts < 0)
            # Said out loud, rather than left to whatever the controller
            # was doing last. Without this the goto ran at the board's own
            # maximum and there was no way to ask for anything else.
            link.set_step_period(axis, period)
            link.set_goto_target(axis, counts)
            try:
                link.set_brake_increment(
                    axis, min(self._config.brake_counts, abs(counts) // 4 + 1)
                )
            except SyntaError:
                # Not every firmware has it, and a goto without a brake
                # point still arrives - it just stops harder.
                logger.debug("axis %d will not take a brake point", axis)
            link.start(axis)
            # Logged per move, because the only way to tell a mount that
            # is merely loud from one that is stalling is to compare the
            # speed it was asked for with the speed it managed.
            logger.info(
                "axis %d goto %+0.3f deg (%+d counts) at %.0fx sidereal, "
                "period %d%s, expect %.1fs",
                axis,
                degrees,
                counts,
                rate,
                period,
                " high-speed" if fast else "",
                abs(degrees) / max(rate * SIDEREAL_RATE_DEG_PER_S, 1e-6),
            )

        # Tracking is a constant-rate motion and a goto is not, so the
        # controller drops tracking when it starts one. It is restored
        # when the goto finishes.

    def _read_positions(self, link: SyntaLink) -> dict[int, int]:
        return {axis: link.position(axis) for axis in (AXIS_RA, AXIS_DEC)}

    async def _log_measured_rate(
        self, link: SyntaLink, before: dict[int, int], elapsed: float
    ) -> None:
        """What the axes actually did, against what they were asked for.

        The controller reports the steps it *sent*, so this cannot catch a
        motor that skipped - but it does catch the other half: an axis
        running at a wholly different speed from the one commanded, which
        is what a lying high-speed ratio looks like from here.
        """
        if elapsed <= 0.05:
            return
        after = await asyncio.to_thread(self._read_positions, link)
        for axis, counts in after.items():
            travelled = self._axis_degrees(axis, counts - before[axis])
            if abs(travelled) < 1e-4:
                continue
            asked = self._config.slew_rate * SIDEREAL_RATE_DEG_PER_S
            logger.info(
                "axis %d moved %+0.3f deg in %.1fs = %.3f deg/s (asked %.3f, ratio %.2f)",
                axis,
                travelled,
                elapsed,
                abs(travelled) / elapsed,
                asked,
                abs(travelled) / elapsed / max(asked, 1e-6),
            )

    def _start_cruise(self, link: SyntaLink, axis: int, counts: int, rate: float) -> None:
        """Move at a chosen speed, by driving the axis rather than aiming it.

        The controller's own goto ignores the step period and ramps to its
        maximum - measured at four degrees a second on this mount, against
        the 0.8 it was asked for. Constant-rate mode does respect the
        period, because that is how tracking holds sidereal to the tick.

        So a slow move is a constant-rate run with the stopping done here:
        start the axis, watch the counts, stop short, and let a small
        native goto cover the last fraction of a degree.
        """
        target = link.position(axis) + counts
        # The direction is kept, not inferred later: the test for arrival
        # is "has it got there yet", which needs to know which way "yet"
        # is. Asking whether the remaining distance is *small* instead
        # gives a window that an axis moving 16,000 counts a second jumps
        # straight over between two polls, and it never stops at all.
        self._cruise[axis] = (target, 1 if counts > 0 else -1)
        link.set_motion_mode(axis, goto=False, fast=False, backward=counts < 0)
        link.set_step_period(axis, self._goto_period(axis, rate, fast=False))
        link.start(axis)
        logger.info(
            "axis %d cruising %+0.3f deg at %.0fx sidereal (%.2f deg/s)",
            axis,
            self._axis_degrees(axis, counts),
            rate,
            rate * SIDEREAL_RATE_DEG_PER_S,
        )

    def _cruise_lead(self, axis: int) -> int:
        """How far ahead of the target to call stop, in counts.

        Everything the axis covers between two polls, half as much again
        for the serial round trips, plus a fixed margin. Stopping early
        and correcting with a short goto beats stopping late and having
        to reverse, which on a mount with backlash is a worse error than
        the one being fixed.
        """
        per_second = (
            self._counts_per_rev[axis] * self._config.slew_rate * SIDEREAL_RATE_DEG_PER_S / 360.0
        )
        margin = self._axis_counts(axis, self._config.approach_deg)
        return int(per_second * self._config.cruise_poll_s * 1.5 + margin)

    def _advance_cruise(self, link: SyntaLink) -> bool:
        """Stop any cruising axis that has arrived. True while any remain.

        Runs in a worker thread, because every line of it is a serial
        round trip and the event loop has a dashboard to feed.
        """
        for axis, (target, direction) in list(self._cruise.items()):
            remaining = direction * (target - link.position(axis))
            if remaining > self._cruise_lead(axis):
                continue
            link.stop(axis)
            self._await_stopped(link, axis)
            # Whatever the axis carried past the stop is taken out by a
            # short goto: small enough that the controller's maximum speed
            # over it is a twitch, and precise because it counts.
            final = target - link.position(axis)
            if abs(self._axis_degrees(axis, final)) > 0.001:
                link.set_motion_mode(axis, goto=True, fast=False, backward=final < 0)
                link.set_goto_target(axis, final)
                link.start(axis)
            del self._cruise[axis]
        return bool(self._cruise)

    def _goto_period(self, axis: int, rate: float, *, fast: bool) -> int:
        """Timer ticks per step for a slew at `rate` times sidereal.

        In high-speed mode the controller multiplies its own stepping by
        the ratio it reports, so the period asked for has to be divided by
        it or the axis runs that many times too fast.
        """
        divisor = rate / self._high_speed_ratio if fast else rate
        return max(1, round(self._sidereal_period[axis] / max(divisor, 1e-6)))

    def slew_degrees_per_second(self) -> float:
        """What the configured rate works out as on the sky."""
        return self._config.slew_rate * SIDEREAL_RATE_DEG_PER_S

    def _await_stopped(self, link: SyntaLink, axis: int, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not link.status(axis).running:
                return
            time.sleep(0.05)
        raise DeviceError(f"axis {axis} would not stop")

    async def wait_for_slew(self, *, timeout_s: float = 120.0) -> None:
        link = self._require_link()
        started = time.monotonic()
        start_counts = await asyncio.to_thread(self._read_positions, link)
        deadline = time.monotonic() + min(timeout_s, self._config.slew_timeout_s)
        while time.monotonic() < deadline:
            async with self._lock:
                # A cruising axis is stopped from here, not by the
                # controller: it was told a speed, not a destination.
                if self._cruise:
                    await asyncio.to_thread(self._advance_cruise, link)
                ra, dec = await asyncio.to_thread(
                    lambda: (link.status(AXIS_RA), link.status(AXIS_DEC))
                )
            # Only a goto counts as "still slewing". An axis running at a
            # constant rate is tracking, and waiting for *that* to stop is
            # waiting forever - which is what a declination nudge did with
            # tracking on, until the slew timeout gave up 180 seconds later.
            if not self._cruise and not _gotoing(ra) and not _gotoing(dec):
                self._slewing = False
                self._cached = None
                await self._log_measured_rate(link, start_counts, time.monotonic() - started)
                # A goto cancels tracking, so it is started again here:
                # either because the mount was tracking before the move -
                # a nudge must not silently stop it - or because the move
                # was a goto, which ends with the target under the sky.
                if self._track_on_arrival:
                    self._tracking = True
                self._track_on_arrival = False
                if self._tracking:
                    await self._apply_tracking(True)
                await self._publish()
                return
            # Fast while cruising: the stop is decided here, so the poll
            # interval is the overshoot. A tenth of a second at a degree a
            # second is six arcminutes, which the final hop then removes.
            await asyncio.sleep(self._config.cruise_poll_s if self._cruise else 0.25)
        raise DeviceError("the slew did not finish in time")

    async def abort_slew(self) -> None:
        link = self._require_link()
        self._cruise.clear()
        async with self._lock:
            await asyncio.to_thread(self._stop_both, link)
        self._slewing = False
        self._target = None
        self._cached = None
        # An abort is a decision to stop, so it does not inherit the
        # goto's intention to be tracking at the end of it.
        self._track_on_arrival = False
        if self._tracking:
            await self._apply_tracking(True)
        await self._publish()

    async def sync_to(self, actual: RaDec) -> None:
        """Move the model, never the mount.

        The axes stay exactly where they are; what changes is this
        object's idea of which patch of sky they are pointing at.
        """
        link = self._require_link()
        now = time.time()
        async with self._lock:
            ra_counts, dec_counts, _, _ = await asyncio.to_thread(self._read_axes, link)

        ra_axis = self._axis_degrees(AXIS_RA, ra_counts)
        dec_axis = self._axis_degrees(AXIS_DEC, dec_counts)
        self._ha_offset_deg = _wrap180(
            hour_angle_deg(actual.ra_deg, self._site.longitude_deg, now) - ra_axis
        )
        self._dec_offset_deg = _wrap180(actual.dec_deg - (90.0 - dec_axis))
        self._cached = None
        await self._publish()

    # -------------------------------------------------------------- tracking

    async def set_tracking(self, enabled: bool, rate: TrackingRate = TrackingRate.SIDEREAL) -> None:
        if enabled and self._parked:
            raise DeviceError("the mount is parked")
        self._rate = rate
        self._tracking = enabled
        await self._apply_tracking(enabled)
        await self._publish()

    async def _apply_tracking(self, enabled: bool) -> None:
        link = self._require_link()
        multiplier = self._rate_multiplier() if enabled else 0.0
        async with self._lock:
            await asyncio.to_thread(self._set_axis_rate, link, AXIS_RA, multiplier)
        self._cached = None

    def _rate_multiplier(self) -> float:
        return RATE_MULTIPLIERS.get(self._rate, 1.0)

    def _set_axis_rate(self, link: SyntaLink, axis: int, multiplier: float) -> None:
        """Run an axis at a multiple of sidereal, or stop it.

        The step period is the mount's own sidereal figure divided by the
        multiplier, so the rate is exact to whatever the controller's
        crystal is - no reimplementation of the gearing, and no rounding
        error accumulating over an hour of tracking.
        """
        link.stop(axis)
        self._await_stopped(link, axis)
        if abs(multiplier) < 1e-6:
            return
        period = max(1, round(self._sidereal_period[axis] / abs(multiplier)))
        link.set_motion_mode(axis, goto=False, fast=False, backward=multiplier < 0)
        link.set_step_period(axis, period)
        link.start(axis)

    # ----------------------------------------------------------- park / home

    async def park(self) -> None:
        """Back to the position the mount powers up in, then stop.

        Home is where the counters read zero, which for an equatorial head
        is counterweight down and the telescope on the polar axis - the
        position it is safe to leave a mount in, and the only one this
        controller knows by heart.
        """
        link = self._require_link()
        self._tracking = False
        self._track_on_arrival = False
        await self._apply_tracking(False)

        async with self._lock:
            ra_counts, dec_counts, _, _ = await asyncio.to_thread(self._read_axes, link)
            moves = {
                AXIS_RA: -self._axis_degrees(AXIS_RA, ra_counts),
                AXIS_DEC: -self._axis_degrees(AXIS_DEC, dec_counts),
            }
            await asyncio.to_thread(self._start_goto, link, moves)

        await self.wait_for_slew(timeout_s=self._config.slew_timeout_s)
        self._parked = True
        self._target = None
        self._cached = None
        await self._publish()

    async def unpark(self) -> None:
        self._parked = False
        self._cached = None
        await self._publish()

    # --------------------------------------------------------------- moving

    async def move_by(self, direction: GuideDirection, degrees: float) -> None:
        """A framing move: a relative goto on one axis.

        A goto rather than a timed run at some rate, because the
        controller ramps a goto up and down by itself and stops on the
        count - so a degree is a degree, at whatever speed the mount can
        manage, instead of a guess about how long to leave a motor on.
        """
        link = self._require_link()
        if self._parked:
            raise DeviceError("the mount is parked")

        travel = abs(degrees)
        if travel > self._config.max_axis_deg:
            raise DeviceError(f"a {travel:.0f} degree nudge is not a nudge")

        if direction in (GuideDirection.EAST, GuideDirection.WEST):
            # The RA axis reads hour angle, and hour angle increases
            # westward - so west is the direction that increases it.
            axis = AXIS_RA
            signed = travel if direction is GuideDirection.WEST else -travel
        else:
            # Declination is measured from the pole the other way: the
            # axis angle is 90 minus the declination, so north is down.
            axis = AXIS_DEC
            signed = -travel if direction is GuideDirection.NORTH else travel

        async with self._lock:
            await asyncio.to_thread(self._start_goto, link, {axis: signed})
        self._cached = None
        await self.wait_for_slew(timeout_s=self._config.slew_timeout_s)

    # -------------------------------------------------------------- guiding

    async def pulse_guide(self, direction: GuideDirection, duration_ms: int) -> None:
        """A fixed-length nudge, at the guide rate.

        Right ascension is corrected by running the tracking axis faster or
        slower rather than by stopping and reversing it: a guide correction
        that stops the axis loses the worm's tooth contact and comes back
        as backlash on the next frame.
        """
        link = self._require_link()
        if self._parked:
            raise DeviceError("the mount is parked")
        seconds = max(0.0, duration_ms / 1000.0)
        rate = self._config.guide_rate

        if direction in (GuideDirection.EAST, GuideDirection.WEST):
            base = self._rate_multiplier() if self._tracking else 0.0
            # West speeds the axis up, east slows it down - the same
            # convention as an ST-4 port, so a guider calibrated against
            # one behaves the same against the other.
            adjusted = base + (rate if direction is GuideDirection.WEST else -rate)
            await self._pulse_axis(link, AXIS_RA, adjusted, seconds, base)
        else:
            sign = 1.0 if direction is GuideDirection.NORTH else -1.0
            await self._pulse_axis(link, AXIS_DEC, sign * rate, seconds, 0.0)

    async def _pulse_axis(
        self, link: SyntaLink, axis: int, multiplier: float, seconds: float, restore: float
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_axis_rate, link, axis, multiplier)
        try:
            await asyncio.sleep(seconds)
        finally:
            async with self._lock:
                await asyncio.to_thread(self._set_axis_rate, link, axis, restore)
            self._cached = None

    # --------------------------------------------------------------- events

    async def _publish(self) -> None:
        try:
            status = await self.status()
        except Exception:
            logger.exception("could not read the mount for a position update")
            return
        self._events.publish(
            Topic.MOUNT_POSITION,
            state=str(status.state),
            ra_deg=status.position.ra_deg,
            dec_deg=status.position.dec_deg,
            alt_deg=status.horizontal.alt_deg if status.horizontal else None,
            az_deg=status.horizontal.az_deg if status.horizontal else None,
            tracking=status.tracking,
        )

    # -------------------------------------------------------------- pointing

    def set_slew_rate(self, multiplier: float) -> None:
        """Change the goto speed, in multiples of sidereal.

        Takes effect on the next goto rather than the one in flight: the
        controller is told the period when a move starts, and changing it
        mid-ramp is how a motor is made to skip.
        """
        self._config.slew_rate = max(1.0, multiplier)

    def set_site(self, site: ObservingSite) -> None:
        """Where the mount is standing. Everything celestial depends on it."""
        self._site = site
        self._cached = None

    def true_position(self) -> RaDec:
        """Where the mount believes it is pointing, without asking it again.

        The simulated camera renders from this, so it has to be cheap and
        synchronous - a serial round trip per frame would slow the preview
        loop to the speed of a 9600 baud line. It returns the last polled
        position, which the status poller refreshes about once a second.

        On real hardware there is no such thing as the *true* position -
        that is what plate solving is for - so this is the mount's own
        claim, which is the best anything here can know.
        """
        if self._cached is not None:
            return self._cached[1].position
        # Nothing polled yet: the pole is where a Sky-Watcher wakes up.
        return RaDec(
            ra_deg=local_sidereal_time_deg(self._site.longitude_deg, time.time()) % 360.0,
            dec_deg=90.0,
        )

    # ------------------------------------------------------------ diagnostics

    async def report(self) -> dict:
        """What the controller says about itself, for the setup panel."""
        link = self._require_link()
        async with self._lock:
            ra_counts, dec_counts, ra_status, dec_status = await asyncio.to_thread(
                self._read_axes, link
            )
        return {
            "firmware": f"{self._version:06X}",
            "port": self._config.port,
            "counts_per_revolution": dict(self._counts_per_rev),
            "sidereal_period": dict(self._sidereal_period),
            "timer_hz": dict(self._timer_hz),
            "high_speed_ratio": self._high_speed_ratio,
            "slew_rate": self._config.slew_rate,
            "slew_deg_per_s": round(self.slew_degrees_per_second(), 3),
            "axis_deg": {
                "ra": round(self._axis_degrees(AXIS_RA, ra_counts), 4),
                "dec": round(self._axis_degrees(AXIS_DEC, dec_counts), 4),
            },
            "initialised": {
                "ra": ra_status.initialised,
                "dec": dec_status.initialised,
            },
            "running": {"ra": ra_status.running, "dec": dec_status.running},
        }


def _gotoing(status) -> bool:
    """Running, and not merely turning at a constant rate."""
    return status.running and not status.slewing


def _wrap180(degrees: float) -> float:
    """Fold an angle into (-180, 180], the short way round."""
    return (degrees + 180.0) % 360.0 - 180.0


def sidereal_arcsec_per_second() -> float:
    """Exposed for tests and for the setup panel's sanity check."""
    return SIDEREAL_RATE_DEG_PER_S * 3600.0
