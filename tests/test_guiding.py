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

    guider = GuidingService(
        camera, mount, events, GuidingConfig(exposure_s=0.001), pixel_scale_arcsec=2.0
    )
    with pytest.raises(AstropiError, match="no guide star"):
        await guider.calibrate()
