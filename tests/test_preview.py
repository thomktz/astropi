"""Display stretch for the frame preview."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from astropi.core.geometry import RaDec
from astropi.devices.backends.simulator.sky import OpticalTrain, render
from astropi.storage.frames import autostretch, to_png

OPTICS = OpticalTrain(focal_length_mm=400, pixel_size_um=3.76, width=600, height=400)
FIELD = RaDec(10.6847, 41.2690)


def _frame() -> np.ndarray:
    return render(FIELD, OPTICS, exposure_s=8.0, hfd_px=3.0)


def test_background_lands_near_the_target_not_mid_grey():
    """The failure this guards against renders the frame as static.

    Scaling between the background and a high percentile maps the sky noise
    itself across the whole output range. It looks like television snow,
    because on a real star field almost every pixel *is* background.
    """
    stretched = autostretch(_frame(), target_background=0.2)
    background = float(np.median(stretched)) / 255.0
    assert background == pytest.approx(0.2, abs=0.06)


def test_downsampling_averages_rather_than_strides():
    """Striding keeps every surviving pixel's full noise.

    That is what makes a reduced preview look like television static: the
    grain survives at full amplitude but is now one screen pixel across.
    Averaging blocks divides the noise by the reduction factor.
    """
    from astropi.storage.frames import _box_downsample

    rng = np.random.default_rng(0)
    noisy = rng.normal(1000, 50, (400, 400)).astype(np.uint16)

    strided = noisy[::4, ::4].astype(float)
    averaged = _box_downsample(noisy, 4)

    assert averaged.shape == (100, 100)
    # Four-by-four blocks: noise should fall by about a factor of four.
    assert averaged.std() < strided.std() / 3
    assert averaged.mean() == pytest.approx(strided.mean(), rel=0.02)


def test_stars_are_brighter_than_the_background():
    stretched = autostretch(_frame())
    assert stretched.max() > 200
    # Only a small fraction of a star field is star.
    assert (stretched > 160).mean() < 0.02


def test_stretch_is_monotonic():
    """Brighter in must stay brighter out, or the image is misleading."""
    ramp = np.linspace(0, 65535, 4096, dtype=np.uint16).reshape(64, 64)
    stretched = autostretch(ramp).flatten().astype(int)
    assert all(b >= a for a, b in pairwise(stretched))


def test_flat_frame_does_not_divide_by_zero():
    """A frame with no noise at all - a disconnected or capped sensor."""
    flat = np.full((64, 64), 500, dtype=np.uint16)
    stretched = autostretch(flat)
    assert stretched.shape == flat.shape
    assert np.isfinite(stretched).all()


def test_to_png_produces_a_png_and_downsamples():
    big = render(
        FIELD,
        OpticalTrain(focal_length_mm=400, pixel_size_um=3.76, width=3000, height=2000),
        exposure_s=4.0,
    )
    png = to_png(big, max_dimension=600)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # A 26-megapixel frame must not be sent whole to a phone over LAN Wi-Fi.
    assert len(png) < 2_000_000
