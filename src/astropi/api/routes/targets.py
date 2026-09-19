"""Catalogue search and target visibility."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import CoordinateIn, CoordinateOut, TargetOut, VisibilityOut
from astropi.services.catalog import Target, TargetSource

router = APIRouter(prefix="/targets", tags=["targets"])


def _out(target: Target, altitude: float | None = None) -> TargetOut:
    return TargetOut(
        id=target.id,
        name=target.name,
        display_name=target.display_name,
        object_type=target.object_type,
        source=str(target.source),
        magnitude=target.magnitude,
        constellation=target.constellation,
        common_names=list(target.common_names),
        coord=CoordinateOut.of(target.coord),
        altitude_deg=None if altitude is None else round(altitude, 2),
    )


@router.get("/search", response_model=list[TargetOut])
async def search(
    observatory: ObservatoryDep,
    q: str = "",
    limit: int = Query(default=30, ge=1, le=200),
    min_altitude_deg: float | None = None,
) -> list[TargetOut]:
    """Search the catalogue, each result carrying its current altitude."""
    results = observatory.catalog.search(q, limit=limit, min_altitude_deg=min_altitude_deg)
    return [_out(target, altitude) for target, altitude in results]


@router.get("/recommended", response_model=list[TargetOut])
async def recommended(
    observatory: ObservatoryDep,
    limit: int = Query(default=20, ge=1, le=100),
    min_altitude_deg: float = 30.0,
    max_magnitude: float = 10.0,
) -> list[TargetOut]:
    """What is highest and brightest right now."""
    results = observatory.catalog.recommended(
        limit=limit, min_altitude_deg=min_altitude_deg, max_magnitude=max_magnitude
    )
    return [_out(target, altitude) for target, altitude in results]


@router.get("/{target_id}", response_model=TargetOut)
async def get_target(target_id: str, observatory: ObservatoryDep) -> TargetOut:
    target = observatory.catalog.get(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no target {target_id!r}")
    altitude, _ = observatory.ephemeris.altaz_now(target.coord)
    return _out(target, altitude)


@router.get("/{target_id}/visibility", response_model=VisibilityOut)
async def visibility(target_id: str, observatory: ObservatoryDep) -> VisibilityOut:
    """Tonight's altitude curve and the events derived from it."""
    target = observatory.catalog.get(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no target {target_id!r}")
    return _visibility_out(observatory, target.coord)


@router.post("/visibility", response_model=VisibilityOut)
async def visibility_for_coord(payload: CoordinateIn, observatory: ObservatoryDep) -> VisibilityOut:
    """Same, for an arbitrary coordinate rather than a catalogue entry."""
    return _visibility_out(observatory, payload.to_radec())


def _visibility_out(observatory: ObservatoryDep, coord) -> VisibilityOut:
    result = observatory.ephemeris.visibility(coord)
    return VisibilityOut(
        altitude_now_deg=round(result.altitude_now_deg, 2),
        azimuth_now_deg=round(result.azimuth_now_deg, 2),
        max_altitude_deg=round(result.max_altitude_deg, 2),
        transit_at=result.transit_at,
        rises_at=result.rises_at,
        sets_at=result.sets_at,
        circumpolar=result.circumpolar,
        never_rises=result.never_rises,
        hours_above_horizon=round(result.hours_above_horizon, 2),
        moon_separation_deg=round(result.moon_separation_deg, 1),
        curve=[
            {"at": s.when.isoformat(), "alt": round(s.altitude_deg, 2), "az": round(s.azimuth_deg, 1)}
            for s in result.samples
        ],
    )


@router.get("", response_model=list[TargetOut])
async def list_targets(
    observatory: ObservatoryDep,
    source: str | None = None,
    limit: int = Query(default=100, ge=1, le=2000),
) -> list[TargetOut]:
    targets = observatory.catalog.all()
    if source:
        try:
            wanted = TargetSource(source)
        except ValueError as error:
            valid = ", ".join(str(s) for s in TargetSource)
            raise HTTPException(
                status_code=400, detail=f"unknown source; expected one of {valid}"
            ) from error
        targets = [t for t in targets if t.source is wanted]
    return [_out(target) for target in targets[:limit]]
