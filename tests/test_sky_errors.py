"""The simulated sky's errors, for a mount that has none of its own."""

from __future__ import annotations

import math

import pytest

from astropi.core.geometry import RaDec
from astropi.core.timekeeping import local_sidereal_time_deg
from astropi.devices.backends.simulator.errors import SkyErrors, SkyErrorsConfig

T0 = 1_790_000_000.0


def tracking(site, at: float, hour_angle_deg: float = 20.0, dec: float = 40.0) -> RaDec:
    """What a tracking mount reports: the same RA and Dec, all night."""
    ra = local_sidereal_time_deg(site.longitude_deg, T0) - hour_angle_deg
    return RaDec(ra % 360.0, dec)


def still(config: SkyErrorsConfig) -> SkyErrorsConfig:
    config.periodic_error_arcsec = 0.0
    config.seeing_arcsec = 0.0
    return config


def test_switched_off_it_is_a_perfect_sky(site):
    sky = SkyErrors(site, SkyErrorsConfig(enabled=False))
    reported = tracking(site, T0)
    assert sky.apply(reported, T0) == reported


def test_a_misaligned_pole_makes_a_tracking_mount_drift(site):
    """The whole point: something for guiding to correct."""
    sky = SkyErrors(site, still(SkyErrorsConfig(polar_alt_error_arcmin=20.0, polar_az_error_arcmin=-12.0)))
    reported = tracking(site, T0)
    before = sky.apply(reported, T0)
    after = sky.apply(reported, T0 + 600.0)
    # On the sky, in arcsec per minute. Which axis it lands on depends on
    # where in the sky the mount points: altitude and azimuth errors
    # trade off with hour angle, and here most of it is in RA.
    ra_drift = (after.ra_deg - before.ra_deg) * 3600.0 * math.cos(math.radians(reported.dec_deg))
    dec_drift = (after.dec_deg - before.dec_deg) * 3600.0
    total = math.hypot(ra_drift, dec_drift) / 10.0
    # A few, as a rough field alignment gives.
    assert 1.0 < total < 20.0


def test_a_perfect_pole_does_not_drift(site):
    sky = SkyErrors(site, still(SkyErrorsConfig(polar_alt_error_arcmin=0.0, polar_az_error_arcmin=0.0)))
    reported = tracking(site, T0)
    before = sky.apply(reported, T0)
    after = sky.apply(reported, T0 + 600.0)
    assert abs(after.dec_deg - before.dec_deg) * 3600.0 < 0.01
    assert abs(after.ra_deg - before.ra_deg) * 3600.0 < 0.01


def test_seeing_scatters_the_star_by_about_what_was_asked(site):
    sky = SkyErrors(site, SkyErrorsConfig(periodic_error_arcsec=0.0, seeing_arcsec=1.0), seed=3)
    reported = tracking(site, T0)
    decs = [sky.apply(reported, T0).dec_deg * 3600.0 for _ in range(400)]
    mean = sum(decs) / len(decs)
    spread = math.sqrt(sum((d - mean) ** 2 for d in decs) / len(decs))
    assert spread == pytest.approx(1.0, rel=0.2)
