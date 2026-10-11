"""The frames on the SSD: folders to browse, JPEG previews of each frame, and deleting."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse

from astropi.api.deps import ObservatoryDep

router = APIRouter(prefix="/library", tags=["library"])


@router.get("")
async def listing(observatory: ObservatoryDep, path: str = "") -> dict:
    return observatory.browser.listing(path)


@router.get("/preview.jpg")
async def preview(
    observatory: ObservatoryDep, path: str, size: Literal["thumb", "large", "full"] = "thumb"
) -> FileResponse:
    jpeg = await observatory.browser.preview(path, size)
    # The URL carries the frame's modified time, so a cached copy is never stale.
    return FileResponse(jpeg, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/summary")
async def summary(observatory: ObservatoryDep, path: str) -> dict:
    """What deleting `path` would remove: for the confirmation, before anything goes."""
    return await asyncio.to_thread(observatory.browser.summary, path)


@router.delete("")
async def delete(observatory: ObservatoryDep, path: str) -> dict:
    """Delete a frame or a whole folder, with its cached previews. There is no undo."""
    return await asyncio.to_thread(observatory.browser.delete, path)
