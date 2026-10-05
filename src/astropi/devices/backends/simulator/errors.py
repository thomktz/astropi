"""What the sky does to a mount that believes it is perfect.

The simulated mount models its own polar misalignment, worm error and
seeing. A *real* mount driving the simulated camera does not: the camera
draws the sky exactly where the mount's counters say it points, which is
a sky with nothing wrong in it - guiding on that has nothing to correct,
and every movement on screen is one the guider made.

This puts those errors back, on top of whatever mount is pointing. The
mount's counters are taken as its axis angles, those angles are turned
about a pole that is deliberately not the true one, and the result is
where the telescope "really" looks - which drifts as the mount tracks,
exactly as a misaligned mount's field does. Nothing is sent to the mount:
this only changes what the pretend sensor sees, so corrections the guider
sends in answer are real corrections, moving the real mount.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from astropi.core.geometry import RaDec
from astropi.core.pointing import misaligned_pole, true_pole, vector_to_radec
from astropi.core.site import ObservingSite
from astropi.core.timekeeping import local_sidereal_time_deg


@dataclass(slots=True)
class SkyErrorsConfig:
    enabled: bool = True
    #: Where the mount's polar axis really points, relative to the pole:
    #: positive altitude too high, positive azimuth east. The defaults give
    #: a few arcseconds a minute of declination drift, a typical quick
    #: field alignment.
    polar_alt_error_arcmin: float = 20.0
    polar_az_error_arcmin: float = -12.0
    #: Peak-to-peak worm error in right ascension, and its period.
    periodic_error_arcsec: float = 18.0
    periodic_error_period_s: float = 479.0
    #: Scatter of one frame's star position from the atmosphere, per axis.
    seeing_arcsec: float = 0.8


def sky_errors_out(config: SkyErrorsConfig) -> dict:
    return {
        "enabled": config.enabled,
        "polar_alt_error_arcmin": config.polar_alt_error_arcmin,
        "polar_az_error_arcmin": config.polar_az_error_arcmin,
        "periodic_error_arcsec": config.periodic_error_arcsec,
        "periodic_error_period_s": config.periodic_error_period_s,
        "seeing_arcsec": config.seeing_arcsec,
    }


class SkyErrors:
    """Polar misalignment, periodic error and seeing, for any mount."""

    def __init__(self, site: ObservingSite, config: SkyErrorsConfig | None = None, seed: int | None = None):
        self.config = config or SkyErrorsConfig()
        self._site = site
        self._random = random.Random(seed)

    def set_site(self, site: ObservingSite) -> None:
        self._site = site

    def apply(self, reported: RaDec, at: float | None = None) -> RaDec:
        """Where a mount reporting `reported` is really looking."""
        config = self.config
        if not config.enabled:
            return reported
        at = time.time() if at is None else at
        latitude = self._site.latitude_deg
        lst = local_sidereal_time_deg(self._site.longitude_deg, at)

        # The mount's own axis angles: it computes its coordinates as if
        # it turned about the true pole.
        hour_angle = lst - reported.ra_deg
        dec = reported.dec_deg

        phase = 2 * math.pi * (at % config.periodic_error_period_s) / config.periodic_error_period_s
        worm = config.periodic_error_arcsec / 2.0 * math.sin(phase)
        seeing_ra = self._random.gauss(0.0, config.seeing_arcsec)
        seeing_dec = self._random.gauss(0.0, config.seeing_arcsec)

        pole = misaligned_pole(
            latitude, config.polar_alt_error_arcmin / 60.0, config.polar_az_error_arcmin / 60.0
        )
        # Worm error turns the axis; seeing moves the star on the sky, and
        # an arcsecond on the sky is more than one of hour angle away from
        # the equator.
        stretch = 1.0 / max(math.cos(math.radians(dec)), 0.05)
        direction = pole.pointing(
            hour_angle + (worm + seeing_ra * stretch) / 3600.0,
            dec + seeing_dec / 3600.0,
        )
        return vector_to_radec(direction, lst, true_pole(latitude))
