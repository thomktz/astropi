"""Health, rig description, and the observing site."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query

from astropi.api.deps import ObservatoryDep
from astropi.api.schemas import NightOut, SiteIn, SiteOut
from astropi.core.site import ObservingSite
from astropi.storage import drive

router = APIRouter(tags=["system"])

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/system")
async def system(observatory: ObservatoryDep) -> dict:
    return observatory.describe()


@router.get("/site", response_model=SiteOut)
async def get_site(observatory: ObservatoryDep) -> SiteOut:
    site = observatory.site
    return SiteOut(
        latitude_deg=site.latitude_deg,
        longitude_deg=site.longitude_deg,
        elevation_m=site.elevation_m,
        name=site.name,
        hemisphere=site.hemisphere,
    )


@router.put("/site", response_model=SiteOut)
async def set_site(payload: SiteIn, observatory: ObservatoryDep) -> SiteOut:
    """Move the observing site.

    Held server-side rather than per-browser so every device on the network
    agrees where the telescope is - the previous per-browser localStorage
    approach meant a phone and a laptop could disagree about the horizon.
    """
    observatory.set_site(
        ObservingSite(
            latitude_deg=payload.latitude_deg,
            longitude_deg=payload.longitude_deg,
            elevation_m=payload.elevation_m,
            name=payload.name,
        )
    )
    return await get_site(observatory)


@router.get("/site/search")
async def search_places(q: str = Query(min_length=2), count: int = 8) -> list[dict]:
    """Look up a place name, so a site can be set without knowing its
    coordinates.

    Proxied through the backend rather than called from the browser: the Pi
    serves the dashboard over plain HTTP on the LAN, and a page served that
    way cannot use the browser geolocation API at all.
    """
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            response = await client.get(GEOCODING_URL, params={"name": q, "count": count, "format": "json"})
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise HTTPException(status_code=503, detail=f"geocoding unavailable: {error}") from error

    return [
        {
            "name": entry.get("name"),
            "country": entry.get("country"),
            "admin1": entry.get("admin1"),
            "latitude_deg": entry.get("latitude"),
            "longitude_deg": entry.get("longitude"),
            "elevation_m": entry.get("elevation", 0.0),
        }
        for entry in response.json().get("results", [])
    ]


@router.get("/night", response_model=NightOut)
async def night(observatory: ObservatoryDep) -> NightOut:
    """Twilight and moon for the coming night."""
    window = observatory.ephemeris.night_window()
    return NightOut(
        sunset=window.sunset,
        sunrise=window.sunrise,
        astronomical_dusk=window.astronomical_dusk,
        astronomical_dawn=window.astronomical_dawn,
        dark_hours=round(window.dark_hours, 2),
        moon_illumination=round(window.moon_illumination, 3),
        moon_altitude_deg=round(window.moon_altitude_deg, 2),
    )


@router.get("/storage")
async def storage(observatory: ObservatoryDep) -> dict:
    """Whether the drive frames are written to is there, and how full it is."""
    return drive.status(observatory.archive.root, observatory.settings.frames_mount_unit).as_dict()


@router.post("/storage/mount")
async def mount_storage(observatory: ObservatoryDep) -> dict:
    """Mount the frames drive, through its fstab entry, without a password.

    For a drive that was plugged in after boot, or that systemd gave up
    waiting for at boot.
    """
    current = drive.status(observatory.archive.root, observatory.settings.frames_mount_unit)
    if current.mounted:
        return current.as_dict()
    if current.mount_unit is None:
        raise HTTPException(
            status_code=400, detail=f"{current.path} is not on a drive that can be mounted from here"
        )
    await drive.mount(current.mount_unit)
    return drive.status(observatory.archive.root, observatory.settings.frames_mount_unit).as_dict()
