"""The drift/efficiency fit, against a closed loop with known truth."""

from __future__ import annotations

import random

import pytest

from astropi.services.guidemodel import AxisModel


def run_loop(efficiency: float, drift: float, seeing: float, cycles: int = 100, seed: int = 3):
    """A plain proportional guide loop on one axis, fed to the model."""
    rng = random.Random(seed)
    model = AxisModel(window=cycles)
    position = 0.0
    for index in range(cycles):
        t = index * 2.5
        measured = position + rng.gauss(0.0, seeing)
        model.observe(t, measured)
        # The loop believes its corrections are exact: it predicts the
        # full push, and the sky delivers `efficiency` of it.
        predicted = -0.7 * measured
        model.pushed(predicted)
        position += efficiency * predicted + drift * 2.5
    return model.fit()


@pytest.mark.parametrize("efficiency", [0.5, 1.0, 1.6])
def test_recovers_efficiency_through_seeing(efficiency):
    # Seeing as large as the corrections: the regime where fitting frame
    # changes against the last push reports roughly double the truth.
    fits = [run_loop(efficiency, drift=0.05, seeing=1.0, seed=seed) for seed in range(30)]
    mean = sum(f.efficiency for f in fits) / len(fits)
    assert mean == pytest.approx(efficiency, abs=0.12)


def test_efficiency_waits_for_a_long_enough_window():
    fit = run_loop(1.0, drift=0.05, seeing=1.0, cycles=20)
    assert fit is not None
    assert fit.efficiency is None


def test_recovers_drift_the_loop_is_fighting():
    fits = [run_loop(1.0, drift=0.08, seeing=0.5, seed=seed) for seed in range(20)]
    mean = sum(f.drift_arcsec_per_s for f in fits) / len(fits)
    assert mean == pytest.approx(0.08, abs=0.02)
    assert sum(f.drift_is_real for f in fits) >= 15


def test_no_drift_is_not_reported_as_one():
    fits = [run_loop(1.0, drift=0.0, seeing=1.0, seed=seed) for seed in range(20)]
    assert sum(f.drift_is_real for f in fits) <= 3


def test_a_dither_is_not_drift():
    model = AxisModel(window=30, drift_window=20)
    for index in range(30):
        error = 5.0 if index >= 20 else 0.0
        if index == 20:
            # The lock moved by 5 arcsec; the error jumps with it.
            model.move_reference(-5.0)
        model.observe(index * 2.0, error)
    fit = model.fit()
    assert fit is not None
    assert abs(fit.drift_arcsec_per_s) < 1e-6


def test_needs_enough_cycles():
    model = AxisModel()
    model.observe(0.0, 1.0)
    assert model.fit() is None
