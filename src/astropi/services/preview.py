"""Continuous camera preview.

A live view on its own settings: short, high-gain, binned exposures that
follow one another as fast as the camera can deliver them. Quite
separate from the imaging configuration, because they answer different
questions. A light frame is two minutes at low gain to collect signal; a
preview is a fraction of a second at high gain to answer "is the mount
roughly pointed at the thing, and is it in focus".

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
import io
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import numpy as np

from astropi.core.errors import DeviceBusyError, DeviceError
from astropi.core.events import EventBus, Topic
from astropi.devices.camera import Camera, ExposureRequest, Frame, FrameKind
from astropi.storage.frames import autostretch_lut

logger = logging.getLogger(__name__)

#: How long to wait before looking again when the camera is unavailable.
IDLE_POLL_S = 0.4


@dataclass(slots=True)
class PreviewConfig:
    #: Off until asked for, from the toggle under the main display.
    #: Opening a dashboard should not set the camera working on its own.
    enabled: bool = False
    exposure_s: float = 0.5
    #: Higher than an imaging frame would use: a preview trades noise for
    #: seeing something now.
    gain: int = 200
    #: Binned by default. A full-resolution frame off a 26-megapixel sensor
    #: is megabytes to move for a picture that ends up a few hundred
    #: pixels wide on screen; zoom in to focus and it can come down to 1.
    binning: int = 2


#: The longest side of the picture sent to the browser.
DISPLAY_SIZE = 1600
#: How often, at most, the dashboard is told a live frame arrived. The
#: picture itself streams at the camera's rate; this is only the caption.
ANNOUNCE_EVERY_S = 1.0


def render_jpeg(frame: Frame, *, stretch: bool, max_dimension: int = DISPLAY_SIZE) -> bytes:
    """A frame as a JPEG for the live view.

    JPEG rather than the PNG a stored frame gets: a live picture is
    replaced several times a second, and encoding a PNG of it costs more
    than the frame took to arrive.
    """
    from PIL import Image

    reduced = _bin_down(frame.data, max(1, -(-max(frame.data.shape) // max_dimension)))
    bit_depth = frame.sensor.bit_depth
    if stretch:
        pixels = autostretch_lut(reduced, bit_depth=bit_depth)[reduced]
    else:
        pixels = (reduced >> max(0, bit_depth - 8)).clip(0, 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _bin_down(data: np.ndarray, step: int) -> np.ndarray:
    """Average `step` x `step` blocks, in integers.

    The same averaging the stored-frame renderer does, without converting
    a 26-megapixel frame to floating point first - which on a Pi took
    longer than the camera takes to deliver the next one.
    """
    if step <= 1:
        return data
    height, width = (data.shape[0] // step) * step, (data.shape[1] // step) * step
    total = np.zeros((height // step, width // step), dtype=np.uint32)
    for dy in range(step):
        for dx in range(step):
            total += data[dy:height:step, dx:width:step]
    return (total // (step * step)).astype(np.uint16)


class PreviewService:
    """Keeps the newest live frame, and a rendering of it, for the dashboard."""

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
        self._seq = 0
        self._new_frame = asyncio.Condition()
        self._rendered: dict[bool, tuple[int, bytes]] = {}
        self._render_lock = asyncio.Lock()
        self._fps: float | None = None
        self._announced_at = 0.0

        self._task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._paused = False

    @property
    def config(self) -> PreviewConfig:
        return self._config

    @property
    def latest(self) -> Frame | None:
        return self._latest

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def fps(self) -> float | None:
        """Frames a second actually arriving, while the stream runs."""
        return self._fps if self.streaming else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def streaming(self) -> bool:
        return self._stream_task is not None and not self._stream_task.done()

    def update_config(self, **changes: object) -> PreviewConfig:
        for field, value in changes.items():
            if value is None:
                continue
            if not hasattr(self._config, field):
                raise DeviceError(f"unknown preview setting {field!r}")
            setattr(self._config, field, value)
        # A stream runs on the settings it started with; restart it on the
        # new ones. The loop picks it straight back up if still enabled.
        self._interrupt()
        return self._config

    @asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Hold the camera for something else.

        A running stream owns the sensor, and every deliberate exposure
        would otherwise come back as "camera busy". This stops the stream,
        then keeps the loop off the camera until the caller is done.
        """
        self._paused = True
        self._interrupt()
        try:
            async with self._lock:
                yield
        finally:
            self._paused = False

    def _interrupt(self) -> None:
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._interrupt()
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _wanted(self) -> bool:
        return self._config.enabled and not self._paused and not self._is_busy()

    async def _run(self) -> None:
        while True:
            if not self._wanted():
                await asyncio.sleep(IDLE_POLL_S)
                continue
            self._stream_task = asyncio.create_task(self._stream())
            # Waited on rather than awaited: a stream cancelled to make way
            # for a capture is not this loop being cancelled.
            await asyncio.wait({self._stream_task})
            if not self._stream_task.cancelled() and self._stream_task.exception() is not None:
                # A preview failure must never take the loop down: the
                # camera may simply be busy, or a cable may have been
                # knocked. Wait and look again.
                error = self._stream_task.exception()
                if not isinstance(error, DeviceBusyError):
                    logger.error("live view stopped: %s", error, exc_info=error)
                await asyncio.sleep(IDLE_POLL_S)

    async def _stream(self) -> None:
        request = ExposureRequest(
            duration_s=self._config.exposure_s,
            gain=self._config.gain,
            binning=self._config.binning,
            kind=FrameKind.PREVIEW,
        )
        async with self._lock:
            stream = getattr(self._camera, "stream", None)
            frames = stream(request) if stream is not None else self._one_at_a_time(request)
            async with contextlib.aclosing(frames):
                last = None
                async for frame in frames:
                    now = time.monotonic()
                    if last is not None:
                        rate = 1.0 / max(now - last, 1e-6)
                        self._fps = rate if self._fps is None else 0.7 * self._fps + 0.3 * rate
                    last = now
                    await self._publish(frame)
                    if not self._wanted():
                        return

    async def _one_at_a_time(self, request: ExposureRequest) -> AsyncIterator[Frame]:
        """A stream, for a camera that can only take single exposures."""
        while True:
            yield await self._camera.expose(request)

    async def _publish(self, frame: Frame) -> None:
        self._latest = frame
        async with self._new_frame:
            self._seq += 1
            self._new_frame.notify_all()
        # The picture streams on its own; this is the caption under it,
        # and once a second is plenty for a number that says how fresh it is.
        now = time.monotonic()
        if now - self._announced_at >= ANNOUNCE_EVERY_S:
            self._announced_at = now
            self._events.publish(
                Topic.CAMERA_FRAME,
                role="camera",
                kind="preview",
                width=frame.shape[1],
                height=frame.shape[0],
                duration_s=frame.request.duration_s,
                fps=None if self._fps is None else round(self._fps, 1),
            )

    async def wait_for_frame(self, after: int, timeout: float) -> int:
        """Block until a frame newer than `after` arrives. Returns its number."""
        async with self._new_frame:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new_frame.wait_for(lambda: self._seq > after), timeout)
            return self._seq

    async def jpeg(self, *, stretch: bool = True) -> tuple[int, bytes] | None:
        """The newest frame as a JPEG, rendered once however many are watching."""
        frame, seq = self._latest, self._seq
        if frame is None:
            return None
        cached = self._rendered.get(stretch)
        if cached is not None and cached[0] == seq:
            return cached
        async with self._render_lock:
            cached = self._rendered.get(stretch)
            if cached is not None and cached[0] == seq:
                return cached
            data = await asyncio.to_thread(render_jpeg, frame, stretch=stretch)
            self._rendered[stretch] = (seq, data)
            return seq, data
