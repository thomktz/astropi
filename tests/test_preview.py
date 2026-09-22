"""The live view, and its contention with everything else for the camera."""

from __future__ import annotations

import asyncio
import time
from itertools import pairwise

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


async def test_one_frame_can_be_taken_without_the_loop(observatory):
    """The refresh button: a look at the sky, not a commitment."""
    observatory.preview.update_config(exposure_s=0.05)
    frame = await observatory.preview.capture_once()

    assert observatory.preview.latest is frame
    assert observatory.preview.config.enabled is False, "one frame must not start the loop"
    assert observatory.frames.latest() is None, "a look at the sky is not a capture"


async def test_the_live_view_can_be_switched_off(observatory):
    """Off means no further frames; the last one stays where it is."""
    observatory.preview.update_config(enabled=True, exposure_s=0.05, period_s=0.0)
    await _wait_for_frame(observatory)

    observatory.preview.update_config(enabled=False)
    # Long enough for a frame already in flight to land, so what is held
    # afterwards is the last one rather than a racing one.
    await asyncio.sleep(0.6)
    settled = observatory.preview.latest

    await asyncio.sleep(0.5)
    assert observatory.preview.latest is settled


async def test_enabling_produces_binned_frames(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1, period_s=0.0, binning=2)
    frame = await _wait_for_frame(observatory)

    height, width = frame.shape
    assert (width, height) == (400, 300), "binning 2 halves each axis"
    assert frame.request.kind is FrameKind.PREVIEW


async def test_preview_frames_stay_out_of_the_frame_store(observatory):
    """A frame every couple of seconds would empty it of light frames."""
    observatory.preview.update_config(enabled=True, exposure_s=0.1, period_s=0.0)
    await _wait_for_frame(observatory)
    assert len(observatory.frames) == 0


async def test_the_view_prefers_whichever_frame_is_newer(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1, period_s=0.0)
    await _wait_for_frame(observatory)

    async with observatory.preview.paused():
        shot = await observatory.camera().expose(
            ExposureRequest(duration_s=0.1, kind=FrameKind.LIGHT)
        )
    frame_id = observatory.frames.add(shot)

    stored = observatory.frames.get(frame_id)
    assert stored is not None
    assert stored.stored_at > observatory.preview.latest.started_at


async def test_an_explicit_exposure_takes_the_camera_from_the_preview(observatory):
    observatory.preview.update_config(enabled=True, exposure_s=0.1, period_s=0.0)
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

    observatory.preview.update_config(enabled=True, exposure_s=0.1, period_s=0.0)
    await _wait_for_frame(observatory)

    await observatory.mount().unpark()
    task = GotoAndCenterTask(
        observatory, RaDec(10.6847, 41.269), tolerance_arcmin=5.0, exposure_s=0.2
    )
    observatory.tasks.submit(task)

    deadline = time.time() + 30
    while time.time() < deadline and not task.progress.state.is_terminal:
        await asyncio.sleep(0.05)

    assert str(task.progress.state) == "succeeded", task.error


async def test_the_period_is_a_cadence_not_a_pause(observatory):
    """"Every five seconds" has to mean every five seconds.

    Sleeping the period *after* each frame made the real cadence exposure
    plus readout plus period, which changes whenever the exposure does.
    """
    observatory.preview.update_config(enabled=True, exposure_s=0.2, period_s=1.0)

    starts: list[float] = []
    deadline = time.time() + 8
    while time.time() < deadline and len(starts) < 3:
        latest = observatory.preview.latest
        if latest and (not starts or latest.started_at != starts[-1]):
            starts.append(latest.started_at)
        await asyncio.sleep(0.02)

    assert len(starts) >= 3, "not enough frames to measure a cadence"
    for first, second in pairwise(starts):
        assert second - first == pytest.approx(1.0, abs=0.25)
