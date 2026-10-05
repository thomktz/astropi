"""Camera control and frame previews."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import CameraOut, ControlOut, CoolingIn, ExposureIn
from astropi.devices import CameraDevice, DeviceRole
from astropi.devices.camera import ControlSpec, ExposureRequest, FrameKind
from astropi.storage import to_png

router = APIRouter(prefix="/camera", tags=["camera"])
_BOOT = f"{time.time():.0f}"


def _camera(observatory: ObservatoryDep, role: str) -> CameraDevice:
    wanted = DeviceRole.GUIDE_CAMERA if role == "guide" else DeviceRole.CAMERA
    return observatory.registry.require_connected(wanted, CameraDevice)


@router.get("", response_model=CameraOut)
async def status(observatory: ObservatoryDep, role: str = "main") -> CameraOut:
    camera = _camera(observatory, role)
    snapshot = await camera.status()
    device_role = DeviceRole.GUIDE_CAMERA if role == "guide" else DeviceRole.CAMERA
    scale = observatory.pixel_scale_arcsec(device_role)
    sensor = snapshot.sensor
    return CameraOut(
        state=str(snapshot.state),
        gain=snapshot.gain,
        offset=snapshot.offset,
        binning=snapshot.binning,
        exposure_progress=snapshot.exposure_progress,
        sensor={
            "width": sensor.width,
            "height": sensor.height,
            "pixel_size_um": sensor.pixel_size_um,
            "bit_depth": sensor.bit_depth,
            "bayer_pattern": sensor.bayer_pattern,
        },
        cooling={
            "supported": snapshot.cooling.supported,
            "enabled": snapshot.cooling.enabled,
            "target_c": snapshot.cooling.target_c,
            "sensor_c": snapshot.cooling.sensor_c,
            "power_percent": snapshot.cooling.power_percent,
            "dew_heater": snapshot.cooling.dew_heater,
        },
        pixel_scale_arcsec=round(scale, 4),
        field_of_view_deg=[
            round(sensor.width * scale / 3600.0, 4),
            round(sensor.height * scale / 3600.0, 4),
        ],
    )


@router.post("/expose")
async def expose(payload: ExposureIn, observatory: ObservatoryDep, role: str = "main") -> dict:
    """Take one frame and keep it in the frame store.

    Returns the frame's id rather than its pixels; the image is fetched
    separately as a PNG, so a client that only wants the metadata is not
    made to download tens of megabytes.
    """
    camera = _camera(observatory, role)
    request = ExposureRequest(
        duration_s=payload.duration_s,
        gain=payload.gain,
        offset=payload.offset,
        binning=payload.binning,
        kind=FrameKind(payload.kind),
    )
    # The preview loop owns the sensor between frames; without standing it
    # down first, every deliberate exposure would come back "camera busy".
    if observatory.preview is not None and role != "guide":
        async with observatory.preview.paused():
            frame = await camera.expose(request)
    else:
        frame = await camera.expose(request)
    frame_id = observatory.frames.add(frame)
    stored = observatory.frames.get(frame_id)
    assert stored is not None
    return stored.summary()


@router.post("/abort")
async def abort(observatory: ObservatoryDep, role: str = "main") -> dict:
    await _camera(observatory, role).abort_exposure()
    return {"aborted": True}


@router.post("/cooling")
async def cooling(payload: CoolingIn, observatory: ObservatoryDep, role: str = "main") -> dict:
    await _camera(observatory, role).set_cooling(payload.enabled, payload.target_c)
    snapshot = await _camera(observatory, role).status()
    return {
        "enabled": snapshot.cooling.enabled,
        "target_c": snapshot.cooling.target_c,
        "sensor_c": snapshot.cooling.sensor_c,
    }


def _control_out(spec: ControlSpec) -> ControlOut:
    return ControlOut(
        name=spec.name,
        label=spec.label,
        value=spec.value,
        writable=spec.writable,
        kind=str(spec.kind),
        minimum=spec.minimum,
        maximum=spec.maximum,
        default=spec.default,
        step=spec.step,
        unit=spec.unit,
        supports_auto=spec.supports_auto,
        auto=spec.auto,
        description=spec.description,
    )


@router.get("/controls", response_model=list[ControlOut])
async def controls(observatory: ObservatoryDep, role: str = "main") -> list[ControlOut]:
    """Every setting the camera has, read-only ones included.

    The client renders whatever comes back rather than knowing the list, so
    a camera with a dew heater grows a dew heater switch and one without
    simply does not.
    """
    return [_control_out(spec) for spec in await _camera(observatory, role).controls()]


class ControlIn(BaseModel):
    value: float


@router.put("/controls/{name}", response_model=ControlOut)
async def set_control(
    name: str, payload: ControlIn, observatory: ObservatoryDep, role: str = "main"
) -> ControlOut:
    # A control this camera does not have, or one it will not let you
    # write, raises `CapabilityError` - which the app already answers with
    # 501, the same as asking an uncooled guide head to cool.
    return _control_out(await _camera(observatory, role).set_control(name, payload.value))


@router.get("/frames")
async def frames(observatory: ObservatoryDep) -> list[dict]:
    return [stored.summary() for stored in observatory.frames.list()]


@router.get("/frames/latest")
async def latest_frame(observatory: ObservatoryDep) -> dict:
    stored = observatory.frames.latest()
    if stored is None:
        raise HTTPException(status_code=404, detail="no frames captured yet")
    return stored.summary()


@router.get("/frames/{frame_id}/preview.png")
async def preview(
    frame_id: str,
    observatory: ObservatoryDep,
    stretch: bool = True,
    max_dimension: int = Query(default=1400, ge=100, le=6000),
) -> Response:
    """Render a frame as a stretched PNG for display.

    Stretched by default because a raw astronomical frame shown linearly is
    a black rectangle - the signal sits in a narrow band just above the sky
    background.
    """
    stored = observatory.frames.get(frame_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"no frame {frame_id}")
    try:
        png = to_png(stored.frame.data, max_dimension=max_dimension, stretch=stretch)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


class PreviewIn(BaseModel):
    """Live-view settings. Everything optional; only what is sent changes."""

    enabled: bool | None = None
    exposure_s: float | None = Field(default=None, gt=0, le=120)
    gain: int | None = Field(default=None, ge=0, le=1000)
    binning: int | None = Field(default=None, ge=1, le=8)


def _preview_out(preview) -> dict:
    config = preview.config
    return {
        "enabled": config.enabled,
        "exposure_s": config.exposure_s,
        "gain": config.gain,
        "binning": config.binning,
        "running": preview.running,
        "streaming": preview.streaming,
        "fps": None if preview.fps is None else round(preview.fps, 1),
    }


@router.get("/preview")
async def get_preview(observatory: ObservatoryDep) -> dict:
    return _preview_out(observatory.require_preview())


@router.put("/preview")
async def set_preview(payload: PreviewIn, observatory: ObservatoryDep) -> dict:
    """Change the live view, including turning it off.

    Separate from the imaging settings on purpose: a preview is a short,
    high-gain, binned frame answering "is it pointed at the thing and is it
    in focus", which is a different question from the one a light frame is
    collecting signal for.
    """
    preview = observatory.require_preview()
    preview.update_config(**payload.model_dump(exclude_none=True))
    return _preview_out(preview)


def _live(preview) -> bool:
    """Whether the live view is on - including while it stands down.

    Switched on, not "frames arriving this instant": a stream pauses for
    every capture and restarts after it, and a display that flipped to the
    capture and back each time would flicker on every frame taken.
    """
    return preview.config.enabled and preview.running


def _view_source(observatory):
    """Whether the viewer should be showing the live preview or a frame.

    While the live view runs it wins, because a capture opens in an
    overlay of its own and freezing the display behind it would be
    backwards. With it off, the newest thing the camera produced wins:
    the last live frame until a capture replaces it.
    """
    preview = observatory.preview
    live = preview.latest if preview is not None else None
    stored = observatory.frames.latest()

    if live is not None and _live(preview):
        # The loop is running, so the display follows it: a capture opens
        # in an overlay of its own and has no business freezing the view.
        return "preview", live
    if live is not None and (stored is None or live.started_at > stored.stored_at):
        # Not streaming - stopped, or standing down for a capture - so
        # whatever the camera produced last.
        return "preview", live
    if stored is not None:
        return "frame", stored
    return None, None


@router.get("/live.mjpg")
async def live(request: Request, observatory: ObservatoryDep, stretch: bool = True) -> StreamingResponse:
    """The live view as a motion-JPEG stream.

    One long response that the browser shows as a moving picture in an
    ordinary `<img>`: each frame is pushed the moment it is rendered, with
    no request per frame and no script in the loop. That round trip - an
    event, then a JSON fetch, then an image fetch - was what held the old
    live view to a frame every few seconds.

    Frames that arrive faster than a client can take them are skipped, not
    queued: a live view should show now, not catch up on the past.
    """
    observatory.require_preview()
    boundary = "astropi-frame"

    async def frames():
        seen, current = -1, None
        while not await request.is_disconnected():
            # Looked up every time rather than once: switching camera
            # builds a new live view, and a stream still waiting on the old
            # one shows its last frame forever.
            preview = observatory.preview
            if preview is None:
                await asyncio.sleep(0.5)
                continue
            if preview is not current:
                seen, current = -1, preview
            if preview.seq <= seen:
                await preview.wait_for_frame(seen, timeout=2.0)
                if preview.seq <= seen:
                    continue
            rendered = await preview.jpeg(stretch=stretch)
            if rendered is None:
                continue
            seen, jpeg = rendered
            yield (
                (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n").encode()
                + jpeg
                + b"\r\n"
            )

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/view")
async def view(observatory: ObservatoryDep) -> dict | None:
    """What the main viewer should be showing.

    One place decides, rather than the client comparing timestamps across
    two sources and getting it subtly wrong mid-capture.
    """
    source, item = _view_source(observatory)
    if source == "preview":
        height, width = item.shape
        return {
            "source": "preview",
            "frame_id": None,
            "width": width,
            "height": height,
            "captured_at": item.started_at,
            "duration_s": item.request.duration_s,
            "streaming": _live(observatory.preview),
            # Changes whenever the stream a browser holds can no longer be
            # the live one - a restart, or a different camera - so the
            # viewer reconnects instead of freezing on the last frame.
            "stream_id": f"{_BOOT}-{id(observatory.preview):x}",
            "fps": None if observatory.preview.fps is None else round(observatory.preview.fps, 1),
            "metadata": {k: v for k, v in item.metadata.items() if not k.startswith("sim_")},
        }
    if source == "frame":
        return {"source": "frame", "frame_id": item.id, **item.summary()}
    return None


@router.get("/view.png")
async def view_image(
    observatory: ObservatoryDep,
    stretch: bool = True,
    max_dimension: int = Query(default=1400, ge=100, le=6000),
) -> Response:
    """The image the viewer should show, from whichever source is current."""
    source, item = _view_source(observatory)
    frame = item if source == "preview" else (item.frame if source == "frame" else None)
    if frame is None:
        raise HTTPException(status_code=404, detail="no frame yet")

    png = to_png(
        frame.data,
        max_dimension=max_dimension,
        stretch=stretch,
        bit_depth=frame.sensor.bit_depth,
    )
    return Response(
        content=png,
        media_type="image/png",
        # A live view is only useful if it is the current one.
        headers={"Cache-Control": "no-store"},
    )
