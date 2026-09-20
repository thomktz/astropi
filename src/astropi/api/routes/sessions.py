"""Session plans: build one, see whether it will work, run it."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import TaskOut
from astropi.core.geometry import RaDec
from astropi.sequencing.tasks import SessionRunTask
from astropi.services.planning import PlanBlock, SessionPlan

router = APIRouter(prefix="/sessions", tags=["sessions"])


class BlockIn(BaseModel):
    """One target and what to shoot on it."""

    target_id: str | None = None
    target_name: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    frames: int = Field(ge=1, le=10_000)
    exposure_s: float = Field(gt=0, le=3600)
    gain: int | None = None
    offset: int | None = None
    binning: int = Field(default=1, ge=1, le=8)
    dither_every: int = Field(default=3, ge=0, le=100)
    center: bool = True
    autofocus: bool = False
    id: str | None = None


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    blocks: list[BlockIn] = Field(default_factory=list)


def _to_block(payload: BlockIn, observatory: ObservatoryDep) -> PlanBlock:
    """Resolve a block, looking a catalogue target up by id if given."""
    if payload.target_id:
        target = observatory.catalog.get(payload.target_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"no target {payload.target_id!r}")
        coord, name = target.coord, target.display_name
    elif payload.ra_deg is not None and payload.dec_deg is not None:
        coord = RaDec(payload.ra_deg, payload.dec_deg)
        name = payload.target_name or str(coord)
    else:
        raise HTTPException(
            status_code=400, detail="each block needs either a target_id or ra_deg and dec_deg"
        )

    block = PlanBlock(
        target_id=payload.target_id,
        target_name=payload.target_name or name,
        coord=coord,
        frames=payload.frames,
        exposure_s=payload.exposure_s,
        gain=payload.gain,
        offset=payload.offset,
        binning=payload.binning,
        dither_every=payload.dither_every,
        center=payload.center,
        autofocus=payload.autofocus,
    )
    if payload.id:
        block.id = payload.id
    return block


def _plan_out(observatory: ObservatoryDep, plan: SessionPlan) -> dict:
    """A plan with its schedule, which is what the editor actually renders."""
    schedule = observatory.planner.schedule(plan)
    return {
        "id": plan.id,
        "name": plan.name,
        "created_at": plan.created_at,
        "duration_s": round(plan.duration_s, 1),
        "integration_s": round(plan.integration_s, 1),
        "starts_at": schedule.starts_at.isoformat(),
        "ends_at": schedule.ends_at.isoformat(),
        "issues": [{"severity": str(i.severity), "message": i.message} for i in schedule.issues],
        "blocks": [
            {
                "id": entry.block.id,
                "target_id": entry.block.target_id,
                "target_name": entry.block.target_name,
                "ra_deg": entry.block.coord.ra_deg,
                "dec_deg": entry.block.coord.dec_deg,
                "frames": entry.block.frames,
                "exposure_s": entry.block.exposure_s,
                "gain": entry.block.gain,
                "offset": entry.block.offset,
                "binning": entry.block.binning,
                "dither_every": entry.block.dither_every,
                "center": entry.block.center,
                "autofocus": entry.block.autofocus,
                "duration_s": round(entry.block.duration_s, 1),
                "integration_s": round(entry.block.integration_s, 1),
                "starts_at": entry.starts_at.isoformat(),
                "ends_at": entry.ends_at.isoformat(),
                "min_altitude_deg": round(entry.min_altitude_deg, 1),
                "max_altitude_deg": round(entry.max_altitude_deg, 1),
                "crosses_meridian": entry.crosses_meridian,
                "issues": [
                    {"severity": str(i.severity), "message": i.message} for i in entry.issues
                ],
            }
            for entry in schedule.blocks
        ],
    }


@router.get("")
async def list_plans(observatory: ObservatoryDep) -> list[dict]:
    return [_plan_out(observatory, plan) for plan in observatory.sessions.list()]


@router.post("")
async def create_plan(payload: PlanIn, observatory: ObservatoryDep) -> dict:
    plan = SessionPlan(
        name=payload.name, blocks=[_to_block(block, observatory) for block in payload.blocks]
    )
    observatory.sessions.save(plan)
    return _plan_out(observatory, plan)


@router.post("/preview")
async def preview_plan(payload: PlanIn, observatory: ObservatoryDep) -> dict:
    """Schedule a plan without saving it.

    So the editor can show durations and warnings as blocks are added,
    before anything is committed to disk.
    """
    plan = SessionPlan(
        name=payload.name, blocks=[_to_block(block, observatory) for block in payload.blocks]
    )
    return _plan_out(observatory, plan)


@router.get("/{plan_id}")
async def get_plan(plan_id: str, observatory: ObservatoryDep) -> dict:
    plan = observatory.sessions.load(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}")
    return _plan_out(observatory, plan)


@router.put("/{plan_id}")
async def update_plan(plan_id: str, payload: PlanIn, observatory: ObservatoryDep) -> dict:
    existing = observatory.sessions.load(plan_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}")

    existing.name = payload.name
    existing.blocks = [_to_block(block, observatory) for block in payload.blocks]
    observatory.sessions.save(existing)
    return _plan_out(observatory, existing)


@router.delete("/{plan_id}")
async def delete_plan(plan_id: str, observatory: ObservatoryDep) -> dict:
    if not observatory.sessions.delete(plan_id):
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}")
    return {"deleted": True, "id": plan_id}


@router.post("/{plan_id}/run", response_model=TaskOut)
async def run_plan(plan_id: str, observatory: ObservatoryDep) -> TaskOut:
    """Run a plan. Returns at once; progress arrives over the socket."""
    plan = observatory.sessions.load(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}")
    if not plan.blocks:
        raise HTTPException(status_code=400, detail="the plan has no blocks")

    task = SessionRunTask(observatory, plan)
    try:
        observatory.tasks.submit(task)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return TaskOut(**task.as_dict())
