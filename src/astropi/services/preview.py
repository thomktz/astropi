"""Continuous camera preview.

A live view on its own settings: short, high-gain, binned exposures that
refresh on a loop. Quite separate from the imaging configuration, because
they answer different questions. A light frame is two minutes at low gain
to collect signal; a preview is two seconds at high gain to answer "is the
mount roughly pointed at the thing, and is it in focus".

Off until switched on, from the button under the main display. A rig that
starts exposing the moment a dashboard is opened is a rig doing something
nobody asked for, and the sensor is not free - it is the one a capture, a
plate solve and an autofocus all need.

While it runs it stands down on its own for anything with a real claim on
the sensor - a task, or a deliberate capture - and picks up afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from astropi.core.errors import DeviceBusyError, DeviceError
from astropi.core.events import EventBus, Topic
from astropi.devices.camera import Camera, ExposureRequest, Frame, FrameKind

logger = logging.getLogger(__name__)

#: How long to wait before looking again when the camera is unavailable.
IDLE_POLL_S = 0.4


@dataclass(slots=True)
class PreviewConfig:
    #: Off until asked for, from the toggle under the main display.
    #: Opening a dashboard should not set the camera working on its own.
    enabled: bool = False
    exposure_s: float = 2.0
    #: Higher than an imaging frame would use: a preview trades noise for
    #: seeing something now.
    gain: int = 200
    #: Binned by default. A full-resolution frame off a 26-megapixel sensor
    #: takes seconds to read and megabytes to send, for a picture that ends
    #: up a few hundred pixels wide on screen.
    binning: int = 2
    #: How often a new frame should start, measured from the start of the
    #: previous one - a period, not a pause afterwards. "Expose three
    #: seconds, new frame every ten" is a mental model that survives
    #: changing the exposure; "expose three, then wait four" is not, since
    #: the actual cadence then depends on two numbers and the readout time.
    #:
    #: Zero runs back to back, and so does any period shorter than a frame
    #: takes.
    period_s: float = 5.0


class PreviewService:
    """Keeps a recent frame available, without touching the frame store."""

    def __init__(
        self,
        camera: Camera,
        events: EventBus,
        *,
        is_busy,
        config: PreviewConfig | None = None,
    ) -> None:
        self._camera = camera
        self._events = events
        self._config = config or PreviewConfig()
        # Callable rather than a reference to the task engine, to keep this
        # service from depending on the sequencing layer.
        self._is_busy = is_busy

        self._latest: Frame | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._paused = False

    @property
    def config(self) -> PreviewConfig:
        return self._config

    @property
    def latest(self) -> Frame | None:
        return self._latest

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def update_config(self, **changes: object) -> PreviewConfig:
        for field, value in changes.items():
            if value is None:
                continue
            if not hasattr(self._config, field):
                raise DeviceError(f"unknown preview setting {field!r}")
            setattr(self._config, field, value)
        return self._config

    @asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Hold the camera for something else.

        A preview running continuously would otherwise own the sensor and
        every deliberate exposure would come back as "camera busy". This
        waits for the frame in flight, then keeps the loop off the camera
        until the caller is done.
        """
        self._paused = True
        try:
            async with self._lock:
                yield
        finally:
            self._paused = False

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A preview failure must never take the loop down: the
                # camera may simply be busy, or a cable may have been
                # knocked. Wait and look again.
                logger.exception("preview frame failed")
                await asyncio.sleep(IDLE_POLL_S)

    async def capture_once(self) -> Frame:
        """One preview frame, now, whether or not the loop is running.

        The refresh button under the display: a look at the sky without
        committing the sensor to a loop, and without putting a frame in the
        store that a real capture would then have to share space with.
        """
        return await self._expose()

    async def _tick(self) -> None:
        # Anything with a real claim on the camera comes first: a running
        # task, or an explicit exposure holding `paused`.
        if not self._config.enabled or self._paused or self._is_busy():
            await asyncio.sleep(IDLE_POLL_S)
            return

        started = time.monotonic()
        try:
            await self._expose()
        except DeviceBusyError:
            # Something claimed the camera between the check above and the
            # lock below. Normal, and not worth a stack trace.
            await asyncio.sleep(IDLE_POLL_S)
            return
        # Measured from when this frame started, so the period is the
        # cadence rather than the exposure plus a pause plus readout.
        remaining = self._config.period_s - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _expose(self) -> Frame:
        async with self._lock:
            if self._paused:
                raise DeviceBusyError("the camera is held by something else")
            frame = await self._camera.expose(
                ExposureRequest(
                    duration_s=self._config.exposure_s,
                    gain=self._config.gain,
                    binning=self._config.binning,
                    kind=FrameKind.PREVIEW,
                )
            )

        self._latest = frame
        self._events.publish(
            Topic.CAMERA_FRAME,
            role="camera",
            kind="preview",
            width=frame.shape[1],
            height=frame.shape[0],
            duration_s=self._config.exposure_s,
        )
        return frame
