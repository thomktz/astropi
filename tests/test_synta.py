"""The Sky-Watcher backend, against a controller that answers like the real one.

The replies here are the ones the Star Adventurer GTi actually gave over
its USB port - 3,628,800 counts on the RA axis, 2,903,040 on declination,
a 16 MHz timer - so the arithmetic is exercised against real gearing rather
than against round numbers that would hide a factor-of-two.
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from astropi.core.events import EventBus
from astropi.core.geometry import RaDec
from astropi.core.site import ObservingSite
from astropi.core.timekeeping import hour_angle_deg, local_sidereal_time_deg
from astropi.devices.backends.synta.mount import AXIS_DEC, AXIS_RA, SyntaMount, SyntaMountConfig
from astropi.devices.backends.synta.protocol import (
    HOME_COUNTS,
    AxisStatus,
    SyntaError,
    SyntaLink,
    Transport,
    decode24,
    encode24,
)
from astropi.devices.mount import GuideDirection, MountState, TrackingRate

PARIS = ObservingSite(latitude_deg=48.8566, longitude_deg=2.3522)

COUNTS_PER_REV = {AXIS_RA: 3_628_800, AXIS_DEC: 2_903_040}
SIDEREAL_PERIOD = {AXIS_RA: 379_912, AXIS_DEC: 474_890}


class FakeController(Transport):
    """A Star Adventurer GTi, as far as the serial line can tell.

    A goto lands instantly, which is what matters for testing the
    conversation. Constant-rate motion does not: it advances with the
    clock at the step period it was given, because the whole point of
    the cruise path is that the caller watches the counts go by and
    decides when to stop.

    It also does what the real controller was measured doing: **ignoring
    the step period in goto mode**. A fake that honoured it would have
    hidden the bug this exists to prevent coming back.
    """

    def __init__(self) -> None:
        self.position = {AXIS_RA: 0, AXIS_DEC: 0}
        self.initialised = {AXIS_RA: False, AXIS_DEC: False}
        self.running = {AXIS_RA: False, AXIS_DEC: False}
        self.mode: dict[int, str] = {}
        self.goto_target: dict[int, int] = {}
        self.step_period: dict[int, int] = {}
        self.brake: dict[int, int] = {}
        self.log: list[str] = []
        self.started_at: dict[int, float] = {}
        self._fraction: dict[int, float] = {}
        self._pending = b""
        #: Counts per worm turn, for `:s`. Unset, the command is refused,
        #: as some firmware does.
        self.steps_per_worm: int | None = None
        #: Axes that answer a stop and keep turning - what the real
        #: declination axis did - and ones that ignore an emergency stop too.
        self.ignores_stop: set[int] = set()
        self.ignores_emergency_stop: set[int] = set()
        #: A step period an axis runs at whatever it is told: a controller
        #: gone wrong, or something else driving it.
        self.period_override: dict[int, int] = {}
        #: Counts an axis leaps by every time it is looked at while
        #: running: a runaway that does not depend on how fast the test is.
        self.leaps: dict[int, int] = {}

    # -- transport

    def write(self, data: bytes) -> None:
        self._pending = self._reply(data.decode().strip()).encode()

    def read_until(self, terminator: bytes, timeout_s: float) -> bytes:
        return self._pending + b"\r"

    def reset(self) -> None:
        self._pending = b""

    def close(self) -> None:
        self.running = {AXIS_RA: False, AXIS_DEC: False}

    # -- controller

    def _advance(self, axis: int) -> None:
        """Roll a constant-rate axis forward for the time it has been running."""
        if not self.running[axis]:
            return
        if axis in self.leaps:
            backward = self.mode.get(axis, "").endswith("1")
            self.position[axis] += -self.leaps[axis] if backward else self.leaps[axis]
            return
        now = time.monotonic()
        elapsed = now - self.started_at.get(axis, now)
        self.started_at[axis] = now
        period = self.period_override.get(axis) or self.step_period.get(axis)
        if not period:
            return
        # Fractions carried between reads, as a real counter's would be:
        # read in a tight loop, each interval is less than a whole step,
        # and truncating each one froze the axis.
        progress = self._fraction.get(axis, 0.0) + elapsed * 16_000_000 / period
        steps = int(progress)
        self._fraction[axis] = progress - steps
        backward = self.mode.get(axis, "").endswith("1")
        self.position[axis] += -steps if backward else steps

    def _reply(self, message: str) -> str:
        command, axis, data = message[1], int(message[2]), message[3:]
        self.log.append(message[1:])

        if command == "e":
            return "=" + encode24(0x0C3003)
        if command == "a":
            return "=" + encode24(COUNTS_PER_REV[axis])
        if command == "b":
            return "=" + encode24(16_000_000)
        if command == "D":
            return "=" + encode24(SIDEREAL_PERIOD[axis])
        if command == "g":
            return "=01"
        if command == "s" and self.steps_per_worm is not None:
            return "=" + encode24(self.steps_per_worm)
        if command == "j":
            self._advance(axis)
            return "=" + encode24(self.position[axis] + HOME_COUNTS)
        if command == "f":
            first = 0x1 if self.mode.get(axis, "").startswith(("1", "3")) else 0x0
            second = 0x1 if self.running[axis] else 0x0
            third = 0x1 if self.initialised[axis] else 0x0
            return f"={first:X}{second:X}{third:X}"
        if command == "F":
            self.initialised[axis] = True
            return "="
        if command == "E":
            self.position[axis] = decode24(data) - HOME_COUNTS
            return "="
        if command == "G":
            if self.running[axis]:
                raise AssertionError("mode changed while the axis was running")
            self.mode[axis] = data
            return "="
        if command == "H":
            self.goto_target[axis] = decode24(data)
            return "="
        if command == "I":
            self.step_period[axis] = decode24(data)
            return "="
        if command == "M":
            self.brake[axis] = decode24(data)
            return "="
        if command == "J":
            mode = self.mode.get(axis, "")
            if not self.initialised[axis]:
                return "!4"
            if mode.startswith(("0", "2")):
                # A goto: apply it and stop, the way a real one ends up.
                step = self.goto_target.get(axis, 0)
                self.position[axis] += -step if mode.endswith("1") else step
            else:
                self.running[axis] = True
                self.started_at[axis] = time.monotonic()
            return "="
        if command in "KL":
            self._advance(axis)
            ignored = self.ignores_stop if command == "K" else self.ignores_emergency_stop
            if axis not in ignored:
                self.running[axis] = False
            return "="
        return "!0"


@pytest.fixture
def rig():
    """A mount whose moves go through the controller's own goto.

    At or above `native_goto_rate` the move is handed to the controller,
    which lands it immediately here - so tests about the conversation are
    not also tests about how long a fifty degree slew takes.
    """
    controller = FakeController()
    mount = SyntaMount(
        PARIS,
        EventBus(),
        SyntaMountConfig(guide_rate=0.5, slew_rate=800.0),
        transport=controller,
    )
    return controller, mount


@pytest.fixture
def cruising_rig():
    """A mount slow enough to be driven at a rate rather than aimed.

    Below `native_goto_rate` the move is a constant-rate run stopped by
    the backend, which is the path that exists because the controller
    ignores the step period in goto mode. Moves here are small on
    purpose: this fake advances with the wall clock, so a degree at a
    degree a second costs a test a second.
    """
    controller = FakeController()
    mount = SyntaMount(
        PARIS,
        EventBus(),
        SyntaMountConfig(slew_rate=400.0),
        transport=controller,
    )
    return controller, mount


def test_values_travel_low_byte_first():
    """The one quirk of this protocol, pinned in both directions."""
    assert decode24("005F37") == 3_628_800
    assert encode24(3_628_800) == "005F37"
    assert decode24(encode24(HOME_COUNTS)) == HOME_COUNTS


def test_a_refusal_is_raised_not_returned():
    class Refusing(Transport):
        def write(self, data): ...
        def read_until(self, terminator, timeout_s):
            return b"!2\r"

        def reset(self): ...
        def close(self): ...

    with pytest.raises(SyntaError, match="motor not stopped"):
        SyntaLink(Refusing()).command("J", 1)


def test_status_bits():
    resting = AxisStatus.parse("100")
    assert resting.slewing and not resting.running and not resting.initialised
    assert AxisStatus.parse("111").initialised


async def test_connecting_learns_the_gearing_and_wakes_the_axes(rig):
    controller, mount = rig
    await mount.connect()

    assert controller.initialised == {AXIS_RA: True, AXIS_DEC: True}
    assert mount._counts_per_rev == COUNTS_PER_REV
    assert mount.descriptor.details["firmware"] == "0C3003"


async def test_the_worm_period_comes_from_the_controller(rig):
    """Its share of an axis turn, at a sidereal day per turn."""
    controller, mount = rig
    # A 180-tooth worm: illustrative, not a figure read off a GTi.
    controller.steps_per_worm = COUNTS_PER_REV[AXIS_RA] // 180
    await mount.connect()

    assert mount.worm_period_s == pytest.approx(86_164.09 / 180, rel=1e-4)


async def test_a_controller_that_will_not_say_has_no_worm_period(rig):
    _, mount = rig
    await mount.connect()

    assert mount.worm_period_s is None


async def test_tracking_runs_the_ra_axis_at_the_mounts_own_sidereal_period(rig):
    """The rate comes from the controller, not from our own arithmetic."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)

    assert controller.step_period[AXIS_RA] == SIDEREAL_PERIOD[AXIS_RA]
    assert controller.running[AXIS_RA]
    assert controller.mode[AXIS_RA] == "10", "slow constant-rate, forward"
    assert AXIS_DEC not in controller.step_period, "declination must not be driven"

    await mount.set_tracking(False)
    assert not controller.running[AXIS_RA]


async def test_lunar_rate_is_slower_than_sidereal(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True, TrackingRate.LUNAR)

    # A longer step period is a slower axis, and the moon drifts east.
    assert controller.step_period[AXIS_RA] > SIDEREAL_PERIOD[AXIS_RA]


async def test_a_goto_stops_aims_then_starts(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    now = time.time()
    lst = hour_angle_deg(0.0, PARIS.longitude_deg, now)  # RA 0 -> its hour angle
    target = RaDec(ra_deg=(0.0 - 15.0) % 360.0, dec_deg=45.0)
    await mount.slew_to(target)

    ra_commands = [entry for entry in controller.log if entry[1] == "1" and entry[0] in "KGHJ"]
    assert [entry[0] for entry in ra_commands][:4] == ["K", "G", "H", "J"], (
        "the controller rejects a mode change while an axis runs, so every goto has to stop first"
    )
    assert lst is not None

    # And it lands where it was asked to, to within a count.
    await mount.wait_for_slew(timeout_s=5)
    status = await mount.status()
    assert status.position.dec_deg == pytest.approx(45.0, abs=0.01)
    assert status.position.ra_deg == pytest.approx(target.ra_deg, abs=0.05)


async def test_declination_uses_its_own_counts_per_revolution(rig):
    """The two axes are geared differently - 3,628,800 against 2,903,040.

    Assuming one figure for both would put declination 25% out, which on a
    45 degree slew is eleven degrees of sky.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    await mount.slew_to(RaDec(ra_deg=0.0, dec_deg=60.0))
    await mount.wait_for_slew(timeout_s=5)

    expected = round(30.0 * COUNTS_PER_REV[AXIS_DEC] / 360.0)
    assert controller.position[AXIS_DEC] == pytest.approx(expected, abs=2)


async def test_pointing_below_the_horizon_is_refused(rig):
    """A mount has no idea where the ground is, and will not ask."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    before = dict(controller.position)

    # Due south of Paris and 40 degrees below the pole: under the horizon
    # at any hour of the night.
    now = time.time()
    lst = local_sidereal_time_deg(PARIS.longitude_deg, now)
    with pytest.raises(Exception, match="the ground"):
        await mount.slew_to(RaDec(ra_deg=(lst + 180.0) % 360.0, dec_deg=-60.0))

    assert controller.position == before, "nothing may move on a refused slew"


async def test_the_horizon_limit_can_be_lowered_for_indoor_testing(rig):
    controller, _ = rig
    mount = SyntaMount(
        PARIS,
        EventBus(),
        # Fast enough to use the controller's own goto, which lands at
        # once here - this test is about the horizon check, not the move.
        SyntaMountConfig(min_altitude_deg=-90.0, slew_rate=800.0),
        transport=controller,
    )
    await mount.connect()
    await mount.unpark()

    now = time.time()
    lst = local_sidereal_time_deg(PARIS.longitude_deg, now)
    await mount.slew_to(RaDec(ra_deg=(lst + 180.0) % 360.0, dec_deg=-60.0))
    await mount.wait_for_slew(timeout_s=5)
    assert controller.position[AXIS_DEC] != 0


async def test_a_wild_sync_cannot_break_the_status_readout(rig):
    """Garbage in, a refusal out - never an exception from `status`."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    mount._dec_offset_deg = 175.0  # as a sync onto the wrong star would leave it
    controller.position[AXIS_DEC] = -round(15.0 * COUNTS_PER_REV[AXIS_DEC] / 360.0)

    status = await mount.status()
    assert -90.0 <= status.position.dec_deg <= 90.0


async def test_sync_moves_the_model_and_not_the_mount(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    before = dict(controller.position)

    await mount.sync_to(RaDec(ra_deg=83.6, dec_deg=22.0))

    assert controller.position == before, "a sync must never turn a motor"
    status = await mount.status()
    assert status.position.ra_deg == pytest.approx(83.6, abs=0.01)
    assert status.position.dec_deg == pytest.approx(22.0, abs=0.01)


async def test_a_sync_does_not_move_the_simulated_sky(rig):
    """The simulated camera draws where the motors point, not the model.

    Drawn from the synced position, a plate solve and sync dragged the
    pretend sky along with the mount's belief: centring measured the same
    22' error on every pass and never converged.
    """
    _, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.status()
    before = mount.true_position()

    await mount.sync_to(RaDec(ra_deg=(before.ra_deg + 0.4) % 360.0, dec_deg=before.dec_deg - 0.3))
    await mount.status()

    after = mount.true_position()
    assert after.ra_deg == pytest.approx(before.ra_deg, abs=0.01)
    assert after.dec_deg == pytest.approx(before.dec_deg, abs=0.01)


async def test_a_guide_pulse_speeds_the_axis_up_and_puts_it_back(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)

    await mount.pulse_guide(GuideDirection.WEST, 50)
    # Faster during the pulse, then back to exactly sidereal afterwards.
    assert controller.step_period[AXIS_RA] == SIDEREAL_PERIOD[AXIS_RA]
    assert controller.running[AXIS_RA], "tracking must resume after a pulse"


async def test_declination_pulses_move_the_declination_axis(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    await mount.pulse_guide(GuideDirection.NORTH, 30)
    assert AXIS_DEC in controller.step_period
    assert not controller.running[AXIS_DEC], "the axis must stop when the pulse ends"


@pytest.mark.parametrize("side_counts", [200_000, -200_000], ids=["east-side", "west-side"])
async def test_a_north_pulse_goes_the_same_way_as_a_north_nudge(rig, side_counts):
    """The two primitives disagreed, and guiding paid for it.

    `pulse_guide` once drove declination the way the error already
    pointed: RA guided at 1.2 arcseconds while declination ran to 20 and
    lost the star. Which way north runs in counts depends on the side of
    the mount, so both sides are checked.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    controller.position[AXIS_DEC] = side_counts
    await mount.status()

    await mount.move_by(GuideDirection.NORTH, 0.2)
    nudged = controller.position[AXIS_DEC] - side_counts

    controller.position[AXIS_DEC] = side_counts
    await mount.pulse_guide(GuideDirection.NORTH, 200)
    pulsed = controller.position[AXIS_DEC] - side_counts
    assert nudged * pulsed > 0, "a north pulse must agree with a north nudge"
    # Towards the pole: the axis angle's size shrinks, on either side.
    assert abs(side_counts + nudged) < abs(side_counts)


async def test_a_west_pulse_goes_the_same_way_as_a_west_nudge(rig):
    """The same check on the axis that was already right."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    await mount.move_by(GuideDirection.WEST, 0.2)
    assert controller.position[AXIS_RA] > 0

    controller.position[AXIS_RA] = 0
    await mount.pulse_guide(GuideDirection.WEST, 200)
    assert controller.position[AXIS_RA] > 0


async def test_parking_returns_to_the_controllers_own_zero(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.slew_to(RaDec(ra_deg=0.0, dec_deg=45.0))
    await mount.wait_for_slew(timeout_s=5)

    await mount.park()

    assert controller.position == {AXIS_RA: 0, AXIS_DEC: 0}
    status = await mount.status()
    assert status.state is MountState.PARKED
    assert not status.tracking


async def test_a_parked_mount_refuses_to_move(rig):
    _, mount = rig
    await mount.connect()

    with pytest.raises(Exception, match="parked"):
        await mount.slew_to(RaDec(ra_deg=10.0, dec_deg=20.0))


async def test_disconnecting_stops_the_motors(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)
    assert controller.running[AXIS_RA]

    await mount.disconnect()
    assert not controller.running[AXIS_RA], "a mount left running is a mount left slewing"


async def test_a_nudge_moves_a_known_angle_not_a_guessed_duration(rig):
    """The framing primitive: one axis, one angle, both directions."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    await mount.move_by(GuideDirection.WEST, 1.0)
    per_degree = COUNTS_PER_REV[AXIS_RA] / 360.0
    assert controller.position[AXIS_RA] == pytest.approx(per_degree, abs=2)

    await mount.move_by(GuideDirection.EAST, 1.0)
    assert controller.position[AXIS_RA] == pytest.approx(0, abs=4), "east must undo west"


@pytest.mark.parametrize("hour_angle", [-40.0, 40.0], ids=["east-target", "west-target"])
async def test_north_raises_declination(rig, hour_angle):
    """North is towards the pole on the sky, whichever side the telescope
    is on - which in counts is down on one side and up on the other.

    Getting this backwards is invisible in the counts and obvious on the
    sky, which is the worst combination - hence a test. Taken from a
    declination well away from the pole, where north still has room to
    mean something.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    lst = local_sidereal_time_deg(PARIS.longitude_deg, time.time())
    await mount.slew_to(RaDec(ra_deg=(lst - hour_angle) % 360.0, dec_deg=40.0))
    await mount.wait_for_slew(timeout_s=5)
    before_counts = controller.position[AXIS_DEC]

    await mount.move_by(GuideDirection.NORTH, 2.0)

    after = (await mount.status()).position.dec_deg
    assert after == pytest.approx(42.0, abs=0.02), "north is towards the pole"
    assert abs(controller.position[AXIS_DEC]) < abs(before_counts), "the axis turns back towards home"


async def test_a_nudge_is_not_a_goto(rig):
    _, mount = rig
    await mount.connect()
    await mount.unpark()

    with pytest.raises(Exception, match="not a nudge"):
        await mount.move_by(GuideDirection.WEST, 200.0)


async def test_a_goto_ends_with_the_mount_tracking(rig):
    """Arriving and standing still lets the target drift straight out.

    The simulator has always started tracking on arrival; the real mount
    was not, and the centring loop was measuring its own drift as a
    pointing error - fifteen arcminutes for every minute it spent solving.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    assert not (await mount.status()).tracking

    await mount.slew_to(RaDec(ra_deg=10.68, dec_deg=41.27))
    await mount.wait_for_slew(timeout_s=5)

    assert (await mount.status()).tracking
    assert controller.step_period[AXIS_RA] == SIDEREAL_PERIOD[AXIS_RA]


async def test_going_home_does_not_start_tracking(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.slew_to(RaDec(ra_deg=10.68, dec_deg=41.27))
    await mount.wait_for_slew(timeout_s=5)

    await mount.park()

    assert not (await mount.status()).tracking
    assert not controller.running[AXIS_RA], "a parked mount must be still"


async def test_a_nudge_does_not_stop_tracking(rig):
    """A goto cancels constant-rate motion, so a nudge has to put it back."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)

    await mount.move_by(GuideDirection.NORTH, 0.5)

    assert (await mount.status()).tracking
    assert controller.running[AXIS_RA]


async def test_a_nudge_on_an_idle_mount_leaves_it_idle(rig):
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    await mount.move_by(GuideDirection.WEST, 0.5)

    assert not (await mount.status()).tracking
    assert not controller.running[AXIS_RA]


async def test_connecting_asks_whether_the_mount_is_parked(rig):
    """A restart must describe the rig, not this object's defaults.

    The application restarts far more often than the mount moves, and it
    was declaring a mount parked while it sat at the declination of
    whatever it was last pointed at.
    """
    controller, mount = rig
    controller.position[AXIS_DEC] = round(30.0 * COUNTS_PER_REV[AXIS_DEC] / 360.0)

    await mount.connect()

    status = await mount.status()
    assert status.state is not MountState.PARKED
    # And it can be moved without unparking something that was not parked.
    await mount.move_by(GuideDirection.WEST, 0.1)


async def test_a_mount_at_home_is_parked(rig):
    controller, mount = rig
    controller.position = {AXIS_RA: 0, AXIS_DEC: 0}

    await mount.connect()

    assert (await mount.status()).state is MountState.PARKED


async def test_a_cruise_stops_ahead_of_the_target_not_on_it(cruising_rig):
    """An axis moving 16,000 counts a second jumps over a small window.

    The first version asked whether the remaining distance was *small*,
    which is a window the axis clears between two polls - so it never
    stopped, and ran until the slew timeout. It asks which side of the
    target it is on now.
    """
    controller, mount = cruising_rig
    await mount.connect()
    await mount.unpark()

    await mount.move_by(GuideDirection.WEST, 0.4)

    assert not controller.running[AXIS_RA]
    assert mount._axis_degrees(AXIS_RA, controller.position[AXIS_RA]) == pytest.approx(0.4, abs=0.02)


async def test_a_slow_move_is_driven_at_a_rate_not_aimed(cruising_rig):
    """Because the controller ignores the step period in goto mode.

    Measured on the real mount: asked for 0.84 degrees a second, it ran
    at 4.0 - its own maximum, ramped to regardless of what it was told.
    Constant-rate mode does respect the period, which is how tracking
    holds sidereal, so a slow move is built out of that instead and
    stopped by the backend when the counts arrive.
    """
    controller, mount = cruising_rig
    await mount.connect()
    await mount.unpark()

    await mount.move_by(GuideDirection.WEST, 0.4)

    assert controller.step_period[AXIS_RA] == pytest.approx(SIDEREAL_PERIOD[AXIS_RA] / 400, rel=0.01), (
        "the period must be the one asked for"
    )
    # Checked in the command log rather than in the controller's current
    # mode: the move *ends* in goto mode, because the last fraction of a
    # degree is cleaned up by a short one.
    modes = [entry[2:] for entry in controller.log if entry.startswith("G1")]
    assert any(mode.startswith(("1", "3")) for mode in modes), "constant rate, not goto"
    # And it arrives: within a tenth of a degree of where it was sent.
    assert mount._axis_degrees(AXIS_RA, controller.position[AXIS_RA]) == pytest.approx(0.4, abs=0.1)
    assert not controller.running[AXIS_RA], "and it stops when it gets there"


async def test_a_fast_move_is_left_to_the_controller(rig):
    """Above the threshold there is nothing to gain by stopping it here."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()

    await mount.move_by(GuideDirection.WEST, 5.0)

    assert controller.mode[AXIS_RA].startswith(("0", "2")), "goto mode"
    assert mount._axis_degrees(AXIS_RA, controller.position[AXIS_RA]) == pytest.approx(5.0, abs=0.01)


async def test_the_slew_rate_reads_out_in_degrees_per_second(rig):
    _, mount = rig
    mount.set_slew_rate(200.0)
    assert mount.slew_degrees_per_second() == pytest.approx(0.836, abs=0.01)


async def test_a_guide_pulse_does_not_stop_the_tracking_axis(rig):
    """Stopping it costs half a second of sky, every correction.

    This made guiding diverge on the real mount: each pulse stopped the
    RA axis, waited for it to halt, set a mode, set a period and started
    it again - twice - so the loop spent seven arcseconds of tracking to
    make a two arcsecond correction, then corrected the drift it had
    just caused. RMS climbed until the star was lost.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)
    controller.log.clear()

    await mount.pulse_guide(GuideDirection.WEST, 40)

    stops = [entry for entry in controller.log if entry.startswith("K1")]
    assert stops == [], "the tracking axis must keep turning through a pulse"
    # It was retuned instead: two period changes, one out and one back.
    periods = [entry for entry in controller.log if entry.startswith("I1")]
    assert len(periods) == 2
    assert controller.running[AXIS_RA], "and it is still tracking afterwards"
    assert controller.step_period[AXIS_RA] == SIDEREAL_PERIOD[AXIS_RA]


async def test_a_pulse_that_reverses_direction_does_stop_the_axis(rig):
    """The one case where it has to: a mode change needs a stopped motor.

    Declination guiding reverses, and the controller refuses `:G` while
    an axis is running - which is why this cannot simply never stop.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.pulse_guide(GuideDirection.NORTH, 30)
    controller.log.clear()

    await mount.pulse_guide(GuideDirection.SOUTH, 30)

    assert any(entry.startswith("K2") for entry in controller.log)
    assert not controller.running[AXIS_DEC]


async def test_a_rate_offset_retunes_tracking_without_stopping_it(rig):
    """Guiding by velocity: a change of speed, and nothing else."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)
    before = len(controller.log)

    applied = await mount.set_ra_rate_offset(0.5)

    sent = controller.log[before:]
    assert not any(entry[:2] in ("K1", "L1") for entry in sent), "the tracking axis must never stop"
    assert controller.running[AXIS_RA]
    assert controller.step_period[AXIS_RA] < SIDEREAL_PERIOD[AXIS_RA]
    # Whole timer ticks: within a hair of what was asked.
    assert applied == pytest.approx(0.5, abs=0.001)

    assert await mount.set_ra_rate_offset(0.0) == pytest.approx(0.0, abs=1e-9)
    assert controller.step_period[AXIS_RA] == SIDEREAL_PERIOD[AXIS_RA]


async def test_a_rate_offset_needs_tracking(rig):
    from astropi.core.errors import DeviceError

    _, mount = rig
    await mount.connect()
    await mount.unpark()
    with pytest.raises(DeviceError):
        await mount.set_ra_rate_offset(0.5)


async def test_declination_steps_move_exactly_that_many_counts(rig):
    """A step move is decided by the counter, not by a timer."""
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    # The telescope on the east side of the mount, where north is down.
    controller.position[AXIS_DEC] = start = 100_000

    assert await mount.step_dec(GuideDirection.NORTH, 3) == 3
    assert controller.position[AXIS_DEC] == start - 3
    assert await mount.step_dec(GuideDirection.SOUTH, 5) == 5
    assert controller.position[AXIS_DEC] == start + 2
    assert not controller.running[AXIS_DEC]


async def test_one_declination_step_is_the_gearing(rig):
    _, mount = rig
    await mount.connect()
    assert mount.dec_step_arcsec == pytest.approx(1_296_000 / COUNTS_PER_REV[AXIS_DEC])


async def test_a_slow_read_does_not_move_the_reported_position(rig):
    """RA is converted at the moment its counter was read.

    The time used to be taken before waiting for the serial line, so a
    read queued behind another command came back seconds of sky late: a
    0.3 s wait put the reported RA 4.5" out, and the simulated camera,
    drawing from it, showed the star jumping with nothing moving.
    """
    import asyncio
    import time as clock

    _, mount = rig
    await mount.connect()
    await mount.unpark()
    # Away from the pole, where an RA error shows on the sky.
    lst = local_sidereal_time_deg(PARIS.longitude_deg, clock.time())
    await mount.slew_to(RaDec(ra_deg=(lst - 20.0) % 360.0, dec_deg=20.0))
    await mount.wait_for_slew()
    await mount.set_tracking(True)

    mount._cached = None
    await mount.status()
    first = mount.true_position()

    # Another command holds the serial line for 0.3 s - a declination
    # step move, say - while the position read waits its turn.
    async def busy() -> None:
        async with mount._lock:
            await asyncio.sleep(0.3)

    holder = asyncio.create_task(busy())
    await asyncio.sleep(0)
    mount._cached = None
    await mount.status()
    await holder
    second = mount.true_position()

    moved = (second.ra_deg - first.ra_deg) * 3600.0 * math.cos(math.radians(first.dec_deg))
    assert abs(moved) < 0.5


# -- safety --------------------------------------------------------------


def _safety_rig(**config):
    controller = FakeController()
    settings = {"guide_rate": 0.5, "slew_rate": 800.0, "stop_retry_s": 0.05, "stop_timeout_s": 0.2}
    settings.update(config)
    mount = SyntaMount(PARIS, EventBus(), SyntaMountConfig(**settings), transport=controller)
    return controller, mount


def _spin(controller, axis: int, period: int) -> None:
    """Set an axis turning without the mount's say-so."""
    controller._advance(axis)
    controller.mode[axis] = "10"
    controller.step_period[axis] = period
    controller.running[axis] = True
    controller.started_at[axis] = time.monotonic()


async def test_an_axis_that_ignores_a_stop_is_emergency_stopped_and_the_mount_holds():
    """What declination did: answered the stop, kept turning. Now both axes
    get an emergency stop, and nothing moves until the fault is cleared."""
    controller, mount = _safety_rig()
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)
    controller.ignores_stop.add(AXIS_DEC)

    with pytest.raises(Exception, match="halted itself"):
        await mount.step_dec(GuideDirection.SOUTH, 1)

    assert "L2" in controller.log and "L1" in controller.log
    assert not controller.running[AXIS_DEC] and not controller.running[AXIS_RA]
    status = await mount.status()
    assert status.state is MountState.ERROR and "kept turning" in status.fault
    for move in (
        mount.step_dec(GuideDirection.SOUTH, 1),
        mount.set_tracking(True),
        mount.move_by(GuideDirection.NORTH, 1.0),
    ):
        with pytest.raises(Exception, match="halted itself"):
            await move

    controller.ignores_stop.clear()
    await mount.clear_fault()
    await mount.set_tracking(True)
    assert await mount.step_dec(GuideDirection.SOUTH, 1) >= 1
    await mount.disconnect()


async def test_an_axis_nothing_set_moving_is_halted_by_the_watchdog():
    """Turning with no move under way - a missed stop, or the SynScan app."""
    controller, mount = _safety_rig(watchdog_period_s=0.03)
    await mount.connect()
    await mount.unpark()
    _spin(controller, AXIS_DEC, SIDEREAL_PERIOD[AXIS_DEC])

    await asyncio.sleep(0.3)

    assert mount.fault is not None and "declination" in mount.fault
    assert not controller.running[AXIS_DEC]
    await mount.disconnect()


async def test_an_axis_at_full_speed_is_halted_at_once():
    controller, mount = _safety_rig(watchdog_period_s=0.03)
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)
    # Tracking RA, retuned by something else to a slew.
    controller.period_override[AXIS_RA] = 600

    await asyncio.sleep(0.3)

    assert mount.fault is not None and "sidereal" in mount.fault
    assert not controller.running[AXIS_RA]
    await mount.disconnect()


async def test_a_step_that_runs_away_halts_the_mount():
    """Asked for one step, the counter ran off: halted, not a warning."""
    controller, mount = _safety_rig()
    await mount.connect()
    await mount.unpark()
    controller.leaps[AXIS_DEC] = 500

    with pytest.raises(Exception, match="halted itself"):
        await mount.step_dec(GuideDirection.NORTH, 1)

    assert "asked for 1 steps" in mount.fault
    assert not controller.running[AXIS_DEC]
    await mount.disconnect()


async def test_an_axis_past_its_travel_is_halted():
    """A spin shows as thousands of degrees - counts are never wrapped."""
    controller, mount = _safety_rig(watchdog_period_s=0.03)
    await mount.connect()
    controller.position[AXIS_DEC] = COUNTS_PER_REV[AXIS_DEC] * 3

    await asyncio.sleep(0.2)

    assert mount.fault is not None and "from home" in mount.fault
    await mount.disconnect()


async def test_tracking_and_gotos_are_left_alone():
    _, mount = _safety_rig(watchdog_period_s=0.03)
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)
    await mount.move_by(GuideDirection.WEST, 2.0)
    await mount.set_ra_rate_offset(7.5)
    await mount.step_dec(GuideDirection.NORTH, 3)

    await asyncio.sleep(0.3)

    assert mount.fault is None
    await mount.disconnect()


async def test_a_cruise_is_left_alone():
    _, mount = _safety_rig(watchdog_period_s=0.03, slew_rate=400.0)
    await mount.connect()
    await mount.unpark()
    await mount.move_by(GuideDirection.NORTH, 0.5)

    assert mount.fault is None
    await mount.disconnect()


async def test_the_fault_is_not_cleared_while_an_axis_still_turns():
    controller, mount = _safety_rig()
    await mount.connect()
    await mount.unpark()
    controller.ignores_stop.add(AXIS_DEC)
    controller.ignores_emergency_stop.add(AXIS_DEC)

    with pytest.raises(Exception, match="halted itself"):
        await mount.step_dec(GuideDirection.SOUTH, 1)

    assert "cut the mount's power" in mount.fault
    # Slowed to a crawl, at least.
    assert controller.step_period[AXIS_DEC] == 0xFFFFFF
    with pytest.raises(Exception, match="still turning"):
        await mount.clear_fault()


async def test_setting_home_zeroes_the_counters_and_forgets_the_sync():
    """ "Forget what you think: I am at home." Nothing moves."""
    controller, mount = _safety_rig(watchdog_period_s=0.03)
    await mount.connect()
    await mount.unpark()
    await mount.set_tracking(True)
    await mount.move_by(GuideDirection.EAST, 20.0)
    await mount.sync_to(RaDec(120.0, 30.0))

    await mount.set_home()

    assert controller.position == {AXIS_RA: 0, AXIS_DEC: 0}
    assert not controller.running[AXIS_RA] and not controller.running[AXIS_DEC]
    status = await mount.status()
    assert status.state is MountState.PARKED and not status.tracking
    assert status.position.dec_deg == pytest.approx(90.0, abs=1e-6)
    # The counters jumped; the watchdog must not read that as a move.
    await mount.unpark()
    await mount.set_tracking(True)
    await asyncio.sleep(0.2)
    assert mount.fault is None
    await mount.disconnect()


async def test_home_can_be_set_while_halted():
    """It moves nothing, and a runaway is when the counters are most wrong."""
    controller, mount = _safety_rig()
    await mount.connect()
    await mount.unpark()
    controller.leaps[AXIS_DEC] = 500
    with pytest.raises(Exception, match="halted itself"):
        await mount.step_dec(GuideDirection.NORTH, 1)
    controller.leaps.clear()

    await mount.set_home()

    assert controller.position[AXIS_DEC] == 0
    assert mount.fault is not None
    await mount.disconnect()


# -- geometry ------------------------------------------------------------


def _eqmod_ra_dec(ra_counts, dec_counts, lst_hours):
    """INDI EQMod's EncodersToRADec, northern hemisphere, transcribed: the
    reference this backend's geometry has to agree with. EQMod's encoders
    read 0x800000 at power-on, with declination's then set a quarter turn
    on - home is 90 degrees - which is where ours read zero."""
    total_ra, total_dec = COUNTS_PER_REV[AXIS_RA], COUNTS_PER_REV[AXIS_DEC]
    init = 0x800000
    ra_step, dec_step = init + ra_counts, init + dec_counts + total_dec // 4

    if ra_step > init:
        hours = 24.0 - (ra_step - init) / total_ra * 24.0
    else:
        hours = (init - ra_step) / total_ra * 24.0
    hours = (hours + 6.0) % 24.0
    if dec_step > init:
        degrees = (dec_step - init) / total_dec * 360.0
    else:
        degrees = 360.0 - (init - dec_step) / total_dec * 360.0
    degrees %= 360.0

    ra = hours + lst_hours
    if 90.0 < degrees <= 270.0:
        ra -= 12.0
        dec = 180.0 - degrees
    else:
        dec = degrees if degrees <= 90.0 else degrees - 360.0
    return (ra % 24.0) * 15.0, dec


def _separation(a: RaDec, b: RaDec) -> float:
    ra1, dec1, ra2, dec2 = map(math.radians, (a.ra_deg, a.dec_deg, b.ra_deg, b.dec_deg))
    cosine = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


async def test_the_axes_read_the_sky_the_way_eqmod_does(rig):
    """Every axis position, against Sky-Watcher's own conventions."""
    import random

    _, mount = rig
    await mount.connect()
    now = time.time()
    lst_hours = local_sidereal_time_deg(PARIS.longitude_deg, now) / 15.0
    rng = random.Random(4)
    for _ in range(500):
        ra_counts = rng.randint(-COUNTS_PER_REV[AXIS_RA] // 2 + 1, COUNTS_PER_REV[AXIS_RA] // 2 - 1)
        dec_counts = rng.randint(-COUNTS_PER_REV[AXIS_DEC] // 2 + 1, COUNTS_PER_REV[AXIS_DEC] // 2 - 1)
        ours = mount._sky_from_axes(
            mount._axis_degrees(AXIS_RA, ra_counts),
            mount._axis_degrees(AXIS_DEC, dec_counts),
            now,
            synced=False,
        )
        theirs = RaDec(*_eqmod_ra_dec(ra_counts, dec_counts, lst_hours))
        # Within a thousandth of an arcsecond: rounding in acos, not geometry.
        assert _separation(ours, theirs) < 3e-7 * 1000, (ra_counts, dec_counts)


async def test_a_goto_lands_where_eqmod_says_those_counts_point(rig):
    """The round trip, through the reference rather than ourselves."""
    controller, mount = rig
    # Geometry, not the sky tonight: some of these are below the horizon.
    mount._config.min_altitude_deg = -90.0
    await mount.connect()
    await mount.unpark()
    for hour_angle, dec in [(-42.0, 15.7), (42.0, 15.7), (-150.0, 70.0), (120.0, -20.0), (-5.0, 60.0)]:
        lst = local_sidereal_time_deg(PARIS.longitude_deg, time.time())
        target = RaDec((lst - hour_angle) % 360.0, dec)
        await mount.slew_to(target)
        await mount.wait_for_slew(timeout_s=5)
        lst_hours = local_sidereal_time_deg(PARIS.longitude_deg, time.time()) / 15.0
        landed = RaDec(*_eqmod_ra_dec(controller.position[AXIS_RA], controller.position[AXIS_DEC], lst_hours))
        assert _separation(landed, target) < 0.05, (hour_angle, dec)


async def test_from_home_a_target_in_the_east_turns_ra_up_and_dec_down(rig):
    """What went wrong on the night: Jupiter in the east, from home.

    The telescope goes to the west side of the mount to look east, with
    the counterweight still down: RA turns a quarter turn less the hour
    angle, positive, and declination turns negative. The code that read
    the RA axis as hour angle did both the other way, and pointed west.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    lst = local_sidereal_time_deg(PARIS.longitude_deg, time.time())

    await mount.slew_to(RaDec((lst + 41.7) % 360.0, 15.72))
    await mount.wait_for_slew(timeout_s=5)

    ra_axis = mount._axis_degrees(AXIS_RA, controller.position[AXIS_RA])
    dec_axis = mount._axis_degrees(AXIS_DEC, controller.position[AXIS_DEC])
    assert ra_axis == pytest.approx(90.0 - 41.7, abs=0.3)
    assert dec_axis == pytest.approx(15.72 - 90.0, abs=0.1)
    assert (await mount.status()).pier_side.value == "west"
