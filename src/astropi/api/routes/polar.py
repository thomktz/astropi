"""Two-frame polar alignment: take a frame, move however the window allows,
take another, then watch the arrows while turning the knobs.

Plain requests rather than a task. Every step is one exposure and one
solve, and between steps the operator is deciding something - whether
the frame is good, where to move next - so there is no long-running loop
for the task engine to own. The live view is the client asking again.

The mount is never moved from here. The nudges between frames are the
ordinary `/mount/nudge`, pressed by the operator.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.core.errors import AstropiError, SolveFailedError
from astropi.core.geometry import RaDec, normalize_deg, wrap_symmetric_deg
from astropi.core.timekeeping import local_sidereal_time_deg
from astropi.devices.camera import ExposureRequest, FrameKind
from astropi.runtime import Observatory
from astropi.services.platesolve import SolveHint, SolveResult
from astropi.services.twopointpolar import (
    PolarFrame,
    TwoFrameFit,
    correction,
    fit_two_frames,
    live_error,
)

router = APIRouter(prefix="/polar", tags=["polar"])


class ShotIn(BaseModel):
    exposure_s: float = Field(default=2.0, gt=0, le=60)
    gain: int | None = Field(default=None, ge=0, le=1000)
    binning: int = Field(default=2, ge=1, le=4)


class AcceptIn(BaseModel):
    shot_id: str


@dataclass
class _Shot:
    id: str
    frame_id: str
    width: int
    height: int
    solve: SolveResult | None
    solve_error: str | None
    frame: PolarFrame | None
    taken_at: float


@dataclass
class _Session:
    """One alignment in progress, kept between requests."""

    frames: list[PolarFrame] = field(default_factory=list)
    shots: dict[str, _Shot] = field(default_factory=dict)
    fit: TwoFrameFit | None = None
    knobs: tuple[float, float] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset(self) -> None:
        self.frames.clear()
        self.shots.clear()
        self.fit = None
        self.knobs = None


# Held beside the observatory rather than on it: the session is this
# screen's working state, and nothing else in the application reads it.
_sessions: weakref.WeakKeyDictionary[Observatory, _Session] = weakref.WeakKeyDictionary()


def _session(observatory: Observatory) -> _Session:
    session = _sessions.get(observatory)
    if session is None:
        session = _sessions[observatory] = _Session()
    return session


def _state(session: _Session) -> dict:
    fit = session.fit
    return {
        "step": "live" if fit else ("second" if session.frames else "first"),
        "frames": [
            {"ra_deg": f.solved.ra_deg, "dec_deg": f.solved.dec_deg, "mount_ra_deg": f.mount.ra_deg}
            for f in session.frames
        ],
        "fit": None
        if fit is None
        else {
            "altitude_error_arcmin": round(fit.error.altitude_error_arcmin, 2),
            "azimuth_error_arcmin": round(fit.error.azimuth_error_arcmin, 2),
            "total_error_arcmin": round(fit.error.total_error_arcmin, 2),
            "uncertainty_arcmin": round(fit.uncertainty_arcmin, 2),
            "rotation_deg": round(fit.rotation_deg, 2),
            "warnings": fit.warnings,
        },
    }


@router.get("")
async def state(observatory: ObservatoryDep) -> dict:
    return _state(_session(observatory))


@router.delete("")
async def reset(observatory: ObservatoryDep) -> dict:
    session = _session(observatory)
    session.reset()
    return _state(session)


async def _shoot(observatory: Observatory, session: _Session, payload: ShotIn) -> _Shot:
    """Expose, solve, and note where the mount's axes stood."""
    if observatory.tasks.busy:
        raise HTTPException(status_code=409, detail="another operation is using the camera")
    camera = observatory.camera()
    mount = observatory.mount()
    request = ExposureRequest(
        duration_s=payload.exposure_s,
        gain=payload.gain,
        binning=payload.binning,
        kind=FrameKind.PREVIEW,
    )
    frame = await camera.expose(request)
    # Read straight after the shutter closes: the mount reading and the
    # sidereal time have to describe the same instant as the photons.
    status = await mount.status()
    taken_at = time.time()
    frame_id = observatory.frames.add(frame)

    hint_center = status.position
    if session.frames:
        # The mount has probably never been synced, so its own idea of
        # where it points can be degrees out. The first solve measured by
        # how much; carry that over and the search stays local.
        first = session.frames[0]
        ra_offset = wrap_symmetric_deg(first.solved.ra_deg - first.mount.ra_deg)
        dec_offset = first.solved.dec_deg - first.mount.dec_deg
        hint_center = RaDec(
            normalize_deg(status.position.ra_deg + ra_offset),
            max(-90.0, min(90.0, status.position.dec_deg + dec_offset)),
        )
    hint = SolveHint(
        center=hint_center,
        radius_deg=10.0 if session.frames else 30.0,
        pixel_scale_arcsec=frame.metadata.get("pixel_scale_arcsec"),
    )

    solve: SolveResult | None = None
    error: str | None = None
    polar_frame: PolarFrame | None = None
    try:
        solve = await observatory.plate_solver.solve(frame, hint)
    except SolveFailedError as failure:
        error = str(failure)
    else:
        polar_frame = PolarFrame(
            solved=solve.center,
            mount=status.position,
            lst_deg=local_sidereal_time_deg(observatory.site.longitude_deg, taken_at),
            pier_side=str(status.pier_side),
        )

    height, width = frame.shape
    shot = _Shot(
        id=uuid.uuid4().hex[:12],
        frame_id=frame_id,
        width=width,
        height=height,
        solve=solve,
        solve_error=error,
        frame=polar_frame,
        taken_at=taken_at,
    )
    # Only the latest few are worth accepting; older ones are stale views.
    session.shots = {key: value for key, value in list(session.shots.items())[-3:]}
    session.shots[shot.id] = shot
    return shot


def _shot_out(shot: _Shot, session: _Session) -> dict:
    out: dict = {
        "id": shot.id,
        "frame_id": shot.frame_id,
        "width": shot.width,
        "height": shot.height,
        "solve_error": shot.solve_error,
        "solved": None,
        "moved_deg": None,
    }
    if shot.solve is not None:
        out["solved"] = {
            "ra_deg": shot.solve.center.ra_deg,
            "dec_deg": shot.solve.center.dec_deg,
            "pixel_scale_arcsec": round(shot.solve.pixel_scale_arcsec, 3),
            "rotation_deg": round(shot.solve.field_rotation_deg, 1),
            "stars": shot.solve.stars_detected,
            "solver": shot.solve.solver,
            "solve_time_s": round(shot.solve.solve_time_s, 2),
        }
    if shot.frame is not None and session.frames and session.fit is None:
        # How far the RA axis has turned since the first frame, so the
        # second step can say whether the move is long enough yet.
        out["moved_deg"] = round(
            wrap_symmetric_deg(shot.frame.mechanical[0] - session.frames[0].mechanical[0]), 2
        )
    return out


@router.post("/capture")
async def capture(payload: ShotIn, observatory: ObservatoryDep) -> dict:
    """Take a frame for the next step, to be looked at before accepting."""
    session = _session(observatory)
    async with session.lock:
        shot = await _shoot(observatory, session, payload)
    return _shot_out(shot, session)


@router.post("/accept")
async def accept(payload: AcceptIn, observatory: ObservatoryDep) -> dict:
    """Keep a captured frame as the next measurement; fit after the second."""
    session = _session(observatory)
    shot = session.shots.get(payload.shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="that frame is no longer available - take another")
    if shot.frame is None:
        raise HTTPException(status_code=422, detail="that frame did not solve, so it cannot be used")
    if session.fit is not None:
        raise HTTPException(status_code=409, detail="already measured - start over to measure again")

    if not session.frames:
        session.frames.append(shot.frame)
        return _state(session)

    try:
        fit = fit_two_frames(session.frames[0], shot.frame, observatory.site)
    except AstropiError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    session.frames.append(shot.frame)
    session.fit = fit
    session.knobs = (fit.altitude_error_deg, fit.azimuth_error_deg)
    return _state(session)


@router.post("/live")
async def live(payload: ShotIn, observatory: ObservatoryDep) -> dict:
    """One more frame against the fit, with the arrows to draw on it."""
    session = _session(observatory)
    if session.fit is None:
        raise HTTPException(status_code=409, detail="take both frames first")
    async with session.lock:
        shot = await _shoot(observatory, session, payload)
    out = _shot_out(shot, session)
    if shot.frame is None or shot.solve is None:
        return out

    site = observatory.site
    error, session.knobs = live_error(session.fit, shot.frame, site, previous=session.knobs)
    plan = correction(session.fit, shot.frame, session.knobs, site)
    out["error"] = {
        "altitude_error_arcmin": round(error.altitude_error_arcmin, 2),
        "azimuth_error_arcmin": round(error.azimuth_error_arcmin, 2),
        "total_error_arcmin": round(error.total_error_arcmin, 2),
        "instructions": error.instructions(site.hemisphere),
    }
    out["overlay"] = _overlay(shot, plan)
    return out


def _overlay(shot: _Shot, plan) -> dict | None:
    """The correction as points on this frame, in fractions of its size.

    Drawn from the frame centre, which is what the solve measured, so a
    small model residual moves the arrow's tip rather than its tail.
    """
    assert shot.solve is not None
    now = shot.solve.pixel_of(plan.now)
    middle = shot.solve.pixel_of(plan.after_altitude)
    end = shot.solve.pixel_of(plan.aligned)
    if now is None or middle is None or end is None:
        return None
    cx, cy = shot.width / 2.0, shot.height / 2.0

    def point(p: tuple[float, float]) -> list[float]:
        return [round((p[0] - now[0] + cx) / shot.width, 5), round((p[1] - now[1] + cy) / shot.height, 5)]

    return {"start": [0.5, 0.5], "after_altitude": point(middle), "aligned": point(end)}
