"""The guide loop, against a mount that genuinely drifts."""

from __future__ import annotations

import asyncio
import math

import pytest

from astropi.core.events import EventBus, Topic
from astropi.core.geometry import RaDec
from astropi.core.timekeeping import SIDEREAL_RATE_DEG_PER_S
from astropi.devices.backends.simulator import (
    SimulatedCamera,
    SimulatedCameraConfig,
    SimulatedMount,
    SimulatedMountConfig,
    guide_camera_config,
)
from astropi.devices.guider import GuidingState
from astropi.services.guiding import GuidingConfig, GuidingService

M31 = RaDec(10.6847, 41.2690)


@pytest.fixture
async def rig(site):
    events = EventBus()
    mount = SimulatedMount(
        site,
        events,
        SimulatedMountConfig(
            slew_rate_deg_per_s=400.0,
            max_slew_seconds=0.2,
            # A generous guide rate so calibration pulses stay short and the
            # test runs in seconds rather than minutes.
            guide_rate_deg_per_s=SIDEREAL_RATE_DEG_PER_S * 8,
            dec_backlash_arcsec=2.0,
            seeing_arcsec=0.4,
            periodic_error_arcsec=6.0,
        ),
        seed=11,
    )
    main = SimulatedCameraConfig(width=1200, height=900, time_scale=0.01, readout_s=0.0)
    camera = SimulatedCamera(mount, events, guide_camera_config(main), seed=11)

    await mount.connect()
    await camera.connect()
    await mount.unpark()
    await mount.slew_to(M31)
    await mount.wait_for_slew()

    guider = GuidingService(
        camera,
        mount,
        events,
        GuidingConfig(
            exposure_s=1.0,
            calibration_pulse_ms=120,
            calibration_steps=4,
            max_pulse_ms=300,
            settle_time_s=0.5,
        ),
        pixel_scale_arcsec=2.06,
    )
    return mount, camera, guider, events


async def test_calibration_measures_both_axes(rig):
    _, _, guider, _ = rig
    calibration = await guider.calibrate()

    assert calibration.ra_rate_arcsec_per_s > 0
    assert calibration.dec_rate_arcsec_per_s > 0
    assert -180 <= calibration.angle_deg <= 180
    assert calibration.pixel_scale_arcsec == pytest.approx(2.06)


async def test_calibration_is_invalidated_on_request(rig):
    _, _, guider, _ = rig
    await guider.calibrate()
    assert guider.calibration is not None

    guider.invalidate_calibration()
    assert guider.calibration is None, "rotating the camera must void the calibration"


async def test_guiding_emits_samples_and_keeps_the_star(rig):
    _, _, guider, events = rig
    samples: list[dict] = []

    async def collect() -> None:
        async with events.subscribe() as stream:
            async for event in stream:
                if event.topic is Topic.GUIDING_SAMPLE:
                    samples.append(event.payload)

    collector = asyncio.create_task(collect())
    try:
        await guider.start()
        await asyncio.sleep(4.0)
    finally:
        await guider.stop()
        collector.cancel()

    assert len(samples) >= 3, "the loop should produce samples"

    status = await guider.status()
    assert status.state is GuidingState.STOPPED
    assert status.rms_total_arcsec is not None

    # The star must stay near the lock point, not wander off.
    worst = max(math.hypot(s["ra_error_arcsec"], s["dec_error_arcsec"]) for s in samples)
    assert worst < 30.0, f"guiding lost control, worst error {worst:.1f} arcsec"


async def test_small_errors_do_not_produce_pulses():
    """Chasing seeing injects more motion than it removes."""
    config = GuidingConfig(min_move_arcsec=0.5)
    service = GuidingService.__new__(GuidingService)
    service._config = config

    assert GuidingService._pulse_for(service, 0.2, 10.0, 0.7) == 0
    assert GuidingService._pulse_for(service, 5.0, 10.0, 0.7) > 0


async def test_pulses_are_capped():
    """One bad frame must not send the mount bolting across the sky."""
    service = GuidingService.__new__(GuidingService)
    service._config = GuidingConfig(max_pulse_ms=400)

    assert GuidingService._pulse_for(service, 10_000.0, 1.0, 1.0) == 400


async def test_guiding_without_a_star_fails_clearly(site):
    """An empty frame should say so, not silently guide on noise."""
    from astropi.core.errors import AstropiError

    events = EventBus()
    mount = SimulatedMount(site, events, SimulatedMountConfig(max_slew_seconds=0.1), seed=1)
    # A tiny, very short exposure: nothing will be detectable in it.
    camera = SimulatedCamera(
        mount,
        events,
        SimulatedCameraConfig(width=64, height=64, time_scale=0.001, readout_s=0.0),
        seed=1,
    )
    await mount.connect()
    await camera.connect()
    # Tracking, because a guider now refuses a mount that is not - and
    # this test is about an empty frame, not about that.
    await mount.unpark()
    await mount.set_tracking(True)

    guider = GuidingService(
        camera, mount, events, GuidingConfig(exposure_s=0.001), pixel_scale_arcsec=2.0
    )
    with pytest.raises(AstropiError, match="no guide star"):
        await guider.calibrate()


async def test_automatic_selection_keeps_clear_of_the_frame_edge(rig):
    """An edge star is one dither away from leaving the sensor."""
    _, _, guider, _ = rig
    stars = await guider._expose_and_detect()
    assert stars, "the test field should contain stars"

    frame = guider.latest_frame
    assert frame is not None
    height, width = frame.shape
    margin = guider._config.edge_margin

    chosen = await guider._acquire_star()
    assert margin * width <= chosen.x <= (1 - margin) * width
    assert margin * height <= chosen.y <= (1 - margin) * height


async def test_a_manual_pick_near_the_edge_is_honoured(rig):
    """The margin governs the automatic choice, not the operator's."""
    _, _, guider, _ = rig
    stars = await guider._expose_and_detect()
    frame = guider.latest_frame
    assert frame is not None
    height, width = frame.shape

    outer = [
        star
        for star in stars
        if star.x < 0.1 * width or star.x > 0.9 * width or star.y < 0.1 * height or star.y > 0.9 * height
    ]
    if not outer:
        pytest.skip("no star near the edge in this field")

    picked = guider.select_star(outer[0].x, outer[0].y)
    assert picked.x == pytest.approx(outer[0].x)


async def test_a_field_with_only_edge_stars_says_so(rig):
    """Better a clear message than guiding on a star about to leave."""
    from astropi.core.errors import AstropiError

    _, _, guider, _ = rig
    await guider._expose_and_detect()
    # Nothing qualifies once the margin covers the whole frame.
    guider._config.edge_margin = 0.5

    with pytest.raises(AstropiError, match="clear of the outer"):
        await guider._acquire_star()


async def test_settings_change_takes_effect_on_the_next_frame(rig):
    """Changing exposure mid-run must not need a restart.

    What you are usually trying to fix is the guiding happening in front of
    you, so the loop reads its configuration each cycle rather than
    capturing it at start.
    """
    _, _, guider, _ = rig
    guider.update_config(exposure_s=4.5, gain=123)

    assert guider.config.exposure_s == pytest.approx(4.5)
    assert guider.config.gain == 123


async def test_unknown_settings_are_refused(rig):
    from astropi.core.errors import AstropiError

    _, _, guider, _ = rig
    with pytest.raises(AstropiError, match="unknown guiding setting"):
        guider.update_config(nonsense=1)


@pytest.mark.parametrize(
    ("mode", "north_allowed", "south_allowed"),
    [
        ("auto", True, True),
        ("north", True, False),
        ("south", False, True),
        ("off", False, False),
    ],
)
async def test_declination_mode_limits_which_way_corrections_go(rig, mode, north_allowed, south_allowed):
    """One-directional dec guiding never pays the backlash on a reversal."""
    from astropi.devices.mount import GuideDirection
    from astropi.services.guiding import DecGuideMode

    _, _, guider, _ = rig
    guider.update_config(dec_mode=DecGuideMode(mode))

    assert guider._dec_allowed(GuideDirection.NORTH) is north_allowed
    assert guider._dec_allowed(GuideDirection.SOUTH) is south_allowed


async def test_the_idle_loop_keeps_the_guide_view_live(rig):
    """A stopped guider still produces frames, so the sub-display is not dark."""
    _, _, guider, _ = rig
    guider.update_config(preview_enabled=True, preview_period_s=0.0)
    await guider.start_preview()
    try:
        assert guider.preview_running is True
        for _ in range(60):
            if guider.latest_frame is not None:
                break
            await asyncio.sleep(0.05)
        first = guider.latest_frame
        assert first is not None, "the idle loop produced no frame"

        # And it keeps going, rather than showing one stale picture.
        for _ in range(60):
            if guider.latest_frame is not first:
                break
            await asyncio.sleep(0.05)
        assert guider.latest_frame is not first
    finally:
        await guider.stop_preview()


async def test_the_idle_loop_does_not_fight_calibration(rig):
    """One owner of the guide camera, whatever else is running.

    Without a lock the idle exposure and a calibration frame reach the
    sensor together and one of them comes back "an exposure is already in
    progress" - which, during calibration, fails the whole thing.
    """
    _, _, guider, _ = rig
    guider.update_config(preview_enabled=True, preview_period_s=0.0)
    await guider.start_preview()
    try:
        calibration = await guider.calibrate()
        assert calibration.ra_rate_arcsec_per_s > 0
    finally:
        await guider.stop_preview()


async def test_the_idle_loop_stands_down_while_guiding(rig):
    """Guiding keeps its cadence; the idle loop is not competing for frames."""
    _, _, guider, events = rig
    samples: list[dict] = []

    async def collect() -> None:
        async with events.subscribe() as stream:
            async for event in stream:
                if event.topic is Topic.GUIDING_SAMPLE:
                    samples.append(event.payload)

    collector = asyncio.create_task(collect())
    guider.update_config(preview_enabled=True, preview_period_s=0.0)
    await guider.start_preview()
    try:
        await guider.start()
        await asyncio.sleep(3.0)
        status = await guider.status()
        assert status.state in {GuidingState.SETTLING, GuidingState.GUIDING}
        assert len(samples) >= 2, "the guide loop was starved of the camera"
    finally:
        await guider.stop()
        await guider.stop_preview()
        collector.cancel()


async def test_guiding_refuses_a_mount_that_is_not_tracking(rig):
    """Guiding corrects tracking; it cannot replace it.

    On a stopped mount the field walks out of frame at fifteen
    arcseconds a second while the loop answers with corrections of two.
    What that looks like from the outside is a guide loop that has gone
    mad: right ascension climbing past twenty arcseconds with the pulse
    pinned to its ceiling, which is exactly what it did.
    """
    from astropi.core.errors import AstropiError

    mount, _, guider, _ = rig
    await mount.set_tracking(False)

    with pytest.raises(AstropiError, match="not tracking"):
        await guider.calibrate()

    with pytest.raises(AstropiError, match="not tracking"):
        await guider.start()
