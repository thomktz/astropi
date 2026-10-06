"""Imaging sessions - one target, four groups started by hand - and the dark library."""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import TaskOut
from astropi.core.errors import AstropiError
from astropi.devices import CameraDevice, DeviceRole
from astropi.sequencing.tasks import SessionGroupTask
from astropi.storage.imaging import group_filter, new_group, new_session, relink
from astropi.storage.naming import FILTER_TOKENS, night_of, session_folder

router = APIRouter(prefix="/imaging", tags=["imaging"])
library_router = APIRouter(prefix="/dark-library", tags=["imaging"])

Group = Literal["light", "flat", "darkflat", "dark"]


class SessionIn(BaseModel):
    target_id: str | None = None
    target_name: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None


class GroupPatch(BaseModel):
    count: int | None = Field(default=None, ge=1, le=10_000)
    exposure_s: float | None = Field(default=None, gt=0, le=3600)
    gain: int | None = None
    offset: int | None = None
    temp_c: float | None = Field(default=None, ge=-40, le=40)
    binning: int | None = Field(default=None, ge=1, le=4)
    dither_every: int | None = Field(default=None, ge=0, le=100)
    linked: bool | None = None
    auto_exposure: bool | None = None
    #: Lights, flats and dark flats: a filter of their own; null for the session's.
    filter: str | None = Field(default=None, max_length=60)


class SessionPatch(BaseModel):
    target_name: str | None = Field(default=None, min_length=1, max_length=120)
    filter: str | None = Field(default=None, min_length=1, max_length=60)
    #: Per group, only the fields sent are changed; a field sent as null is cleared.
    groups: dict[Group, GroupPatch] = Field(default_factory=dict)


class StartIn(BaseModel):
    into_library: bool = False


class LibraryShootIn(BaseModel):
    count: int = Field(default=30, ge=1, le=1000)
    exposure_s: float = Field(gt=0, le=3600)
    gain: int | None = None
    offset: int | None = None
    temp_c: float = Field(ge=-40, le=40)
    binning: int = Field(default=1, ge=1, le=4)


def _out(observatory: ObservatoryDep, session: dict[str, Any]) -> dict[str, Any]:
    folder = observatory.archive.root / session_folder(session["night"], session["target_name"])
    dark = session["groups"]["dark"]
    library = (
        observatory.dark_library.match(dark["exposure_s"], dark["gain"], dark["offset"], dark["temp_c"])
        if dark["exposure_s"]
        else None
    )
    filters = {kind: group_filter(session, kind) for kind in ("light", "flat", "darkflat")}
    return {**session, "folder": str(folder), "dark_library_match": library, "filters": filters}


def _remember_filter(observatory: ObservatoryDep, name: str) -> None:
    """Keep a filter in the list offered next time, and as the default."""
    known = _filters(observatory)
    if name not in known:
        known.append(name)
    observatory.state.put("filters", {"known": known, "last": name})


def _filters(observatory: ObservatoryDep) -> list[str]:
    return list(observatory.state.get("filters").get("known") or FILTER_TOKENS)


def _load(observatory: ObservatoryDep, session_id: str) -> dict[str, Any]:
    session = observatory.imaging.load(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
    return session


def _submit(observatory: ObservatoryDep, task) -> TaskOut:
    try:
        observatory.tasks.submit(task)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return TaskOut(**task.as_dict())


async def _light_defaults(observatory: ObservatoryDep) -> dict[str, Any]:
    """Start a new session's lights from the last ones shot, at the cooler's set point."""
    last = observatory.state.get("last_light") or {}
    light = {k: last[k] for k in ("exposure_s", "gain", "offset", "binning") if last.get(k) is not None}
    if observatory.registry.has(DeviceRole.CAMERA):
        with contextlib.suppress(Exception):
            cooling = (await observatory.registry.get(DeviceRole.CAMERA, CameraDevice).status()).cooling
            if cooling.supported and cooling.enabled and cooling.target_c is not None:
                light["temp_c"] = cooling.target_c
    return light


@router.get("/filters")
async def filters(observatory: ObservatoryDep) -> dict:
    """The filters offered, and the one used last."""
    return {"known": _filters(observatory), "last": observatory.state.get("filters").get("last", "None")}


@router.get("")
async def list_sessions(observatory: ObservatoryDep) -> list[dict]:
    return [_out(observatory, session) for session in observatory.imaging.list()]


@router.post("")
async def create_session(payload: SessionIn, observatory: ObservatoryDep) -> dict:
    if payload.target_id:
        target = observatory.catalog.get(payload.target_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"no target {payload.target_id!r}")
        name, ra, dec = target.display_name, target.coord.ra_deg, target.coord.dec_deg
    elif payload.target_name:
        name, ra, dec = payload.target_name, payload.ra_deg, payload.dec_deg
    else:
        raise HTTPException(status_code=400, detail="a session needs a target")
    session = new_session(
        target_name=payload.target_name or name,
        target_id=payload.target_id,
        ra_deg=ra,
        dec_deg=dec,
        night=night_of(dt.datetime.now()),
        filter_name=observatory.state.get("filters").get("last", "None"),
        light=await _light_defaults(observatory),
    )
    observatory.imaging.save(session)
    observatory.current_session = session
    return _out(observatory, session)


@router.get("/{session_id}")
async def get_session(session_id: str, observatory: ObservatoryDep) -> dict:
    return _out(observatory, _load(observatory, session_id))


@router.patch("/{session_id}")
async def update_session(session_id: str, payload: SessionPatch, observatory: ObservatoryDep) -> dict:
    session = _load(observatory, session_id)
    if payload.target_name:
        session["target_name"] = payload.target_name
    if payload.filter:
        session["filter"] = payload.filter
        _remember_filter(observatory, payload.filter)
    for kind, patch in payload.groups.items():
        changes = patch.model_dump(exclude_unset=True)
        if changes.get("filter"):
            _remember_filter(observatory, changes["filter"])
        group = session["groups"][kind]
        # Against the fields a group of this kind has now, so a session
        # saved before a field existed can still be given it.
        group.update({k: v for k, v in changes.items() if k in new_group(kind)})
        # Typing a value into a following group is taking it over.
        taken = set(changes) - {"count", "auto_exposure", "filter", "linked"}
        if kind != "light" and "linked" not in changes and taken:
            group["linked"] = False
        if kind == "flat" and "exposure_s" in changes and "auto_exposure" not in changes:
            group["auto_exposure"] = changes["exposure_s"] is None
    observatory.imaging.save(relink(session))
    if observatory.current_session and observatory.current_session["id"] == session_id:
        observatory.current_session = session
    return _out(observatory, session)


@router.delete("/{session_id}")
async def delete_session(session_id: str, observatory: ObservatoryDep) -> dict:
    if not observatory.imaging.delete(session_id):
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
    if observatory.current_session and observatory.current_session["id"] == session_id:
        observatory.current_session = None
    return {"deleted": True, "id": session_id}


@router.post("/{session_id}/open")
async def open_session(session_id: str, observatory: ObservatoryDep) -> dict:
    """The session being worked: the night log goes in its folder."""
    session = _load(observatory, session_id)
    observatory.current_session = session
    return _out(observatory, session)


@router.post("/{session_id}/groups/{group}/start", response_model=TaskOut)
async def start_group(
    session_id: str, group: Group, payload: StartIn, observatory: ObservatoryDep
) -> TaskOut:
    _load(observatory, session_id)
    try:
        task = SessionGroupTask(observatory, group, session_id=session_id, into_library=payload.into_library)
    except AstropiError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _submit(observatory, task)


@library_router.get("")
async def library_sets(observatory: ObservatoryDep) -> list[dict]:
    return observatory.dark_library.sets()


@library_router.post("/shoot", response_model=TaskOut)
async def library_shoot(payload: LibraryShootIn, observatory: ObservatoryDep) -> TaskOut:
    settings = {**payload.model_dump(), "captured": 0}
    return _submit(observatory, SessionGroupTask(observatory, "dark", settings=settings, into_library=True))
