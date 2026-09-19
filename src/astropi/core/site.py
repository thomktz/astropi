"""The observing site.

Held by the backend rather than by each browser so that every device looking
at the dashboard agrees on where the telescope is. The rig has exactly one
location at a time, and pointing, rise/set times and polar alignment all
depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObservingSite:
    """Latitude and longitude in degrees, east-positive longitude."""

    latitude_deg: float
    longitude_deg: float
    elevation_m: float = 0.0
    name: str = "Home"

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError(f"latitude out of range: {self.latitude_deg}")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError(f"longitude out of range: {self.longitude_deg}")

    @property
    def hemisphere(self) -> str:
        return "north" if self.latitude_deg >= 0 else "south"
