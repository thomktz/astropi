"""Camera control and frame previews."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import CameraOut, ControlOut, CoolingIn, ExposureIn
from astropi.devices import CameraDevice, DeviceRole
from astropi.devices.camera import ControlSpec, ExposureRequest, FrameKind
from astropi.storage import to_png

router = APIRouter(prefix="/camera", tags=["camera"])


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
    period_s: float | None = Field(default=None, ge=0, le=600)


def _preview_out(config) -> dict:
    return {
        "enabled": config.enabled,
        "exposure_s": config.exposure_s,
        "gain": config.gain,
        "binning": config.binning,
        "period_s": config.period_s,
    }


@router.get("/preview")
async def get_preview(observatory: ObservatoryDep) -> dict:
    preview = observatory.require_preview()
    return _preview_out(preview.config) | {"running": preview.running}


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
    return _preview_out(preview.config) | {"running": preview.running}


@router.get("/view")
async def view(observatory: ObservatoryDep) -> dict | None:
    """What the main viewer should be showing.

    One place decides, rather than the client comparing timestamps across
    two sources: whichever of the live preview and the last stored frame
    is newer. During a capture run the preview stands down, so this
    naturally follows the light frames as they arrive.
    """
    stored = observatory.frames.latest()
    preview = observatory.preview.latest if observatory.preview else None

    use_preview = preview is not None and (
        stored is None or preview.started_at > stored.stored_at
    )
    if use_preview and preview is not None:
        height, width = preview.shape
        return {
            "source": "preview",
            "frame_id": None,
            "width": width,
            "height": height,
            "captured_at": preview.started_at,
            "duration_s": preview.request.duration_s,
            "metadata": {
                k: v for k, v in preview.metadata.items() if not k.startswith("sim_")
            },
        }
    if stored is None:
        return None
    return {"source": "frame", "frame_id": stored.id, **stored.summary()}


@router.get("/view.png")
async def view_image(
    observatory: ObservatoryDep,
    stretch: bool = True,
    max_dimension: int = Query(default=1400, ge=100, le=6000),
) -> Response:
    """The image the viewer should show, from whichever source is newer."""
    stored = observatory.frames.latest()
    preview = observatory.preview.latest if observatory.preview else None

    use_preview = preview is not None and (
        stored is None or preview.started_at > stored.stored_at
    )
    frame = preview if use_preview else (stored.frame if stored else None)
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
