"""The frames on the SSD: folders to browse, and JPEG previews of each frame."""

from __future__ import annotations

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
    observatory: ObservatoryDep, path: str, size: Literal["thumb", "large"] = "thumb"
) -> FileResponse:
    jpeg = await observatory.browser.preview(path, size)
    # The URL carries the frame's modified time, so a cached copy is never stale.
    return FileResponse(jpeg, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
