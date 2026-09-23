"""Guiding control."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import DitherIn, GuidingOut
from astropi.services.guiding import DecGuideMode
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
            # What was actually measured, not just what was derived from
            # it: where the star went for each leg, and whether the two
            # came out the expected way round.
            "west_shift_px": list(calibration.west_shift_px),
            "north_shift_px": list(calibration.north_shift_px),
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


def _refuse_while_busy(observatory, what: str) -> None:
    """Calibration pulses the mount, so it cannot share it with a task.

    Found by running both at once: the centring loop kept measuring an
    error it had not caused, because calibration was pushing the mount
    out from under it, and neither operation could converge. Guiding
    *steadily* during a task is fine and expected - a sequence does it on
    purpose - so this only guards the part that drives the mount itself.
    """
    if observatory.tasks.busy:
        running = observatory.tasks.current
        name = running.name if running is not None else "a task"
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot {what} while {name} is running - it moves the mount, "
                "and so does this"
            ),
        )


@router.post("/calibrate", response_model=GuidingOut)
async def calibrate(observatory: ObservatoryDep) -> GuidingOut:
    _refuse_while_busy(observatory, "calibrate")
    await observatory.require_guider().calibrate()
    return await _snapshot(observatory)


@router.post("/start", response_model=GuidingOut)
async def start(observatory: ObservatoryDep) -> GuidingOut:
    """Start guiding, calibrating first if there is no calibration yet."""
    if observatory.guider is not None and observatory.guider.calibration is None:
        _refuse_while_busy(observatory, "calibrate")
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


class SettingsIn(BaseModel):
    """Guiding settings. Everything is optional; only what is sent changes."""

    # Touched most nights: how bright a guide star you found decides these.
    exposure_s: float | None = Field(default=None, gt=0, le=60)
    gain: int | None = Field(default=None, ge=0, le=1000)
    dec_mode: Literal["auto", "north", "south", "off"] | None = None

    # Touched when guiding misbehaves.
    ra_aggressiveness: float | None = Field(default=None, gt=0, le=2)
    dec_aggressiveness: float | None = Field(default=None, ge=0, le=2)
    min_move_arcsec: float | None = Field(default=None, ge=0, le=10)
    max_pulse_ms: int | None = Field(default=None, gt=0, le=10_000)
    search_radius_px: float | None = Field(default=None, gt=0, le=500)
    edge_margin: float | None = Field(default=None, ge=0, le=0.45)

    # Set once for a mount, and only used by the next calibration.
    calibration_pulse_ms: int | None = Field(default=None, gt=0, le=10_000)
    calibration_steps: int | None = Field(default=None, ge=2, le=20)

    # Only matter when dithering between sub-exposures.
    settle_arcsec: float | None = Field(default=None, gt=0, le=30)
    settle_time_s: float | None = Field(default=None, ge=0, le=300)

    # The idle loop that keeps the guide view live between runs.
    preview_enabled: bool | None = None
    preview_period_s: float | None = Field(default=None, ge=0, le=300)


def _settings_out(config) -> dict:
    return {
        "exposure_s": config.exposure_s,
        "gain": config.gain,
        "dec_mode": str(config.dec_mode),
        "ra_aggressiveness": config.ra_aggressiveness,
        "dec_aggressiveness": config.dec_aggressiveness,
        "min_move_arcsec": config.min_move_arcsec,
        "max_pulse_ms": config.max_pulse_ms,
        "search_radius_px": config.search_radius_px,
        "edge_margin": config.edge_margin,
        "calibration_pulse_ms": config.calibration_pulse_ms,
        "calibration_steps": config.calibration_steps,
        "settle_arcsec": config.settle_arcsec,
        "settle_time_s": config.settle_time_s,
        "preview_enabled": config.preview_enabled,
        "preview_period_s": config.preview_period_s,
    }


@router.get("/settings")
async def get_settings(observatory: ObservatoryDep) -> dict:
    return _settings_out(observatory.require_guider().config)


@router.put("/settings")
async def update_settings(payload: SettingsIn, observatory: ObservatoryDep) -> dict:
    """Change guiding settings, including while the loop is running.

    The loop reads its configuration each cycle, so a new exposure or a
    lower aggressiveness takes effect on the next frame - which is the
    point, since what you are usually trying to fix is the guiding
    happening in front of you.
    """
    guider = observatory.require_guider()
    changes = payload.model_dump(exclude_none=True)
    if "dec_mode" in changes:
        changes["dec_mode"] = DecGuideMode(changes["dec_mode"])
    return _settings_out(guider.update_config(**changes))
