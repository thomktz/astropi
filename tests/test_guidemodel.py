"""The per-axis drift filter, against a star with known truth."""

from __future__ import annotations

import itertools
import math
import random

import pytest

from astropi.services.guidemodel import DriftFilter

FRAME_S = 2.4


def watch(drift, *, seeing=1.0, frames=120, agility=0.0003, seed=1, loop=None, response=1.0):
    """Frames of a drifting star through the filter.

    `drift` is arcsec per second, or a function of time. With `loop`, the
    filter's own estimates steer corrections - `loop(filter, dt)` returns
    the push, which the sky delivers at `response` of what was predicted.
    """
    rng = random.Random(seed)
    drift_at = drift if callable(drift) else (lambda _t: drift)
    kf = DriftFilter(agility)
    position = 0.0
    history = []
    for index in range(frames):
        t = index * FRAME_S
        kf.observe(t, position + rng.gauss(0.0, seeing))
        history.append((t, kf.drift, kf.drift_error, position))
        push = loop(kf, FRAME_S) if loop else 0.0
        kf.pushed(push)
        position += drift_at(t) * FRAME_S + response * push
    return kf, history


def test_a_steady_drift_is_found_and_then_holds_still():
    """Every frame refines one estimate - no restarts, no jumps."""
    kf, history = watch(0.1, seeing=1.0)
    assert kf.drift == pytest.approx(0.1, abs=0.01)
    settled = [d for _, d, _, _ in history[40:]]
    jumps = [abs(b - a) * 60 for a, b in itertools.pairwise(settled)]
    # Arcsec per minute, frame to frame, once known.
    assert max(jumps) < 0.3
    assert sum(jumps) / len(jumps) < 0.1


def test_it_knows_how_well_it_knows():
    kf, history = watch(0.05, seeing=1.0)
    first_known = next(t for t, _, error, _ in history if error * 60 < 1.0)
    assert first_known < 90
    assert kf.drift_error * 60 < 0.5
    # And the truth is inside the error it claims, most of the time.
    inside = sum(abs(d - 0.05) < 2 * e for _, d, e, _ in history[30:])
    assert inside > 0.9 * len(history[30:])


def test_no_drift_reads_as_none():
    kf, _ = watch(0.0, seeing=1.5)
    assert not kf.drift_is_real


def test_the_seeing_is_learned():
    kf, _ = watch(0.0, seeing=1.5, frames=200)
    assert kf.seeing == pytest.approx(1.5, rel=0.3)


def test_it_keeps_measuring_while_the_drift_is_cancelled():
    """Corrections are part of its prediction, not mistaken for the sky."""

    def cancel(kf, dt):
        return -(kf.drift * dt + 0.5 * kf.position)

    kf, history = watch(0.1, seeing=0.8, loop=cancel)
    assert kf.drift == pytest.approx(0.1, abs=0.01)
    # And the star is held: the position stays near the lock point.
    positions = [p for *_, p in history[60:]]
    assert math.sqrt(sum(p * p for p in positions) / len(positions)) < 1.0


def test_a_wrong_calibration_is_absorbed():
    """Corrections landing at half strength: it settles on twice the rate.

    What it then reports is the rate that has to be *sent* to hold the
    star - which is what cancelling needs - and the star is still held.
    """

    def cancel(kf, dt):
        return -(kf.drift * dt + 0.5 * kf.position)

    kf, history = watch(0.1, seeing=0.8, loop=cancel, response=0.5, frames=200)
    assert kf.drift == pytest.approx(0.2, rel=0.15)
    positions = [p for *_, p in history[120:]]
    assert math.sqrt(sum(p * p for p in positions) / len(positions)) < 1.5


def test_a_swinging_drift_is_followed_with_more_agility():
    """Periodic error: the RA rate swings over minutes."""
    worm = lambda t: 0.118 * math.cos(2 * math.pi * t / 479)  # noqa: E731
    errors = {}
    for agility in (0.0003, 0.003):
        _, history = watch(worm, seeing=1.5, frames=250, agility=agility)
        late = history[50:]
        errors[agility] = math.sqrt(sum((d - worm(t)) ** 2 for t, d, _, _ in late) / len(late)) * 60
    assert errors[0.003] < 3.5
    assert errors[0.003] < errors[0.0003]


def test_a_dither_moves_the_position_not_the_drift():
    kf, _ = watch(0.05, seeing=0.5, frames=60)
    drift, position = kf.drift, kf.position
    kf.move_reference(5.0)
    assert kf.drift == drift
    assert kf.position == pytest.approx(position - 5.0)


def test_nothing_before_the_first_frame():
    kf = DriftFilter(0.001)
    assert not kf.ready
    assert not kf.drift_is_real
    kf.pushed(3.0)
    kf.observe(0.0, 1.0)
    assert kf.ready
    assert kf.position == 1.0
