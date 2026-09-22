"""Direct mount control.

Raw moves, for when the operator wants the mount to do exactly one thing.
Anything with a feedback loop - GoTo with centring, polar alignment - is a
task instead, under /api/tasks.
"""

from __future__ import annotations

from fastapi import APIRouter

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import (
    CoordinateIn,
    CoordinateOut,
    MountOut,
    NudgeIn,
    PulseGuideIn,
    TrackingIn,
)
from astropi.core.timekeeping import hour_angle_deg
from astropi.devices.mount import GuideDirection, TrackingRate

router = APIRouter(prefix="/mount", tags=["mount"])


async def _snapshot(observatory: ObservatoryDep) -> MountOut:
    status = await observatory.mount().status()
    return MountOut(
        state=str(status.state),
        tracking=status.tracking,
        tracking_rate=str(status.tracking_rate),
        pier_side=str(status.pier_side),
        slewing=status.slewing,
        coord=CoordinateOut.of(status.position),
        altitude_deg=round(status.horizontal.alt_deg, 3) if status.horizontal else None,
        azimuth_deg=round(status.horizontal.az_deg, 3) if status.horizontal else None,
        target=CoordinateOut.of(status.target) if status.target else None,
        hour_angle_deg=round(
            hour_angle_deg(status.position.ra_deg, observatory.site.longitude_deg), 4
        ),
    )


@router.get("", response_model=MountOut)
async def status(observatory: ObservatoryDep) -> MountOut:
    return await _snapshot(observatory)


@router.post("/slew", response_model=MountOut)
async def slew(payload: CoordinateIn, observatory: ObservatoryDep) -> MountOut:
    """Slew without plate-solve correction. Returns as soon as it starts."""
    await observatory.mount().slew_to(payload.to_radec())
    return await _snapshot(observatory)


@router.post("/sync", response_model=MountOut)
async def sync(payload: CoordinateIn, observatory: ObservatoryDep) -> MountOut:
    await observatory.mount().sync_to(payload.to_radec())
    return await _snapshot(observatory)


@router.post("/abort", response_model=MountOut)
async def abort(observatory: ObservatoryDep) -> MountOut:
    await observatory.mount().abort_slew()
    return await _snapshot(observatory)


@router.post("/park", response_model=MountOut)
async def park(observatory: ObservatoryDep) -> MountOut:
    await observatory.mount().park()
    # Parked at the pole, the rig is not on a target any more.
    observatory.set_active_target(None)
    return await _snapshot(observatory)


@router.post("/unpark", response_model=MountOut)
async def unpark(observatory: ObservatoryDep) -> MountOut:
    await observatory.mount().unpark()
    return await _snapshot(observatory)


@router.post("/tracking", response_model=MountOut)
async def tracking(payload: TrackingIn, observatory: ObservatoryDep) -> MountOut:
    await observatory.mount().set_tracking(payload.enabled, TrackingRate(payload.rate))
    return await _snapshot(observatory)


@router.post("/pulse")
async def pulse(payload: PulseGuideIn, observatory: ObservatoryDep) -> dict:
    """One guide-rate correction. The loop's primitive, not the operator's."""
    await observatory.mount().pulse_guide(GuideDirection(payload.direction), payload.duration_ms)
    return {"direction": payload.direction, "duration_ms": payload.duration_ms}


@router.post("/nudge", response_model=MountOut)
async def nudge(payload: NudgeIn, observatory: ObservatoryDep) -> MountOut:
    """Move by a fixed angle, for framing by hand.

    Returns when the move has finished, so the panel that greys its
    keypad out for the duration is telling the truth about it.
    """
    await observatory.mount().move_by(GuideDirection(payload.direction), payload.degrees)
    return await _snapshot(observatory)
