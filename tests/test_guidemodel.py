"""The pixel model - drift and correction response - against known truth."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from astropi.services.guidemodel import PixelModel, Response

FRAME_S = 2.4
#: One arcsec of RA axis and one declination step, as a GTi at +58 deg
#: with the camera square to the axes might see them.
TRUE = Response(ra=(-0.45, 0.02), dec=(0.0, -0.21))


def turned(response: Response, degrees: float, scale: float) -> Response:
    a = math.radians(degrees)

    def one(v):
        return (
            scale * (v[0] * math.cos(a) - v[1] * math.sin(a)),
            scale * (v[0] * math.sin(a) + v[1] * math.cos(a)),
        )

    return Response(ra=one(response.ra), dec=one(response.dec))


def run(calibration=TRUE, *, drift=(0.004, -0.010), seeing=0.25, frames=300, measure=15, seed=1):
    """A star on the sensor, guided by the model the way the loop does it.

    Right ascension continuous, declination in whole steps. Returns the
    held offsets after settling, and the last fit.
    """
    rng = random.Random(seed)
    model = PixelModel(calibration)
    x = y = 0.0
    held, fit = [], None
    for k in range(frames):
        model.observe(k * FRAME_S, x + rng.gauss(0, seeing), y + rng.gauss(0, seeing))
        fit = model.fit()
        ra = dec = 0.0
        if fit is not None and k >= measure:
            want = (
                -(fit.drift[0] * FRAME_S + 0.6 * fit.position[0]),
                -(fit.drift[1] * FRAME_S + 0.6 * fit.position[1]),
            )
            ra, dec_units = fit.response.solve(*want) or (0.0, 0.0)
            dec = float(round(dec_units))
        x += drift[0] * FRAME_S + TRUE.ra[0] * ra + TRUE.dec[0] * dec
        y += drift[1] * FRAME_S + TRUE.ra[1] * ra + TRUE.dec[1] * dec
        model.sent(ra, dec)
        if k >= measure + 40:
            held.append((x, y))
    return held, fit


def rms(points):
    return math.sqrt(sum(x * x + y * y for x, y in points) / len(points))


def test_with_no_corrections_it_is_the_drift():
    drifts = [run(measure=10**6, seed=seed)[1].drift for seed in range(30)]
    assert statistics.mean(d[0] for d in drifts) * 60 == pytest.approx(0.24, abs=0.06)
    assert statistics.mean(d[1] for d in drifts) * 60 == pytest.approx(-0.60, abs=0.06)


def test_it_holds_the_star_and_keeps_measuring_the_drift():
    held, _ = run()
    # Seeing is 0.25 px a frame; held to less than that.
    assert rms(held) < 0.25
    drifts = [run(seed=seed)[1].drift for seed in range(20)]
    assert statistics.mean(d[1] for d in drifts) * 60 == pytest.approx(-0.60, abs=0.1)


@pytest.mark.parametrize(("degrees", "scale"), [(10, 1.3), (0, 0.7)])
def test_a_wrong_calibration_still_holds_the_star(degrees, scale):
    held, _ = run(turned(TRUE, degrees, scale))
    assert rms(held) < 0.3


def test_it_knows_how_well_it_knows_the_drift():
    fits = [run(measure=10**6, seed=seed)[1] for seed in range(30)]
    spread = statistics.stdev(f.drift[1] for f in fits)
    claimed = statistics.mean(f.drift_error[1] for f in fits)
    assert claimed == pytest.approx(spread, rel=0.4)


def test_the_responses_stay_at_the_calibration_until_shown_otherwise():
    _, fit = run(measure=10**6)
    assert fit.response.ra == pytest.approx(TRUE.ra, abs=1e-6)
    assert fit.response.dec == pytest.approx(TRUE.dec, abs=1e-6)


def test_solving_for_a_move_inverts_the_responses():
    ra, dec = TRUE.solve(1.0, -0.5)
    assert TRUE.ra[0] * ra + TRUE.dec[0] * dec == pytest.approx(1.0)
    assert TRUE.ra[1] * ra + TRUE.dec[1] * dec == pytest.approx(-0.5)


def test_a_dither_moves_every_offset_not_the_drift():
    model = PixelModel(TRUE)
    for k in range(20):
        model.observe(k * FRAME_S, 0.01 * k, 0.0)
    before = model.fit()
    model.move_reference(5.0, -3.0)
    after = model.fit()
    assert after.drift == pytest.approx(before.drift)
    assert after.position == pytest.approx((before.position[0] - 5.0, before.position[1] + 3.0))


def test_nothing_until_enough_frames():
    model = PixelModel(TRUE)
    model.observe(0.0, 0.0, 0.0)
    assert model.fit() is None


#: The simulator's worm: its period, and half its peak-to-peak swing in
#: arcsec of RA axis - the units `TRUE.ra` is per.
WORM_PERIOD_S = 479.0
WORM_UNITS = 9.0


def worm_track(model, *, frames=200, drift=(0.004, -0.010), seeing=0.25, seed=1, start=1.7e9):
    """An unguided star, drifting and swung by the worm, into a model."""
    rng = random.Random(seed)
    for k in range(frames):
        t = start + k * FRAME_S
        worm = WORM_UNITS * math.sin(2 * math.pi * t / WORM_PERIOD_S)
        model.observe(
            t,
            drift[0] * k * FRAME_S + TRUE.ra[0] * worm + rng.gauss(0, seeing),
            drift[1] * k * FRAME_S + TRUE.ra[1] * worm + rng.gauss(0, seeing),
        )
    return model.fit()


def test_a_known_worm_is_fitted_rather_than_taken_for_drift():
    """Over one worm turn a straight line through the swing still tilts,
    by however the window's start falls on it; fitted, it does not."""
    starts = [1.7e9 + 37.0 * n for n in range(12)]
    blind = [worm_track(PixelModel(TRUE, window_s=480), start=t).drift[0] * 60 for t in starts]
    fitted = [
        worm_track(PixelModel(TRUE, window_s=480, worm_period_s=WORM_PERIOD_S), start=t) for t in starts
    ]

    assert max(abs(d - 0.24) for d in blind) > 0.3
    assert max(abs(f.drift[0] * 60 - 0.24) for f in fitted) < 0.1
    assert statistics.mean(f.worm.amplitude for f in fitted) == pytest.approx(WORM_UNITS, rel=0.1)


def guided_with_worm(worm_period_s, *, seed=1, frames=500, measure=40):
    """The loop, cancelling what the model says the sky will do next."""
    rng = random.Random(seed)
    model = PixelModel(TRUE, window_s=480, worm_period_s=worm_period_s)
    x = y = 0.0
    held = []
    start = 1.7e9 + rng.uniform(0, WORM_PERIOD_S)
    for k in range(frames):
        t = start + k * FRAME_S
        worm = WORM_UNITS * math.sin(2 * math.pi * t / WORM_PERIOD_S)
        model.observe(
            t, x + TRUE.ra[0] * worm + rng.gauss(0, 0.25), y + TRUE.ra[1] * worm + rng.gauss(0, 0.25)
        )
        fit = model.fit()
        ra = dec = 0.0
        if fit is not None and k >= measure:
            ahead = fit.response.solve(*(-v for v in fit.motion(FRAME_S)))
            back = fit.response.solve(-fit.position[0], -fit.position[1])
            ra = ahead[0] + 0.7 * back[0]
            dec = float(round(ahead[1] + 0.6 * back[1]))
        x += 0.004 * FRAME_S + TRUE.ra[0] * ra + TRUE.dec[0] * dec
        y += -0.010 * FRAME_S + TRUE.ra[1] * ra + TRUE.dec[1] * dec
        model.sent(ra, dec)
        if k >= measure + 100:
            later = WORM_UNITS * math.sin(2 * math.pi * (t + FRAME_S) / WORM_PERIOD_S)
            held.append((x + TRUE.ra[0] * later, y + TRUE.ra[1] * later))
    return rms(held)


def test_cancelling_the_worm_as_it_comes_holds_the_star():
    fitted = statistics.mean(guided_with_worm(WORM_PERIOD_S, seed=seed) for seed in range(3))
    blind = statistics.mean(guided_with_worm(None, seed=seed) for seed in range(3))

    assert fitted < 0.2
    assert blind > 2 * fitted


def test_seeing_alone_does_not_move_the_position():
    """The last few frames count only when they sit off the fit by more
    than seeing would put them."""
    rng = random.Random(3)
    model = PixelModel(TRUE)
    for k in range(60):
        model.observe(k * FRAME_S, rng.gauss(0, 0.3), rng.gauss(0, 0.3))
    fit = model.fit()

    assert math.hypot(*fit.position) < 0.15


def test_a_sudden_move_shows_within_a_few_frames():
    """A snagged cable: two pixels at once, which a fit minutes long
    would take many frames to believe."""
    rng = random.Random(3)
    model = PixelModel(TRUE)
    for k in range(60):
        model.observe(k * FRAME_S, rng.gauss(0, 0.2), rng.gauss(0, 0.2))
    for k in range(60, 66):
        model.observe(k * FRAME_S, 2.0 + rng.gauss(0, 0.2), rng.gauss(0, 0.2))
    fit = model.fit()

    assert fit.position[0] > 1.2
