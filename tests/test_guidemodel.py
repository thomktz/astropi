"""The guided-drift readout, against a closed loop with known truth."""

from __future__ import annotations

import random

import pytest

from astropi.services.guidemodel import AxisModel


def run_loop(drift: float, seeing: float, cycles: int = 20, seed: int = 3):
    """A plain proportional guide loop on one axis, fed to the model."""
    rng = random.Random(seed)
    model = AxisModel(window=cycles)
    position = 0.0
    for index in range(cycles):
        measured = position + rng.gauss(0.0, seeing)
        model.observe(index * 2.5, measured)
        push = -0.7 * measured
        model.pushed(push)
        position += push + drift * 2.5
    return model.fit()


def test_sees_the_drift_the_loop_is_hiding():
    # Guided, the error itself barely moves; the drift is in the pushes.
    fits = [run_loop(drift=0.08, seeing=0.5, seed=seed) for seed in range(20)]
    mean = sum(f.drift_arcsec_per_s for f in fits) / len(fits)
    assert mean == pytest.approx(0.08, abs=0.02)
    assert sum(f.drift_is_real for f in fits) >= 15


def test_no_drift_is_not_reported_as_one():
    fits = [run_loop(drift=0.0, seeing=1.0, seed=seed) for seed in range(20)]
    assert sum(f.drift_is_real for f in fits) <= 3


def test_a_dither_is_not_drift():
    model = AxisModel(window=30)
    for index in range(30):
        error = 5.0 if index >= 15 else 0.0
        if index == 15:
            # The lock moved by 5 arcsec; the error jumps with it.
            model.move_reference(-5.0)
        model.observe(index * 2.0, error)
    fit = model.fit()
    assert fit is not None
    assert abs(fit.drift_arcsec_per_s) < 1e-6


def test_needs_enough_frames():
    model = AxisModel()
    model.observe(0.0, 1.0)
    assert model.fit() is None


def _cancel(drift: float, response: float, noise: float, seed: int, rounds: int = 6):
    """Rounds of measure-and-cancel against an axis with known truth."""
    from astropi.services.guidemodel import DriftCanceller

    rng = random.Random(seed)
    axis = DriftCanceller(tolerance=1.0 / 60)
    for index in range(rounds):
        left_over = drift - response * axis.rate + rng.gauss(0.0, noise)
        if not axis.update(left_over, noise):
            return axis, index + 1
    return axis, None


def test_right_calibration_cancels_in_one_step():
    axis, rounds = _cancel(drift=1.3, response=1.0, noise=0.005, seed=1)
    assert rounds == 2
    assert axis.rate == pytest.approx(1.3, rel=0.05)


@pytest.mark.parametrize("response", [0.5, 1.6])
def test_a_wrong_calibration_is_learned_from_the_rounds(response):
    """Two rounds at two rates say how much of a correction lands."""
    for seed in range(10):
        axis, rounds = _cancel(drift=1.3, response=response, noise=0.005, seed=seed)
        assert rounds is not None and rounds <= 4
        assert axis.response == pytest.approx(response, rel=0.15)
        # Cancelling the sky's drift takes 1/response of it, calibrated.
        assert axis.rate == pytest.approx(1.3 / response, rel=0.1)


def test_noise_does_not_become_a_response():
    """A step too small to measure against the noise teaches nothing."""
    from astropi.services.guidemodel import DriftCanceller

    axis = DriftCanceller(tolerance=0.0)
    axis.update(0.02, 0.01)
    axis.update(0.03, 0.01)
    assert not axis.response_measured
    assert axis.response == 1.0


def test_a_swinging_drift_is_not_chased():
    """Periodic error: a slope that changes every round cannot be cancelled."""
    from astropi.services.guidemodel import DriftCanceller

    axis = DriftCanceller(tolerance=1.0 / 60)
    slopes = [0.10, -0.05, 0.08, -0.09, 0.07, -0.02]
    rounds = 0
    for slope in slopes:
        rounds += 1
        if not axis.update(slope - axis.rate, 0.005):
            break
    assert axis.unsteady
    assert rounds <= 3
    # Only the steady part - here, what the rounds averaged - is cancelled.
    assert abs(axis.rate) < 0.06
