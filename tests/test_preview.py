"""The live view, and its contention with everything else for the camera."""

from __future__ import annotations

import asyncio
import time

import pytest

from astropi.config import Settings
from astropi.devices.camera import ExposureRequest, FrameKind
from astropi.runtime import Observatory


@pytest.fixture
async def observatory(tmp_path):
    settings = Settings(
        camera_width=800,
        camera_height=600,
        simulator_time_scale=0.05,
        simulator_slew_rate_deg_per_s=400.0,
        simulator_solve_seconds=0.0,
        data_dir=tmp_path,
    )
    observatory = await Observatory.build(settings)
    try:
        yield observatory
    finally:
        await observatory.shutdown()


async def _wait_for_frame(observatory, *, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if observatory.preview.latest is not None:
            return observatory.preview.latest
        await asyncio.sleep(0.05)
    raise AssertionError("no preview frame arrived")


async def test_the_live_view_is_off_until_asked_for(observatory):
    """Opening a dashboard should not set the camera working on its own."""
    assert observatory.preview.config.enabled is False
    await asyncio.sleep(0.4)
    assert observatory.preview.latest is None


async def test_the_live_view_can_be_switched_off(observatory):
    """Off means no further frames; the last one stays where it is."""
    observatory.preview.update_config(enabled=True, exposure_s=0.05)
    await _wait_for_frame(observatory)

    observatory.preview.update_config(enabled=False)
    # Long enough for a frame already in flight to land, so what is held
    # afterwards is the last one rather than a racing one.
    await asyncio.sleep(0.6)
    settled = observatory.preview.latest

    await asyncio.sleep(0.5)
    assert observatory.preview.latest is settled


async def test_enabling_produces_binned_frames(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1, binning=2)
    frame = await _wait_for_frame(observatory)

    height, width = frame.shape
    assert (width, height) == (400, 300), "binning 2 halves each axis"
    assert frame.request.kind is FrameKind.PREVIEW


async def test_preview_frames_stay_out_of_the_frame_store(observatory):
    """A frame every couple of seconds would empty it of light frames."""
    observatory.preview.update_config(enabled=True, exposure_s=0.1)
    await _wait_for_frame(observatory)
    assert len(observatory.frames) == 0


async def test_the_view_prefers_whichever_frame_is_newer(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1)
    await _wait_for_frame(observatory)

    async with observatory.preview.paused():
        shot = await observatory.camera().expose(ExposureRequest(duration_s=0.1, kind=FrameKind.LIGHT))
    frame_id = observatory.frames.add(shot)

    stored = observatory.frames.get(frame_id)
    assert stored is not None
    assert stored.stored_at > observatory.preview.latest.started_at


async def test_an_explicit_exposure_takes_the_camera_from_the_preview(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1)
    await _wait_for_frame(observatory)

    # Without standing the loop down this raises "camera busy".
    async with observatory.preview.paused():
        frame = await observatory.camera().expose(ExposureRequest(duration_s=0.1))
    assert frame.shape == (600, 800), "full frame, not the binned preview"


async def test_a_task_runs_while_the_live_view_is_on(observatory):
    """The bug this guards against failed a whole GoTo.

    Declining to *start* a preview frame once a task is running is not
    enough: a task beginning while a frame is already in flight collides
    with it, and the task is the one that fails. The engine holds the
    camera for the task's whole run instead.
    """
    from astropi.core.geometry import RaDec
    from astropi.sequencing.tasks import GotoAndCenterTask

    observatory.preview.update_config(enabled=True, exposure_s=0.1)
    await _wait_for_frame(observatory)

    await observatory.mount().unpark()
    task = GotoAndCenterTask(observatory, RaDec(10.6847, 41.269), tolerance_arcmin=5.0, exposure_s=0.2)
    observatory.tasks.submit(task)

    deadline = time.time() + 30
    while time.time() < deadline and not task.progress.state.is_terminal:
        await asyncio.sleep(0.05)

    assert str(task.progress.state) == "succeeded", task.error


async def test_the_live_view_follows_back_to_back(observatory):
    """No pause between frames: the next starts as soon as one lands."""
    observatory.preview.update_config(enabled=True, exposure_s=1.0)
    await _wait_for_frame(observatory)
    first = observatory.preview.seq
    # At a time scale of 0.05 a one second exposure is 50 ms of wall clock,
    # plus the simulated readout.
    await asyncio.sleep(2.0)
    assert observatory.preview.seq - first >= 3
    assert observatory.preview.fps is not None


async def test_changing_a_setting_restarts_the_stream_on_it(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1, binning=1)
    await _wait_for_frame(observatory)
    observatory.preview.update_config(binning=2)
    deadline = time.time() + 5
    while time.time() < deadline and observatory.preview.latest.shape != (300, 400):
        await asyncio.sleep(0.05)
    assert observatory.preview.latest.shape == (300, 400)


async def test_the_live_picture_is_rendered_once_per_frame(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1)
    await _wait_for_frame(observatory)
    seq, jpeg = await observatory.preview.jpeg()
    assert jpeg[:2] == b"\xff\xd8", "a JPEG"
    again = await observatory.preview.jpeg()
    assert again[0] >= seq


async def test_a_goto_to_the_moon_tracks_it_and_does_not_solve(observatory):
    """No stars to solve on, and sidereal tracking would lose it."""
    from astropy.coordinates import EarthLocation  # noqa: F401  (astropy import warms the cache)

    from astropi.core.geometry import RaDec
    from astropi.devices.mount import TrackingRate
    from astropi.sequencing.tasks import GotoAndCenterTask

    await observatory.mount().unpark()
    # Somewhere always above the horizon, standing in for wherever the
    # Moon is when the test runs.
    task = GotoAndCenterTask(
        observatory, RaDec(0.0, 80.0), solve=False, tracking_rate=TrackingRate.LUNAR, exposure_s=0.2
    )
    observatory.tasks.submit(task)
    deadline = time.time() + 30
    while time.time() < deadline and not task.progress.state.is_terminal:
        await asyncio.sleep(0.05)

    assert str(task.progress.state) == "succeeded", task.error
    status = await observatory.mount().status()
    assert status.tracking_rate is TrackingRate.LUNAR
    assert not any(
        "solve" in message.lower() and "not plate solved" not in message.lower()
        for message in task.progress.messages
    )
