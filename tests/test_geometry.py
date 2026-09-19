from __future__ import annotations

import math

import pytest

from astropi.core.geometry import (
    RaDec,
    angular_separation_deg,
    format_dms,
    format_hms,
    normalize_deg,
    parse_angle,
    wrap_symmetric_deg,
)


def test_normalize_wraps_into_range():
    assert normalize_deg(370.0) == pytest.approx(10.0)
    assert normalize_deg(-10.0) == pytest.approx(350.0)


def test_wrap_symmetric_centres_on_zero():
    assert wrap_symmetric_deg(350.0) == pytest.approx(-10.0)
    assert wrap_symmetric_deg(190.0) == pytest.approx(-170.0)


def test_declination_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        RaDec(0.0, 91.0)


def test_separation_of_identical_points_is_zero():
    coord = RaDec(83.822, -5.391)
    assert coord.separation_deg(coord) == pytest.approx(0.0, abs=1e-12)


def test_separation_agrees_with_astropy():
    """Cross-check the hand-rolled Vincenty formula against astropy.

    This is the reason the fast path can skip astropy at all: it has to
    give the same answer, and a regression here would quietly degrade
    every solve-convergence check in the codebase.
    """
    import astropy.units as u
    from astropy.coordinates import SkyCoord

    pairs = [
        ((88.7929, 7.4071), (78.6345, -8.2016)),  # Betelgeuse - Rigel
        ((10.6847, 41.2690), (10.6947, 41.2691)),  # sub-arcminute
        ((0.0, 89.9), (180.0, 89.9)),  # across the pole
        ((359.99, 0.0), (0.01, 0.0)),  # across the RA wrap
    ]
    for (ra1, dec1), (ra2, dec2) in pairs:
        ours = angular_separation_deg(RaDec(ra1, dec1), RaDec(ra2, dec2))
        theirs = SkyCoord(ra1 * u.deg, dec1 * u.deg).separation(
            SkyCoord(ra2 * u.deg, dec2 * u.deg)
        ).deg
        assert ours == pytest.approx(theirs, abs=1e-9)


def test_separation_near_pole_is_small_despite_large_ra_difference():
    """Two points an hour apart in RA are close together near the pole."""
    a = RaDec(0.0, 89.9)
    b = RaDec(180.0, 89.9)
    assert angular_separation_deg(a, b) == pytest.approx(0.2, abs=1e-6)


@pytest.mark.parametrize(
    ("text", "hours", "expected"),
    [
        ("10.6847", False, 10.6847),
        ("00h42m44.3s", False, 10.6846),
        ("0h42m44.3s", True, 10.6846),
        ("+41:16:09", False, 41.2692),
        ("-05°23'12\"", False, -5.3867),
        ("2.5", True, 37.5),
    ],
)
def test_parse_angle(text, hours, expected):
    assert parse_angle(text, hours=hours) == pytest.approx(expected, abs=1e-3)


def test_parse_angle_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_angle("not an angle")


def test_formatting_round_trips_through_parsing():
    """Display precision is a tenth of a second and a tenth of an arcsecond.

    That bounds the round-trip error at 1.5 arcsec in RA and 0.05 arcsec in
    declination - fine for a readout, and far finer than any pointing
    decision made from it.
    """
    coord = RaDec(83.6331, 22.0145)
    ra_tolerance = 1.6 / 3600.0
    dec_tolerance = 0.06 / 3600.0
    assert parse_angle(format_hms(coord.ra_deg), hours=True) == pytest.approx(
        coord.ra_deg, abs=ra_tolerance
    )
    assert parse_angle(format_dms(coord.dec_deg)) == pytest.approx(coord.dec_deg, abs=dec_tolerance)


def test_offset_clamps_at_the_pole():
    assert RaDec(0.0, 89.0).offset_by(0.0, 5.0).dec_deg == pytest.approx(90.0)


def test_ra_hours_conversion():
    assert RaDec(180.0, 0.0).ra_hours == pytest.approx(12.0)
    assert math.isclose(RaDec(15.0, 0.0).ra_hours, 1.0)
