"""Guiding control."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import DitherIn, GuidingOut
from astropi.storage import to_png

router = APIRouter(prefix="/guiding", tags=["guiding"])


async def _snapshot(observatory: ObservatoryDep) -> GuidingOut:
    guider = observatory.require_guider()
    status = await guider.status()
    calibration = status.calibration
    return GuidingOut(
        state=str(status.state),
        calibrated=calibration is not None,
        rms_ra_arcsec=_round(status.rms_ra_arcsec),
        rms_dec_arcsec=_round(status.rms_dec_arcsec),
        rms_total_arcsec=_round(status.rms_total_arcsec),
        samples=status.samples,
        calibration=None
        if calibration is None
        else {
            "ra_rate_arcsec_per_s": round(calibration.ra_rate_arcsec_per_s, 3),
            "dec_rate_arcsec_per_s": round(calibration.dec_rate_arcsec_per_s, 3),
            "angle_deg": round(calibration.angle_deg, 2),
            "pixel_scale_arcsec": round(calibration.pixel_scale_arcsec, 3),
            "calibrated_at": calibration.calibrated_at,
            "dec_at_calibration_deg": round(calibration.dec_at_calibration_deg, 3),
        },
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


@router.get("", response_model=GuidingOut)
async def status(observatory: ObservatoryDep) -> GuidingOut:
    return await _snapshot(observatory)


@router.post("/calibrate", response_model=GuidingOut)
async def calibrate(observatory: ObservatoryDep) -> GuidingOut:
    await observatory.require_guider().calibrate()
    return await _snapshot(observatory)


@router.post("/start", response_model=GuidingOut)
async def start(observatory: ObservatoryDep) -> GuidingOut:
    """Start guiding, calibrating first if there is no calibration yet."""
    await observatory.require_guider().start()
    return await _snapshot(observatory)


@router.post("/stop", response_model=GuidingOut)
async def stop(observatory: ObservatoryDep) -> GuidingOut:
    await observatory.require_guider().stop()
    return await _snapshot(observatory)


@router.post("/dither", response_model=GuidingOut)
async def dither(payload: DitherIn, observatory: ObservatoryDep) -> GuidingOut:
    await observatory.require_guider().dither(
        payload.amount_px, settle_px=payload.settle_px, settle_time_s=payload.settle_time_s
    )
    return await _snapshot(observatory)


@router.post("/calibration/clear")
async def clear_calibration(observatory: ObservatoryDep) -> dict:
    """Drop the calibration - do this after rotating the camera."""
    observatory.require_guider().invalidate_calibration()
    return {"calibrated": False}


class LockIn(BaseModel):
    """A point on the guide sensor, in unbinned pixels."""

    x: float = Field(ge=0)
    y: float = Field(ge=0)
    radius_px: float = Field(default=40.0, gt=0, le=400)


@router.get("/frame")
async def frame_info(observatory: ObservatoryDep) -> dict | None:
    """Geometry for the guide view's overlays.

    Kept separate from the image so the overlay can update without
    refetching the frame, and so a client that only wants to know where the
    lock is does not download a picture to find out.
    """
    info = observatory.require_guider().frame_info()
    if info is None:
        return None
    return {
        "width": info.width,
        "height": info.height,
        "captured_at": info.captured_at,
        "lock": None if info.lock is None else {"x": info.lock[0], "y": info.lock[1]},
        "star": None if info.star is None else {"x": info.star[0], "y": info.star[1]},
        "search_radius_px": info.search_radius_px,
        "candidates": [{"x": x, "y": y, "snr": round(snr, 1)} for x, y, snr in info.candidates],
    }


@router.get("/frame.png")
async def frame_image(
    observatory: ObservatoryDep,
    max_dimension: int = Query(default=512, ge=100, le=4000),
) -> Response:
    """The latest guide frame, stretched for display.

    Downsampled hard by default. The view is a few hundred pixels on screen
    and a new frame arrives every couple of seconds, so sending the sensor's
    full resolution would put close to a megabyte per frame on a LAN that
    also has to carry the imaging data.
    """
    guider = observatory.require_guider()
    frame = guider.latest_frame
    if frame is None:
        raise HTTPException(
            status_code=404,
            detail="no guide frame yet - start guiding, or take a preview exposure",
        )
    png = to_png(frame.data, max_dimension=max_dimension, bit_depth=frame.sensor.bit_depth)
    return Response(
        content=png,
        media_type="image/png",
        # Never cached: the whole point is that it is the newest one.
        headers={"Cache-Control": "no-store"},
    )


@router.post("/preview")
async def preview(observatory: ObservatoryDep) -> dict:
    """Take one guide exposure without starting the loop.

    Needed before guiding begins: that is when a star has to be picked, and
    when it is worth checking the guide camera is focused at all.
    """
    info = await observatory.require_guider().preview()
    return {"width": info.width, "height": info.height, "stars": len(info.candidates)}


@router.post("/lock")
async def lock(payload: LockIn, observatory: ObservatoryDep) -> dict:
    """Lock onto the detected star nearest a point on the sensor."""
    star = observatory.require_guider().select_star(payload.x, payload.y, radius_px=payload.radius_px)
    return {
        "x": round(star.x, 2),
        "y": round(star.y, 2),
        "snr": round(star.snr, 1),
        "hfd": round(star.hfd, 2),
    }
