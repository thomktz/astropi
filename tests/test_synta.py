"""The Sky-Watcher backend, against a controller that answers like the real one.

The replies here are the ones the Star Adventurer GTi actually gave over
its USB port - 3,628,800 counts on the RA axis, 2,903,040 on declination,
a 16 MHz timer - so the arithmetic is exercised against real gearing rather
than against round numbers that would hide a factor-of-two.
"""

from __future__ import annotations

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

    Motion is instant rather than modelled: what is being tested here is
    the conversation - the order of commands, the encoding, the direction
    - not the mechanics, which the simulated mount already covers.
    """

    def __init__(self) -> None:
        self.position = {AXIS_RA: 0, AXIS_DEC: 0}
        self.initialised = {AXIS_RA: False, AXIS_DEC: False}
        self.running = {AXIS_RA: False, AXIS_DEC: False}
        self.mode: dict[int, str] = {}
        self.goto_target: dict[int, int] = {}
        self.step_period: dict[int, int] = {}
        self.log: list[str] = []
        self._pending = b""

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
        if command == "j":
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
            return "="
        if command in "KL":
            self.running[axis] = False
            return "="
        return "!0"


@pytest.fixture
def rig():
    controller = FakeController()
    mount = SyntaMount(
        PARIS,
        EventBus(),
        SyntaMountConfig(guide_rate=0.5),
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
        def read_until(self, terminator, timeout_s): return b"!2\r"
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
        "the controller rejects a mode change while an axis runs, so every "
        "goto has to stop first"
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
        SyntaMountConfig(min_altitude_deg=-90.0),
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


async def test_north_raises_declination(rig):
    """The axis reads 90 minus the declination, so north turns it *down*.

    Getting this backwards is invisible in the counts and obvious on the
    sky, which is the worst combination - hence a test. Taken from a
    declination well away from the pole, where north still has room to
    mean something.
    """
    controller, mount = rig
    await mount.connect()
    await mount.unpark()
    await mount.slew_to(RaDec(ra_deg=(await mount.status()).position.ra_deg, dec_deg=40.0))
    await mount.wait_for_slew(timeout_s=5)
    before_counts = controller.position[AXIS_DEC]

    await mount.move_by(GuideDirection.NORTH, 2.0)

    after = (await mount.status()).position.dec_deg
    assert after == pytest.approx(42.0, abs=0.02), "north is towards the pole"
    assert controller.position[AXIS_DEC] < before_counts, "and that is down in counts"


async def test_a_nudge_is_not_a_goto(rig):
    _, mount = rig
    await mount.connect()
    await mount.unpark()

    with pytest.raises(Exception, match="not a nudge"):
        await mount.move_by(GuideDirection.WEST, 200.0)
