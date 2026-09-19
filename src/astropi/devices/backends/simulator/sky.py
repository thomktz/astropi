"""Synthetic star field rendering.

Produces raw sensor frames for a given pointing so that plate solving, star
detection, guiding and autofocus all run against real pixels locally. A
simulator that returned a blank frame, or a position with no image behind
it, would exercise none of the code that matters.

Two sources of stars are combined:

* the real catalogue, so a frame centred on M31 or the Double Cluster looks
  like the thing it claims to be and a plate solve has genuine anchors;
* procedurally generated field stars, because the catalogue holds about a
  thousand objects and any given 3-degree field would otherwise be empty.
  These are seeded from the sky cell they fall in, so the *same* field
  always renders the *same* stars - guiding depends on a star still being
  there on the next frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from astropi.core.geometry import RaDec, normalize_deg

#: Sky is diced into cells this many degrees across for procedural stars.
CELL_SIZE_DEG = 1.0
#: Field stars per square degree down to the faint limit below. Roughly a
#: mid-galactic-latitude count to magnitude 15 - dense enough that a
#: modest sub-frame still has stars to solve on and to guide with.
FIELD_STARS_PER_SQ_DEG = 420.0
#: Electrons per second from a magnitude-zero star.
#:
#: Order-of-magnitude for a 71 mm aperture (400 mm at f/5.6) with a
#: typical CMOS quantum efficiency: about 4e7 photons per second
#: reaching the sensor, most of which are detected. Getting this roughly
#: right is what makes a simulated sub-exposure show the same stars a
#: real one would, so detection thresholds tuned here still hold on the
#: actual rig.
FLUX_ZERO_POINT_E_PER_S = 2.5e7


@dataclass(frozen=True, slots=True)
class OpticalTrain:
    """Everything needed to turn sky coordinates into pixels."""

    focal_length_mm: float
    pixel_size_um: float
    width: int
    height: int
    rotation_deg: float = 0.0

    @property
    def pixel_scale_arcsec(self) -> float:
        """Arcseconds per pixel, the small-angle approximation."""
        return 206.264806 * self.pixel_size_um / self.focal_length_mm

    @property
    def field_width_deg(self) -> float:
        return self.width * self.pixel_scale_arcsec / 3600.0

    @property
    def field_height_deg(self) -> float:
        return self.height * self.pixel_scale_arcsec / 3600.0

    @property
    def field_radius_deg(self) -> float:
        return math.hypot(self.field_width_deg, self.field_height_deg) / 2.0


@dataclass(frozen=True, slots=True)
class Star:
    ra_deg: float
    dec_deg: float
    magnitude: float


def project(stars: np.ndarray, center: RaDec, optics: OpticalTrain) -> tuple[np.ndarray, np.ndarray]:
    """Gnomonic (tangent-plane) projection of RA/Dec onto pixel coordinates.

    Gnomonic because that is what a telescope actually does to first order,
    and what every plate solver assumes - a linear approximation would put
    stars in visibly wrong places at the corners of a wide field.

    `stars` is an (N, 2) array of RA and Dec in degrees.
    """
    ra = np.radians(stars[:, 0])
    dec = np.radians(stars[:, 1])
    ra0 = math.radians(center.ra_deg)
    dec0 = math.radians(center.dec_deg)

    cos_dec, sin_dec = np.cos(dec), np.sin(dec)
    cos_dec0, sin_dec0 = math.cos(dec0), math.sin(dec0)
    cos_dra = np.cos(ra - ra0)
    sin_dra = np.sin(ra - ra0)

    # Cosine of angular distance from the tangent point.
    denominator = sin_dec0 * sin_dec + cos_dec0 * cos_dec * cos_dra
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = cos_dec * sin_dra / denominator
        eta = (cos_dec0 * sin_dec - sin_dec0 * cos_dec * cos_dra) / denominator
    # Stars more than 90 degrees away project onto the far side of the
    # tangent plane; push them out of frame instead of mirroring them in.
    behind = denominator <= 0
    xi = np.where(behind, np.nan, xi)
    eta = np.where(behind, np.nan, eta)

    # Tangent-plane units are radians; convert to pixels.
    scale = 1.0 / math.radians(optics.pixel_scale_arcsec / 3600.0)

    theta = math.radians(optics.rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    # RA increases eastward, which is to the left in a normal sky image.
    x_frame = -xi * scale
    y_frame = eta * scale

    x = x_frame * cos_t - y_frame * sin_t + optics.width / 2.0
    y = x_frame * sin_t + y_frame * cos_t + optics.height / 2.0
    return x, y


def _cell_stars(ra_cell: int, dec_cell: int) -> np.ndarray:
    """Deterministic field stars for one sky cell.

    Seeded from the cell index so repeated visits to a field return an
    identical star pattern - without that, a guide star would vanish
    between consecutive frames and the loop could never lock on.
    """
    seed = (ra_cell * 73_856_093) ^ (dec_cell * 19_349_663)
    rng = np.random.default_rng(seed & 0x7FFF_FFFF)

    dec_low = dec_cell * CELL_SIZE_DEG
    dec_high = dec_low + CELL_SIZE_DEG
    # Cells shrink in RA extent toward the poles, so their area - and the
    # star count that follows from it - stays honest.
    mean_dec = math.radians((dec_low + dec_high) / 2.0)
    area = CELL_SIZE_DEG * CELL_SIZE_DEG * max(math.cos(mean_dec), 1e-3)
    count = rng.poisson(FIELD_STARS_PER_SQ_DEG * area)
    if count == 0:
        return np.empty((0, 3))

    ra_width = CELL_SIZE_DEG / max(math.cos(mean_dec), 1e-3)
    ra = ra_cell * ra_width + rng.random(count) * ra_width
    dec = dec_low + rng.random(count) * CELL_SIZE_DEG
    # Faint stars vastly outnumber bright ones; an exponential in magnitude
    # reproduces that without pretending to a real luminosity function.
    magnitude = 15.0 - rng.exponential(2.4, count)
    return np.column_stack([np.mod(ra, 360.0), dec, np.clip(magnitude, 4.0, 16.0)])


def field_stars(center: RaDec, radius_deg: float) -> np.ndarray:
    """All procedural stars within `radius_deg` of a pointing."""
    dec_low = max(-90.0, center.dec_deg - radius_deg)
    dec_high = min(90.0, center.dec_deg + radius_deg)
    dec_cells = range(math.floor(dec_low / CELL_SIZE_DEG), math.floor(dec_high / CELL_SIZE_DEG) + 1)

    cos_dec = max(math.cos(math.radians(center.dec_deg)), 1e-3)
    ra_span = min(radius_deg / cos_dec, 180.0)

    chunks: list[np.ndarray] = []
    for dec_cell in dec_cells:
        mean_dec = math.radians((dec_cell + 0.5) * CELL_SIZE_DEG)
        ra_width = CELL_SIZE_DEG / max(math.cos(mean_dec), 1e-3)
        low = math.floor((center.ra_deg - ra_span) / ra_width)
        high = math.floor((center.ra_deg + ra_span) / ra_width)
        for ra_cell in range(low, high + 1):
            chunk = _cell_stars(ra_cell, dec_cell)
            if len(chunk):
                chunks.append(chunk)

    if not chunks:
        return np.empty((0, 3))
    return np.vstack(chunks)


def render(
    center: RaDec,
    optics: OpticalTrain,
    *,
    exposure_s: float,
    hfd_px: float = 3.0,
    gain: int = 100,
    bit_depth: int = 16,
    sky_brightness_e_per_s: float = 12.0,
    read_noise_e: float = 1.5,
    catalog: np.ndarray | None = None,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Render one frame of raw sensor counts.

    Stars are laid down as Gaussians whose width follows `hfd_px`, so an
    autofocus routine measuring half-flux diameter sees it change as the
    focuser moves. Noise is Poisson on the accumulated signal plus Gaussian
    read noise, which is what star detection has to work through.
    """
    rng = rng or np.random.default_rng()
    center = center.normalized()

    stars = field_stars(center, optics.field_radius_deg * 1.2)
    if catalog is not None and len(catalog):
        stars = np.vstack([stars, catalog]) if len(stars) else catalog

    # Sky background first: everything else is added on top of it.
    signal = np.full(
        (optics.height, optics.width),
        sky_brightness_e_per_s * exposure_s,
        dtype=np.float32,
    )

    if len(stars):
        _paint_stars(signal, stars, center, optics, exposure_s=exposure_s, hfd_px=hfd_px)

    # Shot noise, then read noise, then the gain and quantisation the ADC
    # applies. Order matters: read noise is not amplified by exposure time.
    noisy = rng.poisson(np.clip(signal, 0, None)).astype(np.float32)
    noisy += rng.normal(0.0, read_noise_e, size=noisy.shape).astype(np.float32)

    electrons_per_adu = max(0.05, 5.0 * math.pow(10.0, -gain / 200.0))
    counts = noisy / electrons_per_adu + 500.0  # 500 ADU pedestal, as cameras ship
    return np.clip(counts, 0, 2**bit_depth - 1).astype(np.uint16)


def _paint_stars(
    signal: np.ndarray,
    stars: np.ndarray,
    center: RaDec,
    optics: OpticalTrain,
    *,
    exposure_s: float,
    hfd_px: float,
) -> None:
    """Add each star's Gaussian profile into the frame.

    Drawn as small stamps rather than evaluating the Gaussian across the
    whole sensor: a full-frame ASI2600 is 26 megapixels and there may be a
    thousand stars, so per-star full-array maths would be minutes per frame.
    """
    x, y = project(stars[:, :2], center, optics)
    magnitude = stars[:, 2]

    # Half-flux diameter to Gaussian sigma for a 2-D Gaussian profile.
    sigma = max(hfd_px / 2.3548, 0.6)
    radius = max(2, math.ceil(sigma * 4))

    visible = (
        np.isfinite(x)
        & np.isfinite(y)
        & (x > -radius)
        & (x < optics.width + radius)
        & (y > -radius)
        & (y < optics.height + radius)
    )
    x, y, magnitude = x[visible], y[visible], magnitude[visible]
    if not len(x):
        return

    total_flux = FLUX_ZERO_POINT_E_PER_S * np.power(10.0, -0.4 * magnitude) * exposure_s

    offsets = np.arange(-radius, radius + 1)
    height, width = signal.shape

    for star_x, star_y, flux in zip(x, y, total_flux, strict=True):
        col0 = round(star_x) - radius
        row0 = round(star_y) - radius
        cols = col0 + offsets
        rows = row0 + offsets
        col_mask = (cols >= 0) & (cols < width)
        row_mask = (rows >= 0) & (rows < height)
        if not col_mask.any() or not row_mask.any():
            continue

        # Separable Gaussian: the outer product of two 1-D profiles.
        gx = np.exp(-0.5 * ((cols[col_mask] - star_x) / sigma) ** 2)
        gy = np.exp(-0.5 * ((rows[row_mask] - star_y) / sigma) ** 2)
        stamp = np.outer(gy, gx)
        stamp_sum = stamp.sum()
        if stamp_sum <= 0:
            continue
        signal[np.ix_(rows[row_mask], cols[col_mask])] += (flux * stamp / stamp_sum).astype(np.float32)


def catalog_array(entries: list[tuple[float, float, float]]) -> np.ndarray:
    """Pack (ra, dec, magnitude) tuples into the array shape `render` wants."""
    if not entries:
        return np.empty((0, 3))
    array = np.asarray(entries, dtype=float)
    array[:, 0] = np.mod(array[:, 0], 360.0)
    return array


def visible_catalog(catalog: np.ndarray, center: RaDec, radius_deg: float) -> np.ndarray:
    """Cheap angular cut so only nearby catalogue rows reach the projector."""
    if not len(catalog):
        return catalog
    ra = np.radians(catalog[:, 0])
    dec = np.radians(catalog[:, 1])
    ra0 = math.radians(normalize_deg(center.ra_deg))
    dec0 = math.radians(center.dec_deg)
    cos_sep = np.sin(dec0) * np.sin(dec) + np.cos(dec0) * np.cos(dec) * np.cos(ra - ra0)
    return catalog[cos_sep >= math.cos(math.radians(min(radius_deg, 179.0)))]
