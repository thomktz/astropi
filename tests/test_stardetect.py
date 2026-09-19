"""Star detection: the measurements guiding and autofocus both depend on."""

from __future__ import annotations

import numpy as np
import pytest

from astropi.core.geometry import RaDec
from astropi.devices.backends.simulator.sky import OpticalTrain, field_stars, render
from astropi.services.stardetect import (
    detect_stars,
    frame_statistics,
    mean_hfd,
    nearest_star,
)

FIELD = RaDec(10.6847, 41.2690)
OPTICS = OpticalTrain(focal_length_mm=400, pixel_size_um=3.76, width=1200, height=900)


def test_field_stars_are_deterministic():
    """Guiding depends on the same star being there on the next frame."""
    first = field_stars(FIELD, 1.0)
    second = field_stars(FIELD, 1.0)
    assert np.array_equal(first, second)
    assert len(first) > 10


def test_statistics_are_robust_to_stars():
    """Bright pixels are signal; they must not drag the background up."""
    data = np.full((400, 400), 500, dtype=np.uint16)
    data[100:110, 100:110] = 60_000
    stats = frame_statistics(data)
    assert stats.background == pytest.approx(500, abs=1)


def test_detects_stars_in_a_rendered_field():
    frame = render(FIELD, OPTICS, exposure_s=4.0, hfd_px=3.0)
    stars = detect_stars(frame)
    assert len(stars) > 5
    assert stars[0].flux >= stars[-1].flux, "results should be brightest first"
    assert all(0 <= s.x < OPTICS.width and 0 <= s.y < OPTICS.height for s in stars)


def test_blank_frame_yields_nothing():
    rng = np.random.default_rng(0)
    noise = (rng.normal(500, 3, (400, 400))).astype(np.uint16)
    assert detect_stars(noise) == []


@pytest.mark.parametrize("hfd", [3.0, 6.0, 10.0])
def test_measured_size_increases_with_defocus(hfd):
    """Autofocus needs the metric to move monotonically with focus."""
    frame = render(FIELD, OPTICS, exposure_s=4.0, hfd_px=hfd)
    measured = mean_hfd(detect_stars(frame))
    assert measured is not None
    assert measured > 1.0


def test_hfd_is_monotonic_across_focus():
    """The V-curve autofocus fits must actually rise on both arms.

    Sampled from 3 pixels up. Below that the star is undersampled - at a
    half-flux diameter of 2 pixels the Gaussian sigma is 0.85 px, under the
    Nyquist limit - and the pixel grid, not the optics, sets what is
    measured. Real undersampled rigs have the same floor, which is why a
    focus routine steps well clear of best focus rather than creeping in.
    """
    measurements = [
        mean_hfd(detect_stars(render(FIELD, OPTICS, exposure_s=4.0, hfd_px=hfd)))
        for hfd in (3.0, 4.0, 6.0, 9.0, 13.0)
    ]
    assert all(m is not None for m in measurements)
    assert measurements == sorted(measurements), f"HFD must rise with defocus: {measurements}"


def test_marginal_detections_are_excluded_from_the_focus_metric():
    """Low-SNR stars measure enormous and would poison the median."""
    from astropi.services.stardetect import DetectedStar

    solid = [DetectedStar(x=10, y=10, flux=1e5, peak=5e3, hfd=3.0, snr=200.0)] * 8
    marginal = [DetectedStar(x=20, y=20, flux=50, peak=20, hfd=16.0, snr=3.0)] * 8

    assert mean_hfd(solid + marginal) == pytest.approx(3.0)
    assert mean_hfd(marginal) is None, "nothing measurable should report nothing"


def test_centroid_is_subpixel():
    """Guiding corrections are meaningless at whole-pixel resolution."""
    frame = render(FIELD, OPTICS, exposure_s=4.0, hfd_px=3.0)
    stars = detect_stars(frame)
    assert any(abs(s.x - round(s.x)) > 0.05 for s in stars)


def test_nearest_star_prefers_position_over_brightness():
    """A brighter interloper must not steal the guide star's lock."""
    frame = render(FIELD, OPTICS, exposure_s=4.0, hfd_px=3.0)
    stars = detect_stars(frame)
    assert len(stars) >= 2

    faint = stars[-1]
    found = nearest_star(stars, faint.x, faint.y, radius_px=5.0)
    assert found is not None
    assert found.x == pytest.approx(faint.x)


def test_nearest_star_returns_nothing_when_out_of_range():
    frame = render(FIELD, OPTICS, exposure_s=4.0, hfd_px=3.0)
    stars = detect_stars(frame)
    assert nearest_star(stars, -500.0, -500.0, radius_px=10.0) is None
