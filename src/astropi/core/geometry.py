"""Coordinate types and spherical maths.

Deliberately dependency-light (no astropy here) so that the hot paths -
guiding corrections at a few Hz, simulator frame generation - stay cheap
and importable anywhere. Anything needing real astrometry (precession,
refraction, frame conversion against a location and time) lives in
`astropi.services.ephemeris`, which is where astropy belongs.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


def normalize_deg(value: float, *, period: float = 360.0) -> float:
    """Wrap an angle into [0, period)."""
    result = math.fmod(value, period)
    return result + period if result < 0 else result


def wrap_symmetric_deg(value: float) -> float:
    """Wrap an angle into [-180, 180), the natural range for an error term."""
    return normalize_deg(value + 180.0) - 180.0


@dataclass(frozen=True, slots=True)
class RaDec:
    """An equatorial coordinate in degrees.

    `ra_deg` is degrees, not hours - hours only appear at the display edge.
    Storing one unit throughout removes a whole category of factor-of-15 bugs.
    """

    ra_deg: float
    dec_deg: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.dec_deg <= 90.0:
            raise ValueError(f"dec out of range: {self.dec_deg}")

    @property
    def ra_hours(self) -> float:
        return self.ra_deg / 15.0

    def normalized(self) -> RaDec:
        return RaDec(normalize_deg(self.ra_deg), self.dec_deg)

    def separation_deg(self, other: RaDec) -> float:
        return angular_separation_deg(self, other)

    def offset_by(self, d_ra_deg: float, d_dec_deg: float) -> RaDec:
        """Shift by deltas in each axis, clamping dec at the poles.

        `d_ra_deg` is a coordinate delta, not an on-sky distance; near the
        pole a degree of RA is a small fraction of a degree on sky. Use
        `separation_deg` when the question is "how far apart".
        """
        dec = max(-90.0, min(90.0, self.dec_deg + d_dec_deg))
        return RaDec(normalize_deg(self.ra_deg + d_ra_deg), dec)

    def __str__(self) -> str:
        return f"{format_hms(self.ra_deg)} {format_dms(self.dec_deg)}"


@dataclass(frozen=True, slots=True)
class AltAz:
    """A horizontal coordinate in degrees, azimuth measured east of north."""

    alt_deg: float
    az_deg: float


@dataclass(frozen=True, slots=True)
class PixelOffset:
    """A shift measured on the sensor, in pixels.

    The guider works in this space because that is what it can actually
    observe; converting to sky angles requires the calibration that maps
    mount pulses onto sensor axes.
    """

    dx: float
    dy: float

    @property
    def magnitude(self) -> float:
        return math.hypot(self.dx, self.dy)


def angular_separation_deg(a: RaDec, b: RaDec) -> float:
    """Great-circle separation via the Vincenty formula.

    Vincenty rather than the simpler spherical law of cosines because the
    latter loses precision exactly where it matters most here - at the
    sub-arcsecond separations that decide whether a solve has converged.
    """
    lat1, lat2 = math.radians(a.dec_deg), math.radians(b.dec_deg)
    d_lon = math.radians(b.ra_deg - a.ra_deg)

    sin_lat1, cos_lat1 = math.sin(lat1), math.cos(lat1)
    sin_lat2, cos_lat2 = math.sin(lat2), math.cos(lat2)
    sin_dlon, cos_dlon = math.sin(d_lon), math.cos(d_lon)

    num = math.hypot(cos_lat2 * sin_dlon, cos_lat1 * sin_lat2 - sin_lat1 * cos_lat2 * cos_dlon)
    den = sin_lat1 * sin_lat2 + cos_lat1 * cos_lat2 * cos_dlon
    return math.degrees(math.atan2(num, den))


def format_hms(ra_deg: float, *, decimals: int = 1) -> str:
    """Format right ascension as `HHhMMmSS.Ss`."""
    hours, minutes, seconds = _sexagesimal(normalize_deg(ra_deg) / 15.0, decimals)
    # Rounding can carry all the way round the clock; 24h is 0h.
    hours %= 24
    width = decimals + 3 if decimals else 2
    return f"{hours:02d}h{minutes:02d}m{seconds:0{width}.{decimals}f}s"


def format_dms(dec_deg: float, *, decimals: int = 1) -> str:
    """Format declination as `+DD°MM'SS.S"`."""
    sign = "-" if dec_deg < 0 else "+"
    degrees, minutes, seconds = _sexagesimal(abs(dec_deg), decimals)
    width = decimals + 3 if decimals else 2
    return f"{sign}{degrees:02d}\u00b0{minutes:02d}'{seconds:0{width}.{decimals}f}\""


def _sexagesimal(value: float, decimals: int) -> tuple[int, int, float]:
    """Split a positive value into whole units, minutes and seconds.

    Rounds to the displayed precision *before* splitting. Rounding each
    field independently lets seconds round up to 60 without carrying, so a
    declination of 29.99999 degrees prints as 29 degrees 59 minutes 60
    seconds instead of 30 degrees exactly.
    """
    total_seconds = round(value * 3600.0, decimals)
    units, remainder = divmod(total_seconds, 3600.0)
    minutes, seconds = divmod(remainder, 60.0)
    return int(units), int(minutes), seconds


_SEXAGESIMAL = re.compile(
    r"^\s*(?P<sign>[+-])?\s*(?P<a>\d+(?:\.\d+)?)\s*[hd:°]\s*"
    r"(?:(?P<b>\d+(?:\.\d+)?)\s*[m':]?\s*)?"
    r"(?:(?P<c>\d+(?:\.\d+)?)\s*[s\"]?\s*)?$",
    re.IGNORECASE,
)


def parse_angle(text: str, *, hours: bool = False) -> float:
    """Parse `12.5`, `12h30m00s`, `+41:16:09` or `-05°23'12\"` into degrees.

    `hours=True` treats a bare number and the leading field as hours, which
    is how catalogues and planetarium software hand over an RA.
    """
    text = text.strip()
    try:
        value = float(text)
    except ValueError:
        pass
    else:
        return value * 15.0 if hours else value

    match = _SEXAGESIMAL.match(text)
    if match is None:
        raise ValueError(f"cannot parse angle: {text!r}")

    a = float(match["a"])
    b = float(match["b"] or 0.0)
    c = float(match["c"] or 0.0)
    magnitude = a + b / 60.0 + c / 3600.0
    if hours or "h" in text.lower():
        magnitude *= 15.0
    return -magnitude if match["sign"] == "-" else magnitude
