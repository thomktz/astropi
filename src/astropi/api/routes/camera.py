"""Camera control and frame previews."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import CameraOut, CoolingIn, ExposureIn
from astropi.devices import CameraDevice, DeviceRole
from astropi.devices.camera import ExposureRequest, FrameKind
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
    frame = await camera.expose(
        ExposureRequest(
            duration_s=payload.duration_s,
            gain=payload.gain,
            offset=payload.offset,
            binning=payload.binning,
            kind=FrameKind(payload.kind),
        )
    )
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
