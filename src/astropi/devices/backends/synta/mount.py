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
    AxisStatus,
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
#: The longest step period the protocol can carry: about a step a second.
MAX_STEP_PERIOD = 0xFFFFFF

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
    #: Declination moves up to this many steps are crept, not goto'd: the
    #: controller's own goto was measured ignoring every move of one to
    #: four steps (1,929 of 1,929) and landing five-step ones anywhere
    #: from four to seven.
    creep_max_steps: int = 40
    #: Speed of a creep, in multiples of sidereal: half, about 60 ms a
    #: step, slow enough for a serial poll to stop it within one.
    creep_rate: float = 0.5
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
    #: How often the watchdog reads both axes, in seconds; `None` turns it
    #: off. It is what stops an axis this code did not set moving - one
    #: that ignored a stop, or that something else is driving.
    watchdog_period_s: float | None = 1.0
    #: The fastest either axis may turn with no move of ours under way,
    #: in multiples of sidereal: tracking, plus guiding's offset, with room.
    watchdog_max_rate: float = 3.0
    #: How long a stopped axis may keep reporting that it is running before
    #: the stop is sent again - and, at the end of the wait, before both
    #: axes are emergency-stopped and the mount refuses to move again.
    stop_retry_s: float = 1.5
    stop_timeout_s: float = 5.0
    #: How far past the meridian a GoTo may take the counterweight above
    #: horizontal, in degrees of RA axis, to stay on the side of the mount
    #: it is already on rather than flip for a target just across it.
    meridian_margin_deg: float = 10.0


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
        #: Counts per turn of the RA worm, where the firmware says.
        self._steps_per_worm: int | None = None
        self._version = 0

        # Where the sky is relative to the axes. Zero means "the mount was
        # at home pointing at the pole", which is true at power-on and
        # true again after a sync corrects it.
        self._ha_offset_deg = 0.0
        self._dec_offset_deg = 0.0

        self._parked = True
        self._tracking = False
        self._rate = TrackingRate.SIDEREAL
        #: Guiding's offset to the RA tracking rate, in axis arcsec/s.
        self._ra_offset = 0.0
        self._target: RaDec | None = None
        self._slewing = False
        #: Whether the move in flight should end with the mount tracking.
        #: A goto should; going home should not, and neither should a
        #: nudge, which only puts back whatever was running before it.
        self._track_on_arrival = False
        self._cached: tuple[float, MountStatus] | None = None
        #: Where the axes physically point, sync model aside - kept when the
        #: cache is dropped, which means "read again", not "no idea where
        #: it is": the simulated camera took that as licence to draw the
        #: sky at the pole.
        self._physical_position: RaDec | None = None
        #: Axes being driven at a constant rate towards a count, because
        #: the controller's own goto will not run slowly.
        #: Axis -> (target counts, direction of travel).
        self._cruise: dict[int, tuple[int, int]] = {}
        self._lock = asyncio.Lock()
        #: Why the mount halted itself, while it refuses to move. Set by
        #: `_halt`, cleared only by `clear_fault` or a fresh connection.
        self._fault: str | None = None
        #: Axes moving because this code moved them, and how: "goto" (the
        #: controller's own, at its own speed), "cruise", "step" or
        #: "pulse". Anything else an axis does is the watchdog's business.
        self._expect: dict[int, str] = {}
        self._watchdog: asyncio.Task[None] | None = None
        #: Per axis: the watchdog's last reading - when, counts, and what
        #: move was expected then - and how many readings in a row the axis
        #: has been turning with no move expected.
        self._watch_last: dict[int, tuple[float, int, str | None]] = {}
        self._strikes: dict[int, int] = {}
        #: The declination counter as last read, for deciding which way is
        #: north without a serial round trip in the middle of a guide pulse.
        self._last_dec_counts = 0

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
                    Capability.GUIDE_RATE_OFFSET,
                    Capability.AXIS_STEPS,
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
        # A fresh conversation with a controller that has just been asked
        # everything again: whatever halted the last one is for the
        # operator to have dealt with, which reconnecting is.
        self._fault = None
        self._expect.clear()
        self._watch_last.clear()
        self._strikes.clear()
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
        if self._config.watchdog_period_s and (self._watchdog is None or self._watchdog.done()):
            self._watchdog = asyncio.create_task(self._watch())
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
        try:
            self._steps_per_worm = link.steps_per_worm(AXIS_RA) or None
        except SyntaError:
            logger.info("this controller does not say how many steps its worm turns")
            self._steps_per_worm = None
        for axis in (AXIS_RA, AXIS_DEC):
            self._counts_per_rev[axis] = link.counts_per_revolution(axis)
            self._timer_hz[axis] = link.timer_frequency(axis)
            self._sidereal_period[axis] = link.sidereal_period(axis)
            if not link.status(axis).initialised:
                link.initialise(axis)

    async def disconnect(self) -> None:
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()
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

    def _require_motion(self) -> SyntaLink:
        """The link, for a command that moves an axis - refused after a fault."""
        link = self._require_link()
        if self._fault is not None:
            raise DeviceError(
                f"the mount halted itself: {self._fault}. "
                "Check it over, then clear the fault to move it again."
            )
        return link

    def _axis_degrees(self, axis: int, counts: int) -> float:
        return counts * 360.0 / self._counts_per_rev[axis]

    def _axis_counts(self, axis: int, degrees: float) -> int:
        return round(degrees * self._counts_per_rev[axis] / 360.0)

    def _sky_from_axes(
        self, ra_axis_deg: float, dec_axis_deg: float, when: float, *, synced: bool = True
    ) -> RaDec:
        """Where the telescope is pointing, from where the motors are.

        German-equatorial geometry (`gem_sky`), then the two offsets that
        `sync_to` sets. Cone error, flexure and polar misalignment all land
        in those offsets, which is exactly what a plate solve plus a sync
        is for.
        """
        hour_angle, declination, _ = gem_sky(ra_axis_deg, dec_axis_deg)
        if synced:
            hour_angle, declination = _fold(
                hour_angle + self._ha_offset_deg, declination + self._dec_offset_deg
            )
        lst = local_sidereal_time_deg(self._site.longitude_deg, when)
        return RaDec(ra_deg=(lst - hour_angle) % 360.0, dec_deg=declination)

    def _axes_from_sky(
        self, target: RaDec, when: float, *, current_dec_axis: float | None = None
    ) -> tuple[float, float]:
        """The axis angles that point at `target`, on the side of the mount
        that keeps the counterweight down - or on the side it is already
        on, while that is still within `meridian_margin_deg` of it, so a
        target just across the meridian does not cost a flip."""
        hour_angle, declination = _fold(
            hour_angle_deg(target.ra_deg, self._site.longitude_deg, when) - self._ha_offset_deg,
            target.dec_deg - self._dec_offset_deg,
        )
        side = gem_side_for(hour_angle)
        if current_dec_axis is not None and abs(current_dec_axis) > 1e-6:
            current = PierSide.EAST if current_dec_axis > 0 else PierSide.WEST
            ra_axis, _ = gem_axes(hour_angle, declination, current)
            if abs(ra_axis) <= 90.0 + self._config.meridian_margin_deg:
                side = current
        return gem_axes(hour_angle, declination, side)

    def _north_sign(self, dec_counts: int) -> int:
        """Which way declination counts run to go north, on the side of the
        mount the telescope is on now. With the telescope on the east side
        (Dec axis positive) declination is 90 minus the axis angle, so north
        is down in counts; on the west side it is 90 plus it, and north is up."""
        return 1 if dec_counts < 0 else -1

    # --------------------------------------------------------------- status

    async def status(self) -> MountStatus:
        cached = self._cached
        now = time.time()
        if cached is not None and now - cached[0] < self._config.status_cache_s:
            return cached[1]

        link = self._require_link()
        async with self._lock:
            ra_counts, dec_counts, ra_status, dec_status, read_at = await asyncio.to_thread(
                self._read_axes_timed, link
            )

        ra_axis = self._axis_degrees(AXIS_RA, ra_counts)
        dec_axis = self._axis_degrees(AXIS_DEC, dec_counts)
        position = self._sky_from_axes(ra_axis, dec_axis, read_at)
        _, _, pier_side = gem_sky(ra_axis, dec_axis)
        # The same axes without the sync model: where the motors physically
        # point, which is what a sync must not change.
        self._physical_position = self._sky_from_axes(ra_axis, dec_axis, read_at, synced=False)
        # A goto has its own state; constant-rate motion is tracking, and
        # the mount reports both as "running".
        gotoing = (ra_status.running and not ra_status.slewing) or (
            dec_status.running and not dec_status.slewing
        )
        self._slewing = gotoing
        if self._fault is not None:
            state = MountState.ERROR
        elif gotoing:
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
            # No sensor: geometry. Which side the telescope is on follows
            # from which way the Dec axis has turned from home.
            pier_side=pier_side,
            slewing=gotoing,
            fault=self._fault,
        )
        self._cached = (now, status)
        return status

    def _read_axes(self, link: SyntaLink):
        return self._read_axes_timed(link)[:4]

    def _read_axes_timed(self, link: SyntaLink):
        """The axes, and the moment the RA counter was actually read.

        Right ascension turns at fifteen arcseconds a second, so turning
        its count into a sky position needs the time *that count* was
        read - not the time the caller started waiting for the serial
        line. A read queued behind a declination move came back 0.3 s
        after that, and the position it gave was 4.5" out; ordinary 9600
        baud round trips put 1-2" of jitter on every other one. The
        simulated camera draws from this position, so the star jumped
        about on the guide frame with nothing moving.
        """
        before = time.time()
        ra = link.position(AXIS_RA)
        when = (before + time.time()) / 2.0
        dec = link.position(AXIS_DEC)
        self._last_dec_counts = dec
        return (ra, dec, link.status(AXIS_RA), link.status(AXIS_DEC), when)

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
        link = self._require_motion()
        if self._parked:
            raise DeviceError("the mount is parked")
        # Guiding's rate offset belongs to the star it was guiding on.
        self._ra_offset = 0.0

        self._check_reachable(target, time.time())
        async with self._lock:
            current = await asyncio.to_thread(self._read_axes_timed, link)
            ra_now = self._axis_degrees(AXIS_RA, current[0])
            dec_now = self._axis_degrees(AXIS_DEC, current[1])
            # Aimed from the moment the counters were read, not from before
            # waiting for the line: the target's hour angle moves on.
            ra_target_deg, dec_target_deg = self._axes_from_sky(target, current[4], current_dec_axis=dec_now)
            for axis, angle in ((AXIS_RA, ra_target_deg), (AXIS_DEC, dec_target_deg)):
                if abs(angle) > self._config.max_axis_deg:
                    raise DeviceError(
                        f"refusing an axis {axis} target of {angle:.0f} degrees from home - "
                        f"beyond the {self._config.max_axis_deg:.0f} degree limit"
                    )

            # Straight from here to there, never "the short way round": the
            # short way can pass the counterweight over the top, and the
            # cables with it. Targets are chosen to keep it down, so the
            # direct path does too.
            moves = {
                AXIS_RA: ra_target_deg - ra_now,
                AXIS_DEC: dec_target_deg - dec_now,
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
        if abs(dec_axis) > self._config.max_axis_deg:
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
                self._expect[axis] = "cruise"
                self._start_cruise(link, axis, counts, rate)
                continue
            self._expect[axis] = "goto"
            period = self._goto_period(axis, rate, fast=fast)
            link.set_motion_mode(axis, goto=True, fast=fast, backward=counts < 0)
            # Said out loud, rather than left to whatever the controller
            # was doing last. Without this the goto ran at the board's own
            # maximum and there was no way to ask for anything else.
            link.set_step_period(axis, period)
            link.set_goto_target(axis, counts)
            try:
                link.set_brake_increment(axis, min(self._config.brake_counts, abs(counts) // 4 + 1))
            except SyntaError:
                # Not every firmware has it, and a goto without a brake
                # point still arrives - it just stops harder.
                logger.debug("axis %d will not take a brake point", axis)
            link.start(axis)
            # Logged per move, because the only way to tell a mount that
            # is merely loud from one that is stalling is to compare the
            # speed it was asked for with the speed it managed.
            logger.info(
                "axis %d goto %+0.3f deg (%+d counts) at %.0fx sidereal, period %d%s, expect %.1fs",
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

    async def _log_measured_rate(self, link: SyntaLink, before: dict[int, int], elapsed: float) -> None:
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
        per_second = self._counts_per_rev[axis] * self._config.slew_rate * SIDEREAL_RATE_DEG_PER_S / 360.0
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
                self._expect[axis] = "goto"
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

    def _await_stopped(self, link: SyntaLink, axis: int, timeout_s: float | None = None) -> None:
        """Wait for an axis that has been told to stop - and make it.

        An axis that is still turning after a stop is not a slow axis; it
        is a runaway. The stop is sent again, and if the axis still turns,
        both are emergency-stopped and the mount refuses to move until the
        fault is cleared. Raising and walking away is what let a
        declination axis spin at full speed for half an hour: nothing was
        left watching it.
        """
        timeout_s = self._config.stop_timeout_s if timeout_s is None else timeout_s
        started = time.monotonic()
        retried = False
        while time.monotonic() - started < timeout_s:
            if not link.status(axis).running:
                return
            if not retried and time.monotonic() - started > self._config.stop_retry_s:
                logger.warning(
                    "axis %d still running %.1fs after a stop; stopping it again",
                    axis,
                    self._config.stop_retry_s,
                )
                link.stop(axis)
                retried = True
            # Short, because this sits in the middle of a guide pulse on
            # the rare path that still has to stop an axis, and every
            # millisecond of it is sky the tracking axis is not following.
            time.sleep(0.02)
        self._halt(link, f"axis {axis} kept turning for {timeout_s:.0f} s after being told to stop")
        raise DeviceError(f"the mount halted itself: {self._fault}")

    def _halt(self, link: SyntaLink, reason: str) -> None:
        """Stop both axes by every means the controller has, and refuse to
        move again until told.

        An emergency stop on each axis first. If one is still turning after
        that, its step period goes to the longest there is - which slows a
        constant-rate run to a step every second or so - and the fault says
        to cut the power, because nothing on the serial line is being obeyed.
        """
        self._fault = reason
        self._tracking = False
        self._track_on_arrival = False
        self._ra_offset = 0.0
        self._slewing = False
        self._cruise.clear()
        self._expect.clear()
        self._cached = None
        logger.error("mount halted: %s", reason)
        for axis in (AXIS_RA, AXIS_DEC):
            try:
                link.stop_now(axis)
            except SyntaError:
                logger.exception("emergency stop of axis %d was refused", axis)
        still = {AXIS_RA, AXIS_DEC}
        deadline = time.monotonic() + 2.0
        while still and time.monotonic() < deadline:
            for axis in list(still):
                try:
                    if not link.status(axis).running:
                        still.discard(axis)
                except SyntaError:
                    pass
            time.sleep(0.05)
        for axis in sorted(still):
            try:
                link.set_step_period(axis, MAX_STEP_PERIOD)
                link.stop_now(axis)
            except SyntaError:
                logger.exception("could not slow axis %d", axis)
            logger.critical("axis %d is still turning after an emergency stop - cut the mount's power", axis)
            self._fault = (
                f"{reason}; axis {axis} kept turning after an emergency stop - cut the mount's power"
            )

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
                ra, dec = await asyncio.to_thread(lambda: (link.status(AXIS_RA), link.status(AXIS_DEC)))
            # Only a goto counts as "still slewing". An axis running at a
            # constant rate is tracking, and waiting for *that* to stop is
            # waiting forever - which is what a declination nudge did with
            # tracking on, until the slew timeout gave up 180 seconds later.
            if not self._cruise and not _gotoing(ra) and not _gotoing(dec):
                for axis in (AXIS_RA, AXIS_DEC):
                    if self._expect.get(axis) in ("goto", "cruise"):
                        del self._expect[axis]
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
            # Checked, not assumed: an abort is what gets pressed when
            # something is wrong, and the axis that is wrong is the one
            # least likely to have listened.
            for axis in (AXIS_RA, AXIS_DEC):
                await asyncio.to_thread(self._await_stopped, link, axis)
        self._expect.clear()
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
        async with self._lock:
            ra_counts, dec_counts, _, _, now = await asyncio.to_thread(self._read_axes_timed, link)

        ra_axis = self._axis_degrees(AXIS_RA, ra_counts)
        dec_axis = self._axis_degrees(AXIS_DEC, dec_counts)
        model_ha, model_dec, _ = gem_sky(ra_axis, dec_axis)
        self._ha_offset_deg = _wrap180(
            hour_angle_deg(actual.ra_deg, self._site.longitude_deg, now) - model_ha
        )
        self._dec_offset_deg = actual.dec_deg - model_dec
        self._cached = None
        await self._publish()

    # -------------------------------------------------------------- tracking

    async def set_tracking(self, enabled: bool, rate: TrackingRate = TrackingRate.SIDEREAL) -> None:
        if enabled and self._parked:
            raise DeviceError("the mount is parked")
        if enabled:
            self._require_motion()
        self._ra_offset = 0.0
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

        An axis already turning the right way is **retuned, not
        restarted**. Stopping it first is the obvious way to write this
        and it is what made guiding diverge: a guide pulse stopped the
        tracking axis, waited for it to halt, set a mode, set a period
        and started it again - twice, once to pulse and once to restore -
        which is half a second of a tracking axis not tracking, or seven
        arcseconds of sky, for every correction of two. The loop then
        corrected the drift it had caused, lost more time causing it, and
        ran away. This is also how the mount's own driver does it.
        """
        stopping = abs(multiplier) < 1e-6
        backward = multiplier < 0
        status = link.status(axis)
        turning_right_way = status.running and status.slewing and status.backward == backward

        if not stopping and turning_right_way:
            link.set_step_period(axis, self._rate_period(axis, multiplier))
            return

        link.stop(axis)
        if stopping:
            # Not waited for. The axis decelerates on its own, and the
            # only thing that needs it stopped is a mode change - which
            # is the branch below. Blocking here put a fifth of a second
            # on the end of every declination guide pulse.
            return
        self._await_stopped(link, axis)
        link.set_motion_mode(axis, goto=False, fast=False, backward=backward)
        link.set_step_period(axis, self._rate_period(axis, multiplier))
        link.start(axis)

    def _rate_period(self, axis: int, multiplier: float) -> int:
        return max(1, round(self._sidereal_period[axis] / abs(multiplier)))

    # ----------------------------------------------------------- park / home

    async def park(self) -> None:
        """Back to the position the mount powers up in, then stop.

        Home is where the counters read zero, which for an equatorial head
        is counterweight down and the telescope on the polar axis - the
        position it is safe to leave a mount in, and the only one this
        controller knows by heart.
        """
        link = self._require_motion()
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

    async def set_home(self) -> None:
        """Take where the mount stands now as home: counterweight down, the
        telescope on the pole - roughly at Polaris.

        For when its idea of where it points is wrong: it was powered on
        somewhere other than home, turned by hand, or lost track in a
        runaway. Both counters go to zero and any sync is dropped, so the
        mount believes exactly what it would had it been switched on here.
        Home is defined by the mechanics rather than by a star, which is
        why "roughly" is good enough: a sync on Polaris itself, 0.6 degrees
        from the pole, turns a small aiming error into a large one in right
        ascension.

        Moves nothing, so it is allowed while halted after a fault.
        """
        link = self._require_link()
        self._tracking = False
        self._track_on_arrival = False
        self._ra_offset = 0.0
        self._cruise.clear()
        async with self._lock:
            await asyncio.to_thread(self._zero_counters, link)
            # The counts jump; the watchdog must not read that as motion.
            self._expect.clear()
            self._watch_last.clear()
            self._strikes.clear()
        self._ha_offset_deg = 0.0
        self._dec_offset_deg = 0.0
        self._parked = True
        self._target = None
        self._cached = None
        logger.warning("home set by the operator: both counters zeroed where the mount stands")
        await self._publish()

    def _zero_counters(self, link: SyntaLink) -> None:
        for axis in (AXIS_RA, AXIS_DEC):
            link.stop(axis)
            self._await_stopped(link, axis)
            link.set_position(axis, 0)

    # --------------------------------------------------------------- moving

    async def move_by(self, direction: GuideDirection, degrees: float) -> None:
        """A framing move: a relative goto on one axis.

        A goto rather than a timed run at some rate, because the
        controller ramps a goto up and down by itself and stops on the
        count - so a degree is a degree, at whatever speed the mount can
        manage, instead of a guess about how long to leave a motor on.
        """
        link = self._require_motion()
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
            # Which way is north in counts depends on the side of the mount.
            axis = AXIS_DEC
            signed = travel if direction is GuideDirection.NORTH else -travel

        async with self._lock:
            if axis == AXIS_DEC:
                signed *= self._north_sign(await asyncio.to_thread(link.position, AXIS_DEC))
            await asyncio.to_thread(self._start_goto, link, {axis: signed})
        self._cached = None
        await self.wait_for_slew(timeout_s=self._config.slew_timeout_s)

    # -------------------------------------------------------------- guiding

    @property
    def dec_step_arcsec(self) -> float:
        counts = self._counts_per_rev.get(AXIS_DEC)
        return 1_296_000.0 / counts if counts else 0.0

    @property
    def worm_period_s(self) -> float | None:
        """How long the RA worm takes to turn once while tracking.

        Its share of a revolution of the axis, times the time a sidereal
        revolution takes - which is the period of its periodic error.
        """
        counts = self._counts_per_rev.get(AXIS_RA)
        if not counts or not self._steps_per_worm:
            return None
        return self._steps_per_worm / counts * 360.0 / SIDEREAL_RATE_DEG_PER_S

    async def set_ra_rate_offset(self, arcsec_per_s: float) -> float:
        """Run RA this much faster than tracking, and leave it there.

        The tracking axis is retuned in place - one status read and one
        step-period write, never a stop - so the change is a change of
        speed and nothing else. The period is a whole number of timer
        ticks, about 2.6 ppm of sidereal apart, so the offset applied is
        the nearest one to what was asked: within about 0.00004"/s.
        """
        link = self._require_motion()
        if self._parked:
            raise DeviceError("the mount is parked")
        if not self._tracking:
            raise DeviceError("RA is not tracking, so there is no rate to offset")
        sidereal = SIDEREAL_RATE_DEG_PER_S * 3600.0
        base = self._rate_multiplier()
        multiplier = base + arcsec_per_s / sidereal
        if multiplier <= 0:
            raise DeviceError(f"an offset of {arcsec_per_s:+.2f}\u2033/s would stop or reverse tracking")
        async with self._lock:
            await asyncio.to_thread(self._set_axis_rate, link, AXIS_RA, multiplier)
        period = self._rate_period(AXIS_RA, multiplier)
        applied = (self._sidereal_period[AXIS_RA] / period - base) * sidereal
        self._ra_offset = applied
        return applied

    async def step_dec(self, direction: GuideDirection, steps: int) -> int:
        """Move declination by exactly this many motor steps.

        A relative goto of that many counts: the controller ramps it and
        stops on the count, so the size of the move is decided by the
        mount's own counter rather than by how long a motor was left on
        over a 9600-baud line. Returns the steps the counter actually
        moved - which is the check that the move was what was asked.
        """
        link = self._require_motion()
        if self._parked:
            raise DeviceError("the mount is parked")
        if direction not in (GuideDirection.NORTH, GuideDirection.SOUTH):
            raise DeviceError(f"declination steps go north or south, not {direction}")
        if steps <= 0:
            return 0
        self._expect[AXIS_DEC] = "step"
        try:
            async with self._lock:
                # Which way north runs in counts depends on the side of the
                # mount, so it is decided from where the axis is now.
                north = self._north_sign(await asyncio.to_thread(link.position, AXIS_DEC))
                counts = steps * (north if direction is GuideDirection.NORTH else -north)
                moved = await asyncio.to_thread(self._step_axis, link, AXIS_DEC, counts)
                # A few steps too many is the stop arriving a step or two
                # late. Many more, or the wrong way, is an axis doing
                # something it was not told to: halted, not logged.
                along = moved if counts > 0 else -moved
                if along < -2 or abs(moved) > steps + max(10, steps):
                    await asyncio.to_thread(
                        self._halt,
                        link,
                        f"declination was asked for {steps} steps {direction} and moved {moved:+d}",
                    )
        finally:
            self._expect.pop(AXIS_DEC, None)
        self._cached = None
        if self._fault is not None:
            await self._publish()
            raise DeviceError(f"the mount halted itself: {self._fault}")
        if abs(moved) != steps:
            logger.warning("declination asked for %d steps %s, counter moved %+d", steps, direction, moved)
        return abs(moved)

    def _step_axis(self, link: SyntaLink, axis: int, counts: int) -> int:
        before = link.position(axis)
        link.stop(axis)
        self._await_stopped(link, axis)
        if abs(counts) <= self._config.creep_max_steps:
            self._creep(link, axis, counts, before)
        else:
            link.set_motion_mode(axis, goto=True, fast=False, backward=counts < 0)
            link.set_goto_target(axis, counts)
            try:
                link.set_brake_increment(axis, min(self._config.brake_counts, abs(counts) // 4 + 1))
            except SyntaError:
                logger.debug("axis %d will not take a brake point", axis)
            link.start(axis)
            self._await_stopped(link, axis, timeout_s=5.0)
        return link.position(axis) - before

    def _creep(self, link: SyntaLink, axis: int, counts: int, before: int) -> None:
        """A few steps, by running slowly and stopping on the counter.

        The size of the move is decided by the counter, read in a loop,
        not by a timer and not by the controller's goto - which does not
        do small moves at all. At half sidereal a step takes about 60 ms
        and a status read about 20, so it stops within one step.
        """
        direction = 1 if counts > 0 else -1
        link.set_motion_mode(axis, goto=False, fast=False, backward=counts < 0)
        link.set_step_period(axis, self._rate_period(axis, self._config.creep_rate))
        link.start(axis)
        per_second = self._counts_per_rev[axis] * self._config.creep_rate * SIDEREAL_RATE_DEG_PER_S / 360.0
        deadline = time.monotonic() + abs(counts) / max(per_second, 1e-6) * 3 + 1.0
        try:
            while time.monotonic() < deadline:
                if (link.position(axis) - before) * direction >= abs(counts):
                    break
        finally:
            link.stop(axis)
            # At creep speed a stop takes a step or two. Waiting the full
            # timeout for one that is not happening is ten seconds of an
            # axis doing whatever it is doing.
            self._await_stopped(link, axis, timeout_s=min(2.0, self._config.stop_timeout_s))

    async def pulse_guide(self, direction: GuideDirection, duration_ms: int) -> None:
        """A fixed-length nudge, at the guide rate.

        Right ascension is corrected by running the tracking axis faster or
        slower rather than by stopping and reversing it: a guide correction
        that stops the axis loses the worm's tooth contact and comes back
        as backlash on the next frame.
        """
        link = self._require_motion()
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
            # Which way north runs depends on the side of the mount. Once
            # hard-coded as "down", which is right on one side only - and
            # before that the other way round, which drove every correction
            # the way the error already pointed.
            north = self._north_sign(self._last_dec_counts)
            sign = float(north if direction is GuideDirection.NORTH else -north)
            await self._pulse_axis(link, AXIS_DEC, sign * rate, seconds, 0.0)

    async def _pulse_axis(
        self, link: SyntaLink, axis: int, multiplier: float, seconds: float, restore: float
    ) -> None:
        if axis == AXIS_DEC:
            self._expect[axis] = "pulse"
        started = time.monotonic()
        async with self._lock:
            await asyncio.to_thread(self._set_axis_rate, link, axis, multiplier)
        try:
            await asyncio.sleep(seconds)
        finally:
            async with self._lock:
                await asyncio.to_thread(self._set_axis_rate, link, axis, restore)
            self._cached = None
            if axis == AXIS_DEC:
                self._expect.pop(axis, None)

        # What the pulse cost beyond its own length. On the tracking axis
        # this is sky lost, and it used to be most of the pulse.
        overhead = time.monotonic() - started - seconds
        if overhead > 0.1:
            # Only the tracking axis loses sky by being slow; declination
            # is standing still either way, and saying "6.8 arcsec lost"
            # about it is a number that means nothing.
            cost = (
                f" - {overhead * SIDEREAL_RATE_DEG_PER_S * 3600.0:.1f} arcsec of sky"
                if axis == AXIS_RA and restore != 0
                else ""
            )
            logger.warning(
                "axis %d guide pulse of %.0f ms took %.0f ms longer than asked%s",
                axis,
                seconds * 1000,
                overhead * 1000,
                cost,
            )

    # ---------------------------------------------------------------- safety

    @property
    def fault(self) -> str | None:
        """Why the mount halted itself, while it refuses to move."""
        return self._fault

    async def clear_fault(self) -> None:
        """Allow moves again after a halt - once both axes are standing still.

        Tracking stays off: after a runaway, where the mount points is
        whatever it ended up at, and the operator decides what next.
        """
        link = self._require_link()
        async with self._lock:
            running = await asyncio.to_thread(
                lambda: [axis for axis in (AXIS_RA, AXIS_DEC) if link.status(axis).running]
            )
        if running:
            names = " and ".join("RA" if axis == AXIS_RA else "Dec" for axis in running)
            raise DeviceError(f"{names} is still turning - cut the mount's power, then reconnect")
        logger.warning("mount fault cleared by the operator: %s", self._fault)
        self._fault = None
        self._watch_last.clear()
        self._strikes.clear()
        self._cached = None
        await self._publish()

    async def _watch(self) -> None:
        """Read both axes every so often, and halt anything unexplained.

        The one part of this backend that does not trust the rest of it. An
        axis turning with no move of ours under way - one that ignored a
        stop, or one the SynScan app is driving over the mount's own WiFi -
        or turning faster than the move it is on allows, or beyond where
        any move could take it, halts both axes.
        """
        period = self._config.watchdog_period_s or 1.0
        while True:
            await asyncio.sleep(period)
            link = self._link
            if link is None or self._fault is not None:
                continue
            reason = None
            try:
                async with self._lock:
                    readings = await asyncio.to_thread(self._read_for_watch, link)
                    reason = self._judge(readings)
                    if reason is not None:
                        await asyncio.to_thread(self._halt, link, reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("the mount watchdog could not read the axes")
                continue
            if reason is not None:
                await self._publish()

    def _read_for_watch(self, link: SyntaLink) -> dict[int, tuple[float, int, AxisStatus]]:
        return {
            axis: (time.monotonic(), link.position(axis), link.status(axis)) for axis in (AXIS_RA, AXIS_DEC)
        }

    def _judge(self, readings: dict[int, tuple[float, int, AxisStatus]]) -> str | None:
        """What is wrong with these readings, if anything."""
        for axis, (when, counts, status) in readings.items():
            name = "RA" if axis == AXIS_RA else "declination"
            expected = self._expect.get(axis)
            last = self._watch_last.get(axis)
            self._watch_last[axis] = (when, counts, expected)

            # Counts are never wrapped, so a spin shows as thousands of
            # degrees rather than hiding behind a full turn.
            angle = self._axis_degrees(axis, counts)
            if abs(angle) > self._config.max_axis_deg + 5.0:
                return (
                    f"the {name} axis is {angle:+.0f} degrees from home, beyond anywhere a move could take it"
                )

            if expected == "goto":
                # The controller's own goto runs at its own speed, and is
                # over when the axis stops.
                self._strikes[axis] = 0
                if not status.running:
                    self._expect.pop(axis, None)
                continue
            if not status.running:
                self._strikes[axis] = 0
                continue

            allowed = self._allowed_rate(axis, expected, status)
            if last is not None and last[2] == expected and when > last[0]:
                per_sidereal = self._counts_per_rev[axis] * SIDEREAL_RATE_DEG_PER_S / 360.0
                rate = abs(counts - last[1]) / (when - last[0]) / per_sidereal
                if rate > max(allowed, 0.5) * 1.5 + 0.5:
                    return (
                        f"the {name} axis was turning at {rate:.0f}x sidereal, where at most "
                        f"{allowed:.1f}x was asked for"
                    )
            if allowed > 0:
                self._strikes[axis] = 0
                continue
            # Turning with nothing asked of it. Twice in a row, so an axis
            # still coasting to a halt from a move that just ended is not
            # mistaken for one that will not stop.
            self._strikes[axis] = self._strikes.get(axis, 0) + 1
            if self._strikes[axis] >= 2:
                return f"the {name} axis is turning with no move under way"
        return None

    def _allowed_rate(self, axis: int, expected: str | None, status: AxisStatus) -> float:
        """How fast this axis may turn now, in multiples of sidereal; zero
        when it should not be turning at all."""
        if expected == "cruise":
            return self._config.slew_rate
        if expected == "step":
            return self._config.creep_rate
        if expected == "pulse":
            return self._config.guide_rate
        if axis == AXIS_RA and self._tracking and status.slewing:
            # Constant-rate motion with tracking on: tracking, with or
            # without guiding's offset.
            return self._config.watchdog_max_rate
        return 0.0

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
            fault=status.fault,
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
        """Where the motors physically point, without asking the mount again.

        The simulated camera renders from this, so it has to be cheap and
        synchronous - a serial round trip per frame would slow the preview
        loop to the speed of a 9600 baud line. It is the last polled axis
        position, which the status poller refreshes about once a second.

        Physical, not synced: the sync model is the mount's *belief*, and
        a sync changes the belief without moving anything. Drawn from the
        synced position, a plate solve and sync moved the pretend sky along
        with the belief - centring then found the same error on every pass
        and could never converge.
        """
        if self._physical_position is not None:
            return self._physical_position
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
            ra_counts, dec_counts, ra_status, dec_status = await asyncio.to_thread(self._read_axes, link)
        return {
            "firmware": f"{self._version:06X}",
            "port": self._config.port,
            "counts_per_revolution": dict(self._counts_per_rev),
            "sidereal_period": dict(self._sidereal_period),
            "timer_hz": dict(self._timer_hz),
            "high_speed_ratio": self._high_speed_ratio,
            "steps_per_worm": self._steps_per_worm,
            "worm_period_s": None if self.worm_period_s is None else round(self.worm_period_s, 2),
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


def gem_sky(ra_axis_deg: float, dec_axis_deg: float) -> tuple[float, float, PierSide]:
    """Hour angle, declination and side of the mount, from the axis angles.

    German-equatorial geometry, in Sky-Watcher's own conventions as INDI's
    EQMod driver has them. Both angles are measured from home - the
    counterweight down, the telescope on the pole - where the counters
    read zero. With the counterweight down the Dec axis lies in the
    meridian, so turning it swings the telescope down the hour circle six
    hours from the meridian: the RA axis angle is the hour angle only
    after that quarter turn, which way depending on which way Dec turned.

    Dec turned positive, the telescope is on the east side of the mount,
    looking west of the RA axis's angle; negative, the west side, looking
    east. Written as though the RA axis read hour angle directly, a GoTo
    to a target in the east pointed at the west - both axes the wrong way.
    """
    if dec_axis_deg > 0:
        hour_angle, declination, side = ra_axis_deg + 90.0, 90.0 - dec_axis_deg, PierSide.EAST
    else:
        hour_angle, declination, side = ra_axis_deg - 90.0, 90.0 + dec_axis_deg, PierSide.WEST
    hour_angle, declination = _fold(hour_angle, declination)
    return hour_angle, declination, side


def gem_axes(hour_angle_deg: float, dec_deg: float, side: PierSide) -> tuple[float, float]:
    """The axis angles that point at an hour angle and declination from
    one side of the mount - the inverse of `gem_sky`."""
    if side is PierSide.EAST:
        return _wrap180(hour_angle_deg - 90.0), 90.0 - dec_deg
    return _wrap180(hour_angle_deg + 90.0), dec_deg - 90.0


def gem_side_for(hour_angle_deg: float) -> PierSide:
    """The side of the mount that reaches this hour angle with the
    counterweight down: west of the pier for a target in the east."""
    return PierSide.WEST if _wrap180(hour_angle_deg) < 0 else PierSide.EAST


def _fold(hour_angle: float, declination: float) -> tuple[float, float]:
    """An hour angle and a declination that may have run past a pole, as
    the same direction seen properly: declination within +/-90."""
    # Wrapped before it is folded. Without the wrap, an axis angle and a
    # sync offset that between them exceed 270 degrees produce a
    # declination outside +/-90 - which is not a pointing, it is a
    # ValueError out of the status endpoint.
    declination = _wrap180(declination)
    if declination > 90.0:
        declination = 180.0 - declination
        hour_angle += 180.0
    elif declination < -90.0:
        declination = -180.0 - declination
        hour_angle += 180.0
    return _wrap180(hour_angle), declination


def _wrap180(degrees: float) -> float:
    """Fold an angle into (-180, 180], the short way round."""
    return (degrees + 180.0) % 360.0 - 180.0


def sidereal_arcsec_per_second() -> float:
    """Exposed for tests and for the setup panel's sanity check."""
    return SIDEREAL_RATE_DEG_PER_S * 3600.0
