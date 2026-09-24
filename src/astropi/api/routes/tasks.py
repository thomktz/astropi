"""Long-running operations.

Every endpoint here returns immediately with a task record; progress arrives
over the WebSocket. None of these could complete inside an HTTP request -
centring takes a minute, an imaging run takes hours.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import (
    AutofocusIn,
    CaptureIn,
    GotoIn,
    GuidingAssistantIn,
    PolarAlignIn,
    TaskOut,
)
from astropi.devices.camera import FrameKind
from astropi.sequencing.tasks import (
    AutofocusTask,
    CapturePlan,
    CaptureSequenceTask,
    GotoAndCenterTask,
    GuidingAssistantTask,
    PolarAlignTask,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _submit(observatory: ObservatoryDep, task) -> TaskOut:
    try:
        observatory.tasks.submit(task)
    except RuntimeError as error:
        # 409: the rig is busy, not a malformed request. The client should
        # offer to cancel the running task rather than retry blindly.
        raise HTTPException(status_code=409, detail=str(error)) from error
    return TaskOut(**task.as_dict())


@router.get("", response_model=list[TaskOut])
async def list_tasks(observatory: ObservatoryDep, limit: int = 20) -> list[TaskOut]:
    return [TaskOut(**task.as_dict()) for task in observatory.tasks.recent(limit)]


@router.get("/current", response_model=TaskOut | None)
async def current(observatory: ObservatoryDep) -> TaskOut | None:
    task = observatory.tasks.current
    return None if task is None else TaskOut(**task.as_dict())


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, observatory: ObservatoryDep) -> TaskOut:
    task = observatory.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
    return TaskOut(**task.as_dict())


@router.delete("/{task_id}")
async def cancel(task_id: str, observatory: ObservatoryDep) -> dict:
    cancelled = await observatory.tasks.cancel(task_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail=f"task {task_id!r} is not running")
    return {"cancelled": True, "id": task_id}


@router.post("/goto", response_model=TaskOut)
async def goto(payload: GotoIn, observatory: ObservatoryDep) -> TaskOut:
    """Slew to a target and centre it by plate solving."""
    catalog_target = None
    if payload.target_id:
        catalog_target = observatory.catalog.get(payload.target_id)
        if catalog_target is None:
            raise HTTPException(status_code=404, detail=f"no target {payload.target_id!r}")
        coord, name = catalog_target.coord, f"GoTo {catalog_target.display_name}"
    elif payload.coord:
        coord, name = payload.coord.to_radec(), "GoTo coordinates"
    else:
        raise HTTPException(status_code=400, detail="provide either target_id or coord")

    task = GotoAndCenterTask(
        observatory,
        coord,
        name=name,
        catalog_target=catalog_target,
        tolerance_arcmin=payload.tolerance_arcmin,
        # A single iteration is a plain slew with one confirming solve.
        max_iterations=payload.max_iterations if payload.center else 1,
        exposure_s=payload.exposure_s,
    )
    return _submit(observatory, task)


@router.post("/polar-align", response_model=TaskOut)
async def polar_align(payload: PolarAlignIn, observatory: ObservatoryDep) -> TaskOut:
    task = PolarAlignTask(
        observatory,
        points=payload.points,
        separation_deg=payload.separation_deg,
        exposure_s=payload.exposure_s,
    )
    return _submit(observatory, task)


@router.post("/polar-align/refine")
async def polar_refine(observatory: ObservatoryDep, exposure_s: float = 4.0) -> dict:
    """One solve against the existing fit, for live feedback at the knobs.

    Deliberately synchronous and outside the task engine: it is a single
    short exposure, and the operator is turning a knob waiting for the
    number to move.
    """
    from astropi.devices.camera import ExposureRequest
    from astropi.services.platesolve import SolveHint

    camera = observatory.camera()
    frame = await camera.expose(ExposureRequest(duration_s=exposure_s, kind=FrameKind.PREVIEW))
    status = await observatory.mount().status()
    solved = await observatory.plate_solver.solve(frame, SolveHint(center=status.position))

    error = observatory.polar_alignment.refine(solved.center, tracking=status.tracking)
    return {
        "altitude_error_arcmin": round(error.altitude_error_arcmin, 2),
        "azimuth_error_arcmin": round(error.azimuth_error_arcmin, 2),
        "total_error_arcmin": round(error.total_error_arcmin, 2),
        "instructions": error.instructions(observatory.site.hemisphere),
    }


@router.post("/autofocus", response_model=TaskOut)
async def autofocus(payload: AutofocusIn, observatory: ObservatoryDep) -> TaskOut:
    task = AutofocusTask(
        observatory,
        steps=payload.steps,
        step_size=payload.step_size,
        exposure_s=payload.exposure_s,
    )
    return _submit(observatory, task)


@router.post("/guide-assistant", response_model=TaskOut)
async def guide_assistant(payload: GuidingAssistantIn, observatory: ObservatoryDep) -> TaskOut:
    """Measure what the mount and the sky do when nothing is correcting them.

    Stops nothing and starts nothing by itself: it refuses if guiding is
    running, because what it measures is the uncorrected mount.
    """
    task = GuidingAssistantTask(
        observatory,
        seconds=payload.seconds,
        measure_backlash=payload.measure_backlash,
    )
    return _submit(observatory, task)


@router.post("/capture", response_model=TaskOut)
async def capture(payload: CaptureIn, observatory: ObservatoryDep) -> TaskOut:
    plan = CapturePlan(
        count=payload.count,
        exposure_s=payload.exposure_s,
        gain=payload.gain,
        offset=payload.offset,
        binning=payload.binning,
        dither_every=payload.dither_every,
        dither_px=payload.dither_px,
        name=payload.name,
    )
    return _submit(observatory, CaptureSequenceTask(observatory, plan))
