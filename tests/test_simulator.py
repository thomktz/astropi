"""The simulated rig must behave like hardware that is imperfect."""

from __future__ import annotations

import pytest

from astropi.core.errors import SafetyError
from astropi.core.geometry import RaDec
from astropi.core.timekeeping import SIDEREAL_RATE_DEG_PER_S
from astropi.devices.backends.simulator import (
    SimulatedCamera,
    SimulatedCameraConfig,
    SimulatedFocuser,
    SimulatedMount,
    SimulatedMountConfig,
)
from astropi.devices.camera import ExposureRequest

M31 = RaDec(10.6847, 41.2690)


@pytest.fixture
async def mount(site, events):
    # Fast slew and guide rates keep the suite quick. Both change how long
    # a move takes, not the geometry it produces, which is what is tested.
    mount = SimulatedMount(
        site,
        events,
        SimulatedMountConfig(
            slew_rate_deg_per_s=400.0,
            max_slew_seconds=0.3,
            guide_rate_deg_per_s=SIDEREAL_RATE_DEG_PER_S * 8,
        ),
        seed=1,
    )
    await mount.connect()
    await mount.unpark()
    return mount


@pytest.fixture
async def camera(mount, events):
    camera = SimulatedCamera(
        mount, events, SimulatedCameraConfig(width=1200, height=900, time_scale=0.01, readout_s=0.0), seed=1
    )
    await camera.connect()
    return camera


async def test_goto_reports_the_target_but_points_elsewhere(mount):
    """The gap between belief and truth is the whole reason for plate solving."""
    await mount.slew_to(M31)
    await mount.wait_for_slew()

    status = await mount.status()
    assert status.position.separation_deg(M31) < 0.01, "mount should believe it is on target"

    error = mount.true_position().separation_deg(M31)
    assert 0.05 < error < 2.0, f"expected a realistic pointing error, got {error:.3f} deg"


async def test_sync_then_regoto_converges(mount):
    """Telling the mount the truth improves its next attempt."""
    await mount.slew_to(M31)
    await mount.wait_for_slew()
    before = mount.true_position().separation_deg(M31)

    await mount.sync_to(mount.true_position())
    await mount.slew_to(M31)
    await mount.wait_for_slew()
    after = mount.true_position().separation_deg(M31)

    assert after < before / 5, f"error should collapse after a sync: {before:.3f} -> {after:.3f}"
    assert after * 60 < 3.0, "should land within a few arcminutes"


async def test_park_returns_to_the_pole_and_stops_tracking(mount):
    await mount.park()
    status = await mount.status()
    assert status.position.dec_deg == pytest.approx(90.0, abs=0.01)
    assert not status.tracking


async def test_slew_below_the_horizon_is_refused(site, events):
    mount = SimulatedMount(
        site, events, SimulatedMountConfig(min_altitude_deg=10.0, max_slew_seconds=0.3), seed=1
    )
    await mount.connect()
    await mount.unpark()
    # The south celestial pole is permanently below a northern horizon.
    with pytest.raises(SafetyError):
        await mount.slew_to(RaDec(0.0, -89.0))


async def test_tracking_holds_position_while_misaligned_drifts(site, events):
    """A perfectly aligned mount holds; a misaligned one does not."""
    aligned = SimulatedMount(
        site,
        events,
        SimulatedMountConfig(
            polar_alt_error_deg=0.0,
            polar_az_error_deg=0.0,
            periodic_error_arcsec=0.0,
            seeing_arcsec=0.0,
            slew_rate_deg_per_s=400.0,
            max_slew_seconds=0.3,
        ),
        seed=1,
    )
    await aligned.connect()
    await aligned.unpark()
    await aligned.slew_to(M31)
    await aligned.wait_for_slew()

    start = aligned.true_position()
    # Ask where it is an hour of simulated time later.
    import time as _time

    later = aligned.true_position(at=_time.time() + 3600)
    assert start.separation_deg(later) * 3600 < 1.0, "an aligned mount should not drift"


async def test_camera_renders_stars_where_the_mount_truly_points(mount, camera):
    await mount.slew_to(M31)
    await mount.wait_for_slew()

    frame = await camera.expose(ExposureRequest(duration_s=2.0))
    assert frame.data.shape == (900, 1200)
    assert frame.data.max() > frame.data.mean() * 1.5, "frame should contain stars"

    truth = mount.true_position()
    assert frame.metadata["sim_true_ra_deg"] == pytest.approx(truth.ra_deg, abs=0.01)


async def test_focuser_changes_star_size(mount, events):
    focuser = SimulatedFocuser(seed=1)
    await focuser.connect()
    camera = SimulatedCamera(
        mount,
        events,
        SimulatedCameraConfig(width=800, height=600, time_scale=0.01, readout_s=0.0),
        focuser=focuser,
        seed=1,
    )
    await camera.connect()

    await focuser.move_to(31_400)
    await focuser.wait_for_move()
    sharp = focuser.hfd_px

    await focuser.move_to(31_400 + 4_000)
    await focuser.wait_for_move()
    blurred = focuser.hfd_px

    assert blurred > sharp * 2, "defocusing must visibly enlarge stars"


async def test_pulse_guide_moves_the_mount(mount):
    from astropi.devices.mount import GuideDirection

    # Away from the pole first: the declination axis is clamped at 90 degrees,
    # so a northward pulse from the park position has nowhere to go.
    await mount.slew_to(M31)
    await mount.wait_for_slew()

    before = mount.true_position()
    for _ in range(12):
        await mount.pulse_guide(GuideDirection.NORTH, 100)
    after = mount.true_position()
    assert after.separation_deg(before) * 3600 > 20.0, "guide pulses should move the mount"


async def test_declination_backlash_swallows_a_reversal(mount):
    """A reversing correction does nothing until the gear teeth re-engage."""
    from astropi.devices.mount import GuideDirection

    await mount.slew_to(M31)
    await mount.wait_for_slew()

    # Wind the axis firmly north so the backlash is fully taken up that way.
    for _ in range(12):
        await mount.pulse_guide(GuideDirection.NORTH, 100)

    before = mount.true_position()
    # One short pulse the other way should be absorbed, not acted on.
    await mount.pulse_guide(GuideDirection.SOUTH, 50)
    absorbed = mount.true_position()
    assert abs(absorbed.dec_deg - before.dec_deg) * 3600 < 2.0

    # Keep pushing and the axis eventually moves.
    for _ in range(12):
        await mount.pulse_guide(GuideDirection.SOUTH, 100)
    assert (before.dec_deg - mount.true_position().dec_deg) * 3600 > 20.0
