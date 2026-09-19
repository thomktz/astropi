"""Where things are and when they are worth shooting.

This is the one place astropy is used. It is a heavy dependency with a slow
import, but sky positions are exactly the problem it exists to solve, and
the hand-rolled alternative silently drops precession, nutation, refraction
and parallax - all of which matter once a plate solve is judging the result.

Altitude curves are computed by sampling the night on a grid and reading
rise, set and transit off it, rather than root-finding each event. One
vectorised transform for a whole night costs about as much as a single
scalar one, and a five-minute grid is far finer than any decision made from
it needs.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time

from astropi.core.geometry import RaDec
from astropi.core.site import ObservingSite

#: Sampling interval for the night-long altitude grid.
SAMPLE_MINUTES = 5

#: Sun altitude defining each twilight stage.
TWILIGHT_CIVIL_DEG = -6.0
TWILIGHT_NAUTICAL_DEG = -12.0
TWILIGHT_ASTRONOMICAL_DEG = -18.0

PLANETS = ("mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune")


@dataclass(frozen=True, slots=True)
class AltitudeSample:
    when: datetime
    altitude_deg: float
    azimuth_deg: float


@dataclass(frozen=True, slots=True)
class Visibility:
    """A target's night, summarised."""

    altitude_now_deg: float
    azimuth_now_deg: float
    max_altitude_deg: float
    transit_at: datetime | None
    rises_at: datetime | None
    sets_at: datetime | None
    circumpolar: bool
    never_rises: bool
    hours_above_horizon: float
    moon_separation_deg: float
    samples: list[AltitudeSample]

    @property
    def is_up(self) -> bool:
        return self.altitude_now_deg > 0.0


@dataclass(frozen=True, slots=True)
class NightWindow:
    """Twilight boundaries for the coming night."""

    sunset: datetime | None
    sunrise: datetime | None
    astronomical_dusk: datetime | None
    astronomical_dawn: datetime | None
    moon_illumination: float
    moon_altitude_deg: float

    @property
    def dark_hours(self) -> float:
        if self.astronomical_dusk and self.astronomical_dawn:
            return max(0.0, (self.astronomical_dawn - self.astronomical_dusk).total_seconds() / 3600.0)
        return 0.0


@functools.lru_cache(maxsize=8)
def _location(latitude: float, longitude: float, elevation: float) -> EarthLocation:
    return EarthLocation(lat=latitude * u.deg, lon=longitude * u.deg, height=elevation * u.m)


def _site_location(site: ObservingSite) -> EarthLocation:
    return _location(site.latitude_deg, site.longitude_deg, site.elevation_m)


def _grid(start: datetime, hours: float) -> Time:
    steps = int(hours * 60 / SAMPLE_MINUTES) + 1
    return Time([start + timedelta(minutes=SAMPLE_MINUTES * i) for i in range(steps)])


def _night_start(now: datetime) -> datetime:
    """Begin the window at local noon, so an evening session sees one night.

    Starting at "now" would cut the night in half for anyone planning after
    midnight; starting at noon keeps dusk and dawn in the same window
    regardless of when the question is asked.
    """
    local_noon = now.astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    if now.astimezone() < local_noon:
        local_noon -= timedelta(days=1)
    return local_noon.astimezone(UTC)


class EphemerisService:
    """Altitude curves, twilight and moon geometry for one site."""

    def __init__(self, site: ObservingSite) -> None:
        self._site = site

    @property
    def site(self) -> ObservingSite:
        return self._site

    def with_site(self, site: ObservingSite) -> EphemerisService:
        return EphemerisService(site)

    def altaz_now(self, coord: RaDec, when: datetime | None = None) -> tuple[float, float]:
        when = when or datetime.now(UTC)
        frame = AltAz(obstime=Time(when), location=_site_location(self._site))
        transformed = SkyCoord(coord.ra_deg * u.deg, coord.dec_deg * u.deg, frame="icrs").transform_to(frame)
        return float(transformed.alt.deg), float(transformed.az.deg)

    def visibility(
        self, coord: RaDec, *, when: datetime | None = None, hours: float = 24.0
    ) -> Visibility:
        """Sample a target over the night and summarise it."""
        now = when or datetime.now(UTC)
        start = _night_start(now)
        times = _grid(start, hours)
        location = _site_location(self._site)
        frame = AltAz(obstime=times, location=location)

        target = SkyCoord(coord.ra_deg * u.deg, coord.dec_deg * u.deg, frame="icrs")
        altaz = target.transform_to(frame)
        altitudes = np.asarray(altaz.alt.deg, dtype=float)
        azimuths = np.asarray(altaz.az.deg, dtype=float)
        stamps = [t.to_datetime(timezone=UTC) for t in times]

        above = altitudes > 0.0
        circumpolar = bool(above.all())
        never_rises = bool(not above.any())

        peak = int(np.argmax(altitudes))
        rises_at = _first_crossing(stamps, altitudes, rising=True)
        sets_at = _first_crossing(stamps, altitudes, rising=False)

        # Into ICRS before comparing: the moon comes back in GCRS, and
        # astropy's separation between mismatched frames is direction
        # dependent and warns about it.
        moon = get_body("moon", Time(now), location).icrs
        separation = float(target.separation(moon).deg)
        altitude_now, azimuth_now = self.altaz_now(coord, now)

        return Visibility(
            altitude_now_deg=altitude_now,
            azimuth_now_deg=azimuth_now,
            max_altitude_deg=float(altitudes[peak]),
            transit_at=stamps[peak],
            rises_at=None if circumpolar or never_rises else rises_at,
            sets_at=None if circumpolar or never_rises else sets_at,
            circumpolar=circumpolar,
            never_rises=never_rises,
            hours_above_horizon=float(above.sum()) * SAMPLE_MINUTES / 60.0,
            moon_separation_deg=separation,
            samples=[
                AltitudeSample(when=s, altitude_deg=float(a), azimuth_deg=float(z))
                for s, a, z in zip(stamps, altitudes, azimuths, strict=True)
            ],
        )

    def altitudes_bulk(self, coords: list[RaDec], when: datetime | None = None) -> np.ndarray:
        """Altitude of many targets at one instant.

        One vectorised transform rather than a loop: ranking a thousand
        catalogue objects by how high they sit is a per-keystroke operation
        in the target picker, and per-object transforms would make it crawl.
        """
        if not coords:
            return np.empty(0)
        when = when or datetime.now(UTC)
        frame = AltAz(obstime=Time(when), location=_site_location(self._site))
        catalog = SkyCoord(
            [c.ra_deg for c in coords] * u.deg,
            [c.dec_deg for c in coords] * u.deg,
            frame="icrs",
        )
        return np.asarray(catalog.transform_to(frame).alt.deg, dtype=float)

    def night_window(self, when: datetime | None = None) -> NightWindow:
        now = when or datetime.now(UTC)
        start = _night_start(now)
        times = _grid(start, 24.0)
        location = _site_location(self._site)
        frame = AltAz(obstime=times, location=location)

        sun_alt = np.asarray(get_sun(times).transform_to(frame).alt.deg, dtype=float)
        stamps = [t.to_datetime(timezone=UTC) for t in times]

        moon_now = get_body("moon", Time(now), location)
        sun_now = get_sun(Time(now))
        elongation = float(sun_now.icrs.separation(moon_now.icrs).deg)
        # Illuminated fraction from the phase angle: the standard
        # approximation, good to a fraction of a percent.
        illumination = (1.0 - math.cos(math.radians(elongation))) / 2.0
        moon_altitude = float(
            moon_now.transform_to(AltAz(obstime=Time(now), location=location)).alt.deg
        )

        return NightWindow(
            sunset=_first_crossing(stamps, sun_alt, rising=False, level=-0.833),
            sunrise=_first_crossing(stamps, sun_alt, rising=True, level=-0.833),
            astronomical_dusk=_first_crossing(
                stamps, sun_alt, rising=False, level=TWILIGHT_ASTRONOMICAL_DEG
            ),
            astronomical_dawn=_first_crossing(
                stamps, sun_alt, rising=True, level=TWILIGHT_ASTRONOMICAL_DEG
            ),
            moon_illumination=illumination,
            moon_altitude_deg=moon_altitude,
        )

    def planet_positions(self, when: datetime | None = None) -> dict[str, RaDec]:
        """Current positions of the naked-eye planets.

        Taken from astropy's built-in ephemeris rather than Keplerian
        elements - it is both more accurate and less code to maintain.
        """
        when = when or datetime.now(UTC)
        time = Time(when)
        location = _site_location(self._site)
        positions: dict[str, RaDec] = {}
        for name in PLANETS:
            body = get_body(name, time, location).icrs
            positions[name] = RaDec(float(body.ra.deg), float(body.dec.deg))
        return positions


def _first_crossing(
    stamps: list[datetime], values: np.ndarray, *, rising: bool, level: float = 0.0
) -> datetime | None:
    """Time a sampled curve crosses `level`, interpolated between samples."""
    for i in range(len(values) - 1):
        low, high = values[i], values[i + 1]
        crossed = (low <= level < high) if rising else (low > level >= high)
        if not crossed:
            continue
        span = high - low
        fraction = 0.0 if span == 0 else (level - low) / span
        delta = (stamps[i + 1] - stamps[i]).total_seconds() * fraction
        return stamps[i] + timedelta(seconds=delta)
    return None
