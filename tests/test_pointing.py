"""The mount geometry both the simulator and polar alignment depend on."""

from __future__ import annotations

import pytest

from astropi.core.geometry import RaDec
from astropi.core.pointing import (
    misaligned_pole,
    radec_to_vector,
    true_pole,
    vector_to_alt_az,
    vector_to_radec,
)

LATITUDE = 48.85


def test_pole_sits_at_latitude_due_north():
    altitude, azimuth = vector_to_alt_az(true_pole(LATITUDE).pointing(0.0, 90.0))
    assert altitude == pytest.approx(LATITUDE, abs=1e-6)
    assert azimuth == pytest.approx(0.0, abs=1e-6)


def test_object_on_the_meridian_is_due_south_at_expected_altitude():
    altitude, azimuth = vector_to_alt_az(true_pole(LATITUDE).pointing(0.0, 30.0))
    assert altitude == pytest.approx(90.0 - (LATITUDE - 30.0), abs=1e-6)
    assert azimuth == pytest.approx(180.0, abs=1e-6)


def test_radec_round_trips_through_the_horizontal_frame():
    pole = true_pole(LATITUDE)
    coord = RaDec(100.0, 30.0)
    recovered = vector_to_radec(radec_to_vector(coord, 123.456, pole), 123.456, pole)
    assert recovered.ra_deg == pytest.approx(coord.ra_deg, abs=1e-9)
    assert recovered.dec_deg == pytest.approx(coord.dec_deg, abs=1e-9)


def test_perfect_alignment_does_not_drift():
    """A mount on the pole tracks a fixed RA/Dec indefinitely."""
    pole = true_pole(LATITUDE)
    coord = RaDec(100.0, 30.0)
    first = vector_to_radec(pole.pointing(50.0 - coord.ra_deg, coord.dec_deg), 50.0, pole)
    # An hour later: sidereal time has advanced 15 degrees, and so has the axis.
    later = vector_to_radec(pole.pointing(65.0 - coord.ra_deg, coord.dec_deg), 65.0, pole)
    assert first.separation_deg(later) == pytest.approx(0.0, abs=1e-9)


def test_misalignment_causes_drift():
    """Half a degree off the pole drifts several arcminutes in an hour."""
    truth = true_pole(LATITUDE)
    mount = misaligned_pole(LATITUDE, alt_error_deg=0.5, az_error_deg=0.0)
    coord = RaDec(100.0, 30.0)
    first = vector_to_radec(mount.pointing(50.0 - coord.ra_deg, coord.dec_deg), 50.0, truth)
    later = vector_to_radec(mount.pointing(65.0 - coord.ra_deg, coord.dec_deg), 65.0, truth)
    drift_arcmin = first.separation_deg(later) * 60.0
    assert 2.0 < drift_arcmin < 15.0


def test_misalignment_causes_pointing_error():
    truth = true_pole(LATITUDE)
    mount = misaligned_pole(LATITUDE, alt_error_deg=0.5, az_error_deg=0.0)
    target = RaDec(100.0, 30.0)
    actual = vector_to_radec(mount.pointing(123.0 - target.ra_deg, target.dec_deg), 123.0, truth)
    assert actual.separation_deg(target) == pytest.approx(0.5, abs=0.15)
