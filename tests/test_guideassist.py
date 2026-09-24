"""The guiding assistant's arithmetic, against series with known answers.

The point of the assistant is separating what guiding should chase from
what it should not, so the tests inject a known drift and a known amount
of noise and check that both come back out.
"""

from __future__ import annotations

import random

import pytest

from astropi.services.guideassist import (
    BacklashMeasurement,
    DriftSample,
    analyse,
    measure_axis,
    polar_confidence,
    polar_error_arcmin,
)


def series(drift_per_min: float, noise: float, *, seconds: float = 4.0, count: int = 60):
    generator = random.Random(7)
    return [
        DriftSample(
            timestamp=index * seconds,
            ra_arcsec=generator.gauss(0, noise),
            dec_arcsec=drift_per_min * (index * seconds / 60.0) + generator.gauss(0, noise),
        )
        for index in range(count)
    ]


def test_drift_and_seeing_come_apart():
    """The slope and the wobble are different measurements of one series."""
    measured = measure_axis(series(1.5, 0.35), lambda sample: sample.dec_arcsec)

    assert measured.drift_arcsec_per_min == pytest.approx(1.5, abs=0.15)
    assert measured.seeing_rms_arcsec == pytest.approx(0.35, abs=0.1)


def test_a_steady_axis_reports_no_drift():
    measured = measure_axis(series(0.0, 0.3), lambda sample: sample.dec_arcsec)
    assert abs(measured.drift_arcsec_per_min) < 0.25


def test_polar_error_follows_the_standard_conversion():
    """One arcminute of polar error drifts 0.262 arcsec a minute.

    That figure - and its reciprocal, 3.81 - is what every drift
    alignment guide quotes. One arcminute is 2.909e-4 radians, the sky
    turns at 15.04 arcsec a second, and the product is 0.262.
    """
    assert polar_error_arcmin(0.262, 0.0) == pytest.approx(1.0, abs=0.01)
    # And it grows with declination, because the drift a given
    # misalignment produces shrinks as the cosine.
    assert polar_error_arcmin(0.262, 60.0) == pytest.approx(2.0, abs=0.02)


def test_near_the_pole_the_conversion_is_refused():
    """The cosine runs away and a small error becomes a huge one."""
    assert polar_error_arcmin(0.3, 89.0) == float("inf")

    report = analyse(series(0.5, 0.3), declination_deg=89.0, hour_angle_deg=0.0)
    assert report.polar_error_arcmin is None
    assert "pole" in report.polar_error_confidence


def test_confidence_names_which_error_is_being_measured():
    """Declination drift shows azimuth at the meridian, altitude away from it."""
    assert "azimuth" in polar_confidence(10.0, 0.0)
    assert "altitude" in polar_confidence(10.0, 90.0)
    assert "mixes" in polar_confidence(10.0, 45.0)


def test_the_dead_band_is_recommended_from_the_seeing():
    """Correcting inside the seeing moves the mount without helping."""
    report = analyse(
        series(0.2, 0.9), declination_deg=20.0, hour_angle_deg=10.0, current_min_move_arcsec=0.15
    )

    assert report.suggested_min_move_arcsec == pytest.approx(0.9, abs=0.25)
    assert any("dead band" in line for line in report.recommendations)


def test_quiet_seeing_is_not_reported_as_a_jump():
    """The outlier test compares steps with steps.

    It used to compare a rate in arcseconds per minute against a size in
    arcseconds, which fired on ordinary noise - the units were different
    by whatever the exposure length happened to be.
    """
    report = analyse(series(0.3, 0.4), declination_deg=20.0, hour_angle_deg=10.0)
    assert not any("jumped" in line for line in report.recommendations)


def test_a_real_jump_is_reported():
    samples = series(0.3, 0.4)
    samples[30] = DriftSample(samples[30].timestamp, 12.0, samples[30].dec_arcsec)

    report = analyse(samples, declination_deg=20.0, hour_angle_deg=10.0)
    assert any("jumped" in line for line in report.recommendations)


def test_heavy_backlash_recommends_one_way_declination():
    """Past a few seconds of it, guiding through backlash is hopeless."""
    report = analyse(
        series(-0.8, 0.3),
        declination_deg=20.0,
        hour_angle_deg=10.0,
        backlash=BacklashMeasurement(arcsec=6.0, milliseconds=4200, uncertainty_arcsec=0.5),
    )

    # Drifting south, so correct north only: the axis then never
    # reverses and never pays the backlash again.
    assert report.suggested_dec_mode == "north"
    assert any("one way only" in line for line in report.recommendations)


def test_polar_misalignment_is_called_out_with_its_consequence():
    report = analyse(series(2.0, 0.3), declination_deg=10.0, hour_angle_deg=5.0)

    assert report.polar_error_arcmin is not None
    assert report.polar_error_arcmin > 5
    # Guiding hides the drift; it does not hide the field rotation.
    assert any("rotate" in line for line in report.recommendations)
