"""Sidereal time, without astropy.

These are the low-precision formulae (good to roughly a second of time over
the next few decades), duplicated here rather than delegated to astropy
because the simulator and the guide loop evaluate them thousands of times
per session and an astropy `Time` round-trip costs milliseconds each. The
high-precision path in `astropi.services.ephemeris` is the one used for
anything an observer actually reads.
"""

from __future__ import annotations

import time

from astropi.core.geometry import normalize_deg

#: Sidereal rate in degrees of hour angle per second of UTC.
SIDEREAL_RATE_DEG_PER_S = 360.98564736629 / 86400.0

#: Sidereal seconds elapse slightly faster than solar ones.
SIDEREAL_TO_SOLAR = 1.0027379093

J2000_JD = 2451545.0
UNIX_EPOCH_JD = 2440587.5


def julian_date(unix_time: float | None = None) -> float:
    return (time.time() if unix_time is None else unix_time) / 86400.0 + UNIX_EPOCH_JD


def gmst_deg(unix_time: float | None = None) -> float:
    """Greenwich Mean Sidereal Time in degrees."""
    jd = julian_date(unix_time)
    d = jd - J2000_JD
    centuries = d / 36525.0
    gmst = (
        280.46061837
        + 360.98564736629 * d
        + 0.000387933 * centuries * centuries
        - (centuries**3) / 38710000.0
    )
    return normalize_deg(gmst)


def local_sidereal_time_deg(longitude_deg: float, unix_time: float | None = None) -> float:
    """Local Sidereal Time in degrees, for an east-positive longitude."""
    return normalize_deg(gmst_deg(unix_time) + longitude_deg)


def hour_angle_deg(ra_deg: float, longitude_deg: float, unix_time: float | None = None) -> float:
    """Hour angle of a right ascension, wrapped to [-180, 180).

    Negative is east of the meridian (not yet culminated), positive is west.
    """
    ha = local_sidereal_time_deg(longitude_deg, unix_time) - ra_deg
    return normalize_deg(ha + 180.0) - 180.0
