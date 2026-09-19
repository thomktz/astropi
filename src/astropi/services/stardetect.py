"""Star detection and shape measurement.

Feeds three different consumers from one implementation: guiding needs a
centroid to a fraction of a pixel, autofocus needs half-flux diameter, and
plate solving needs a list of the brightest sources. Written against numpy
alone - no scipy, no photutils - to keep the install small enough to be
comfortable on a Raspberry Pi.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class DetectedStar:
    x: float
    y: float
    flux: float
    peak: float
    hfd: float
    snr: float


@dataclass(frozen=True, slots=True)
class FrameStatistics:
    background: float
    noise: float
    saturated_fraction: float


def frame_statistics(data: np.ndarray, *, bit_depth: int = 16) -> FrameStatistics:
    """Robust background and noise estimates.

    Median and median-absolute-deviation rather than mean and standard
    deviation: on a star field the bright pixels are the signal, and a mean
    would let them drag the background estimate up with them.
    """
    sample = data[::4, ::4].astype(np.float32)
    background = float(np.median(sample))
    mad = float(np.median(np.abs(sample - background)))
    noise = max(mad * 1.4826, 1e-3)
    saturation = (2**bit_depth) - 1
    saturated = float((sample >= saturation * 0.98).mean())
    return FrameStatistics(background=background, noise=noise, saturated_fraction=saturated)


def detect_stars(
    data: np.ndarray,
    *,
    threshold_sigma: float = 6.0,
    max_stars: int = 200,
    min_separation_px: int = 8,
    aperture_px: int = 12,
    bit_depth: int = 16,
) -> list[DetectedStar]:
    """Find stars, brightest first."""
    stats = frame_statistics(data, bit_depth=bit_depth)
    image = data.astype(np.float32) - stats.background
    cutoff = threshold_sigma * stats.noise

    candidates = np.argwhere(image > cutoff)
    if not len(candidates):
        return []

    # Sort by brightness and walk down the list, rejecting anything too
    # close to a star already accepted. That collapses the many pixels of
    # one star into a single detection without a connected-components pass.
    values = image[candidates[:, 0], candidates[:, 1]]
    order = np.argsort(values)[::-1]
    candidates = candidates[order]

    height, width = image.shape
    claimed: list[tuple[int, int]] = []
    stars: list[DetectedStar] = []
    min_sep_sq = min_separation_px * min_separation_px

    for row, col in candidates:
        if len(stars) >= max_stars:
            break
        if any((row - r) ** 2 + (col - c) ** 2 < min_sep_sq for r, c in claimed):
            continue
        # Skip sources that touch the edge: their centroid and flux are
        # both truncated, which for guiding means a star that appears to
        # move as it leaves the frame.
        if not (aperture_px <= col < width - aperture_px and aperture_px <= row < height - aperture_px):
            continue

        star = _measure(image, int(row), int(col), aperture_px, stats.noise)
        if star is None:
            continue
        claimed.append((int(row), int(col)))
        stars.append(star)

    stars.sort(key=lambda s: s.flux, reverse=True)
    return stars


def _measure(
    image: np.ndarray, row: int, col: int, aperture: int, noise: float
) -> DetectedStar | None:
    """Centroid, flux and half-flux diameter inside one aperture."""
    patch = image[row - aperture : row + aperture + 1, col - aperture : col + aperture + 1]
    # Only pixels clearly above the noise contribute. Clipping the raw patch
    # at zero instead would turn symmetric noise into a positive pedestal
    # spread over every pixel of the aperture, which swamps a faint star's
    # real flux and pushes its measured half-flux radius far too wide.
    positive = np.where(patch >= 2.0 * noise, patch, 0.0)
    flux = float(positive.sum())
    if flux <= 0:
        return None

    size = positive.shape[0]
    axis = np.arange(size) - aperture
    # Flux-weighted first moment: the sub-pixel centre guiding needs.
    weight_x = positive.sum(axis=0)
    weight_y = positive.sum(axis=1)
    dx = float((axis * weight_x).sum() / flux)
    dy = float((axis * weight_y).sum() / flux)

    distance = np.hypot((axis - dx)[None, :], (axis - dy)[:, None])
    hfd = _half_flux_diameter(positive, distance, flux)

    peak = float(patch[aperture, aperture])
    # Noise scales with the square root of the number of pixels that
    # actually contributed, not the whole aperture - counting empty sky
    # would make every star look worse the larger the aperture was set.
    contributing = max(int((positive > 0).sum()), 1)
    snr = flux / (noise * math.sqrt(contributing))
    return DetectedStar(x=col + dx, y=row + dy, flux=flux, peak=peak, hfd=hfd, snr=snr)


def _half_flux_diameter(patch: np.ndarray, distance: np.ndarray, flux: float) -> float:
    """Diameter of the circle containing half the star's flux.

    HFD is the standard focus metric because, unlike full-width-half-maximum,
    it stays meaningful for a badly defocused doughnut with no single peak.
    """
    order = np.argsort(distance, axis=None)
    cumulative = np.cumsum(patch.flatten()[order])
    half = flux / 2.0
    index = int(np.searchsorted(cumulative, half))
    if index >= len(order):
        return float(distance.flatten()[order][-1] * 2.0)
    return float(distance.flatten()[order][index] * 2.0)


def brightest_star(stars: list[DetectedStar]) -> DetectedStar | None:
    return stars[0] if stars else None


def nearest_star(stars: list[DetectedStar], x: float, y: float, *, radius_px: float) -> DetectedStar | None:
    """The star closest to a position, if one is within `radius_px`.

    This is how the guide loop re-acquires its star on each frame: it looks
    where the star was, not for the brightest thing in the field, so a
    passing satellite or a brighter neighbour cannot steal the lock.
    """
    best: DetectedStar | None = None
    best_distance = radius_px
    for star in stars:
        distance = math.hypot(star.x - x, star.y - y)
        if distance <= best_distance:
            best, best_distance = star, distance
    return best


def mean_hfd(
    stars: list[DetectedStar], *, sample: int = 20, min_snr: float = 15.0
) -> float | None:
    """Median HFD over well-detected stars - the autofocus measurement.

    Median over several stars rather than one, because a single star's
    measured size wanders with seeing and a focus routine that trusts one
    star produces a noisy V-curve.

    The SNR floor matters as much as the median. In a marginal detection,
    scattered noise pixels pass the aperture threshold and land far from the
    centroid, which pushes the half-flux radius out to several times the
    true value. Those stars are the faint tail of any frame, so without this
    filter they make up enough of the sample to drag the median around and
    destroy the monotonicity that autofocus depends on.
    """
    usable = [s.hfd for s in stars[:sample] if s.hfd > 0 and s.snr >= min_snr]
    if not usable:
        # Nothing measured confidently: better to report no measurement than
        # a number the focus routine would fit a curve through.
        return None
    return float(np.median(usable))
