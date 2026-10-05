"""The guide loop, against a mount that genuinely drifts."""

from __future__ import annotations

import asyncio
import math

import pytest

from astropi.core.errors import DeviceError
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
from astropi.devices.guider import GuideCalibration, GuidingState
from astropi.services.guiding import GuidingConfig, GuidingService, _without_drift

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
            # Sized for this rig's compressed time, like its 8x guide rate.
            calibration_ra_offset_arcsec_per_s=60.0,
            max_ra_offset_arcsec_per_s=60.0,
            calibration_steps=4,
            max_pulse_ms=300,
            settle_time_s=0.5,
            # Measured separately below; these tests are about the loop.
            null_drift=False,
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

    guider = GuidingService(camera, mount, events, GuidingConfig(exposure_s=0.001), pixel_scale_arcsec=2.0)
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


async def test_the_assistant_measures_an_unguided_mount(rig):
    """End to end: it watches, it measures, and it refuses to guide.

    The simulated mount has a deliberately misaligned polar axis, so
    there is a real drift underneath the seeing for it to find.
    """
    from astropi.sequencing.tasks.assistant import GuidingAssistantTask

    mount, _camera, guider, _ = rig

    await mount.unpark()
    await mount.set_tracking(True)

    class Rig:
        """Just enough observatory for the task to run against.

        The task asks an observatory for three things; a class that has
        those three things is a better test double than a mock, because
        it fails if the task starts asking for a fourth.
        """

        def __init__(self, mount, guider):
            self.site = site_of(mount)
            self._mount = mount
            self._guider = guider

        def require_guider(self):
            return self._guider

        def mount(self):
            return self._mount

        def guide_pixel_scale_arcsec(self):
            return 2.06

    task = GuidingAssistantTask(Rig(mount, guider), seconds=6.0, measure_backlash=False)
    report = await task.run()

    assert report.samples >= 3
    assert report.polar_error_confidence
    # Nothing was corrected while it watched.
    assert guider.calibration is None


def site_of(mount):
    """The observing site a simulated mount was built with."""
    return mount._site


async def test_the_assistant_recovers_a_polar_error_it_was_never_told(site):
    """The whole chain, against a misalignment only the simulator knows.

    Geometry to rendered pixels to detected centroid to a least-squares
    slope to arcminutes of polar error - and the number that comes out
    is compared with the one that went in. A deliberately large error,
    because the drift has to beat the seeing within a few seconds of
    wall clock rather than the couple of minutes a real run takes.
    """
    from astropi.sequencing.tasks.assistant import GuidingAssistantTask

    events = EventBus()
    mount = SimulatedMount(
        site,
        events,
        SimulatedMountConfig(
            polar_alt_error_deg=1.5,
            polar_az_error_deg=0.0,
            max_slew_seconds=0.2,
            seeing_arcsec=0.3,
            periodic_error_arcsec=0.0,
        ),
        seed=5,
    )
    main = SimulatedCameraConfig(width=1200, height=900, time_scale=0.001, readout_s=0.0)
    camera = SimulatedCamera(mount, events, guide_camera_config(main), seed=5)
    await mount.connect()
    await camera.connect()
    await mount.unpark()
    # East of the meridian and well south of the pole, where declination
    # drift is large and the conversion behaves.
    await mount.slew_to(RaDec(ra_deg=(mount._lst() - 60.0) % 360.0, dec_deg=10.0))
    await mount.wait_for_slew()
    await mount.set_tracking(True)

    guider = GuidingService(camera, mount, events, GuidingConfig(exposure_s=0.5), pixel_scale_arcsec=2.06)

    class Rig:
        def __init__(self):
            self.site = site

        def require_guider(self):
            return guider

        def mount(self):
            return mount

        def guide_pixel_scale_arcsec(self):
            return 2.06

    report = await GuidingAssistantTask(Rig(), seconds=25.0, measure_backlash=False).run()

    assert report.polar_error_arcmin is not None
    # 1.5 degrees is 90 arcminutes. Within a third of that is a real
    # measurement of a quantity nothing in the chain was handed.
    assert report.polar_error_arcmin == pytest.approx(90.0, rel=0.35)


async def test_calibration_takes_up_declination_backlash_first(site):
    """Backlash larger than two calibration pulses must not read as a dead axis.

    The first pulses north only take up slack in the gears; measured as
    part of the leg they looked like an axis that was not turning, and
    calibration gave up after two.
    """
    events = EventBus()
    mount = SimulatedMount(
        site,
        events,
        SimulatedMountConfig(
            slew_rate_deg_per_s=400.0,
            max_slew_seconds=0.2,
            guide_rate_deg_per_s=SIDEREAL_RATE_DEG_PER_S * 8,
            # Three calibration pulses' worth of slack.
            dec_backlash_arcsec=3 * 0.120 * 8 * 15.04,
            seeing_arcsec=0.2,
            periodic_error_arcsec=0.0,
        ),
        seed=5,
    )
    main = SimulatedCameraConfig(width=1200, height=900, time_scale=0.01, readout_s=0.0)
    camera = SimulatedCamera(mount, events, guide_camera_config(main), seed=5)
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
            # Sized for this rig's compressed time, like its 8x guide rate.
            calibration_ra_offset_arcsec_per_s=60.0,
            max_ra_offset_arcsec_per_s=60.0,
            calibration_steps=4,
            max_pulse_ms=300,
        ),
        pixel_scale_arcsec=2.06,
    )

    phases: list[str] = []
    report = guider._progress

    def record(phase: str, **detail) -> None:
        phases.append(phase)
        report(phase, **detail)

    guider._progress = record
    calibration = await guider.calibrate()

    assert calibration.dec_rate_arcsec_per_s == pytest.approx(calibration.ra_rate_arcsec_per_s, rel=0.35)
    assert "backlash" in phases


async def _drift_rig(site, *, rate_error: float = 1.0, fine: bool = True):
    """A rig with a strong polar drift, for the drift-cancelling tests."""
    events = EventBus()
    mount = SimulatedMount(
        site,
        events,
        SimulatedMountConfig(
            slew_rate_deg_per_s=400.0,
            max_slew_seconds=0.2,
            guide_rate_deg_per_s=SIDEREAL_RATE_DEG_PER_S * 8,
            # A drift that moves the star several pixels inside the short
            # measurement used here. Over a fraction of a pixel, where a
            # star's centroid is biased by where it sits on the pixel
            # grid, a measured drift is as much grid as sky.
            polar_alt_error_deg=5.0,
            polar_az_error_deg=-3.5,
            dec_backlash_arcsec=0.0,
            seeing_arcsec=0.3,
            periodic_error_arcsec=0.0,
            fine_guiding=fine,
        ),
        seed=9,
    )
    main = SimulatedCameraConfig(width=1200, height=900, time_scale=0.01, readout_s=0.0)
    camera = SimulatedCamera(mount, events, guide_camera_config(main), seed=9)
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
            # Sized for this rig's compressed time, like its 8x guide rate.
            calibration_ra_offset_arcsec_per_s=60.0,
            max_ra_offset_arcsec_per_s=60.0,
            calibration_steps=4,
            max_pulse_ms=300,
            search_radius_px=40,
            drift_min_s=4.0,
            drift_max_s=10.0,
            drift_precision_arcsec_per_min=3.0,
        ),
        pixel_scale_arcsec=2.06,
    )
    await guider.calibrate()
    # The mount now answers pulses at a different rate than calibrated -
    # as it would at another declination, or with a calibration gone stale.
    mount._config.guide_rate_deg_per_s *= rate_error

    measured: list[dict] = []
    report = guider._progress

    def record(phase: str, **detail) -> None:
        if phase == "drift_measuring":
            measured.append(detail)
        report(phase, **detail)

    guider._progress = record
    return guider, measured


async def _hold(guider, frames: int = 40, timeout_s: float = 90.0) -> list:
    """Run until the loop has held the star for this many frames."""
    await guider.start()
    try:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while guider._mode != "hold" or len(guider._samples) < frames:
            assert asyncio.get_running_loop().time() < deadline, f"stuck in {guider._mode}"
            await asyncio.sleep(0.2)
        return list(guider._samples)
    finally:
        await guider.stop()


@pytest.mark.parametrize("fine", [True, False], ids=["rate-and-steps", "pulses"])
async def test_drift_is_measured_then_cancelled(site, fine):
    """Measured with no corrections, then cancelled - and the star held.

    A calibration that is off is covered in `test_guidemodel`, on frames
    2.4 s apart: learning it takes minutes of sky, and this rig squeezes
    frames closer together but cannot squeeze the minutes.
    """
    rate_error = 1.0
    guider, measured = await _drift_rig(site, rate_error=rate_error, fine=fine)
    held = await _hold(guider)

    last = measured[-1]
    sky = (last["drift_x"], last["drift_y"])
    # There was a real drift to measure, in pixels a minute...
    assert math.hypot(*sky) > 3
    # ...and while holding, the star stays put against it.
    dec_rms = math.sqrt(sum(s.dec_error_arcsec**2 for s in held) / len(held))
    assert dec_rms < 1.5
    # And the drift is still being measured, corrections and all.
    now = guider._fit.drift
    assert now[0] * 60 == pytest.approx(sky[0], abs=0.35 * math.hypot(*sky))
    assert now[1] * 60 == pytest.approx(sky[1], abs=0.35 * math.hypot(*sky))


async def test_declination_never_reverses_against_the_drift(site):
    """Once a drift is being cancelled, Dec is only ever pushed one way.

    A reversing pulse pays the gear backlash, and everything sent while it
    does - the steady cancelling included - is swallowed; the star walks
    off for several frames, then snaps back. Past the lock the far side,
    the drift brings it back by itself.
    """
    guider, _ = await _drift_rig(site)
    held = await _hold(guider)

    fit = guider._fit
    dec_to_cancel = guider._trusted_response(fit, guider._calibration).solve(-fit.drift[0], -fit.drift[1])[1]
    cancelling = "north" if dec_to_cancel > 0 else "south"
    directions = {s.dec_direction for s in held if s.dec_pulse_ms > 0 or s.dec_steps}
    assert directions == {cancelling}


async def test_calibration_measures_in_the_units_guiding_uses(rig):
    """Rate offsets and steps, measured with rate offsets and steps."""
    _, _, guider, _ = rig
    calibration = await guider.calibrate()

    assert calibration.mode == "fine"
    # An arcsec of RA axis moves the star by the cosine of the declination.
    assert calibration.ra_sky_per_axis == pytest.approx(math.cos(math.radians(M31.dec_deg)), rel=0.2)
    assert calibration.dec_arcsec_per_step == pytest.approx(1_296_000 / 2_903_040, rel=0.2)
    assert calibration.dec_north_sign == 1.0


async def test_mounts_without_fine_controls_calibrate_with_pulses(site):
    guider, _ = await _drift_rig(site, fine=False)
    assert guider._calibration.mode == "pulse"


async def test_rate_guiding_never_pulses_ra_and_puts_tracking_back(site):
    """RA is corrected by speed alone - and tracking is plain again after."""
    guider, _ = await _drift_rig(site)
    mount = guider._mount
    held = await _hold(guider)

    assert all(s.ra_pulse_ms == 0 for s in held)
    assert any(s.ra_rate_offset != 0 for s in held)
    # Declination only ever in whole steps.
    assert all(isinstance(s.dec_steps, int) for s in held)
    assert mount._ra_offset == 0.0


def test_the_drift_during_calibration_is_taken_back_out():
    """What each leg would have measured on a sky that stood still."""
    true_ra, true_dec = (-0.266, -0.011), (0.016, -0.245)
    ra_units, dec_units = 60.0, 75.0
    drift_west, drift_north = (0.6, -0.4), (0.9, -0.3)
    west = (true_ra[0] * ra_units + drift_west[0], true_ra[1] * ra_units + drift_west[1])
    north = (true_dec[0] * dec_units + drift_north[0], true_dec[1] * dec_units + drift_north[1])
    measured = GuideCalibration(
        ra_rate_arcsec_per_s=1.0,
        dec_rate_arcsec_per_s=1.0,
        angle_deg=math.degrees(math.atan2(west[1], west[0])),
        pixel_scale_arcsec=2.06,
        calibrated_at=0.0,
        dec_at_calibration_deg=41.0,
        west_shift_px=west,
        north_shift_px=north,
        mode="fine",
        ra_response_px=(west[0] / ra_units, west[1] / ra_units),
        dec_response_px=(north[0] / dec_units, north[1] / dec_units),
    )

    corrected = _without_drift(measured, drift_west, drift_north)

    assert corrected.ra_response_px == pytest.approx(true_ra)
    assert corrected.dec_response_px == pytest.approx(true_dec)
    assert corrected.angle_deg == pytest.approx(math.degrees(math.atan2(true_ra[1], true_ra[0])))
    assert corrected.drift_corrected
    # Most of a leg is not drift: something else is wrong, so hands off.
    assert _without_drift(measured, (10.0, 0.0), drift_north) is None


async def test_the_worm_period_is_the_mounts_unless_set(rig):
    mount, _, guider, _ = rig
    assert guider.worm_period_s() == mount._config.periodic_error_period_s

    guider.update_config(worm_period_s=600.0)
    assert guider.worm_period_s() == 600.0


async def test_a_mount_failure_stops_the_mount_rather_than_walking_away(site):
    """The guide loop dying mid-correction must not leave an axis turning."""
    guider, _ = await _drift_rig(site)
    mount = guider._mount
    stopped: list[bool] = []
    abort = mount.abort_slew

    async def step_that_fails(direction, steps):
        raise DeviceError("axis 2 kept turning")

    async def recorded_abort():
        stopped.append(True)
        await abort()

    mount.step_dec = step_that_fails
    mount.abort_slew = recorded_abort
    guider.update_config(null_drift=False, dec_aggressiveness=2.0)
    await guider.start()
    try:
        deadline = asyncio.get_running_loop().time() + 60
        while guider.state is not GuidingState.ERROR:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.1)
    finally:
        await guider.stop()

    assert stopped
