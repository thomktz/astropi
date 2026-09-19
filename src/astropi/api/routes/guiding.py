"""Guiding control."""

from __future__ import annotations

from fastapi import APIRouter

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import DitherIn, GuidingOut

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
