"""Polar alignment from two or more frames and whatever moves you like between them.

The three-point method in `polaralign` slews itself around a fixed sweep,
which is no use when the sky is a window: the operator knows where the
frame has room and where it hits the curtain, and the mount does not. So
here the operator takes one frame, moves the mount by hand to wherever
the window still has stars, and takes another.

Two frames are enough because the mount says how far it moved. Each solve
gives a direction on the sky (two numbers); the mount's own axis readings
give the hour angle and declination it was standing at. What is unknown
is the polar axis (altitude and azimuth error) and where the mount's
axis readings are zeroed relative to the sky (an hour angle and a
declination offset - the mount has never been synced, and the camera may
not sit square on the dovetail). Four unknowns, four observables.

A move in right ascension alone is the well-posed case, and the one to
recommend: the two pointings then lie on a circle about the axis, and a
camera mounted off-square merely changes that circle's size - it is
absorbed into the declination offset exactly. A move in declination is
allowed, but there the same mounting error leaks into the answer.

After the fit the offsets are known, so every further frame gives the
axis directly - which is what makes the live view at the knobs possible
with the mount standing wherever the last frame left it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from astropi.core.errors import AstropiError
from astropi.core.geometry import RaDec, wrap_symmetric_deg
from astropi.core.pointing import (
    PolarAxis,
    Vec3,
    misaligned_pole,
    radec_to_vector,
    true_pole,
    vector_to_radec,
)
from astropi.core.site import ObservingSite
from astropi.services.polaralign import PolarAlignmentError, error_from_axis, solve_axis_from_pointing

#: Below this much hour angle between the frames the circle is too short an
#: arc to say where its centre is.
MIN_ROTATION_DEG = 5.0
#: Below this the answer is fine; above it the user is told to move further.
RECOMMENDED_ROTATION_DEG = 20.0
#: Assumed scatter of one plate solve, for the uncertainty estimate.
SOLVE_NOISE_ARCSEC = 5.0


@dataclass(frozen=True, slots=True)
class PolarFrame:
    """One solved frame, with where the mount thought it was at the time."""

    solved: RaDec
    #: The mount's own reading - uncorrected, unsynced, whatever it says.
    mount: RaDec
    lst_deg: float
    pier_side: str = "unknown"

    @property
    def mechanical(self) -> tuple[float, float]:
        """Hour angle and declination as the mount's axes read them."""
        return wrap_symmetric_deg(self.lst_deg - self.mount.ra_deg), self.mount.dec_deg


@dataclass(frozen=True, slots=True)
class PolarFit:
    """The polar axis, plus the offsets that tie the mount's axes to the sky."""

    error: PolarAlignmentError
    altitude_error_deg: float
    azimuth_error_deg: float
    ha_offset_deg: float
    dec_offset_deg: float
    #: How far the RA axis turned between the frames.
    rotation_deg: float
    #: How far declination moved between them; ideally nothing.
    dec_change_deg: float
    #: One-sigma uncertainty of the total error, from solve noise alone.
    uncertainty_arcmin: float
    frames: int = 2
    #: RMS distance of the solves from the fit; nil for exactly two.
    residual_arcsec: float = 0.0

    @property
    def warnings(self) -> list[str]:
        notes: list[str] = []
        if abs(self.rotation_deg) < RECOMMENDED_ROTATION_DEG:
            notes.append(
                f"The frames span only {abs(self.rotation_deg):.0f}° of RA - "
                f"{RECOMMENDED_ROTATION_DEG:.0f}° or more gives a steadier answer."
            )
        if abs(self.dec_change_deg) > 0.05:
            notes.append(
                "Declination moved between the frames, so a camera that is not square to the "
                "axis shows up as alignment error. A move in RA alone avoids that."
            )
        if self.frames > 2 and self.residual_arcsec > 60:
            notes.append(
                f"The frames disagree by {self.residual_arcsec / 60:.1f}' - something other than the "
                "mount's axes moved between them. Start over if the tripod was bumped."
            )
        return notes


def _axis(alt_error_deg: float, az_error_deg: float, site: ObservingSite) -> PolarAxis:
    return misaligned_pole(site.latitude_deg, alt_error_deg, az_error_deg)


def _predict(params: np.ndarray, frame: PolarFrame, site: ObservingSite) -> Vec3:
    alt, az, ha0, dec0 = params
    ha, dec = frame.mechanical
    return _axis(alt, az, site).pointing(ha + ha0, dec + dec0)


def _observed(frame: PolarFrame, site: ObservingSite) -> Vec3:
    return radec_to_vector(frame.solved, frame.lst_deg, true_pole(site.latitude_deg))


def _residuals(params: np.ndarray, frames: list[PolarFrame], site: ObservingSite) -> np.ndarray:
    values: list[float] = []
    for frame in frames:
        predicted = _predict(params, frame, site)
        observed = _observed(frame, site)
        values.extend(p - o for p, o in zip(predicted, observed, strict=True))
    return np.array(values)


def _jacobian(params: np.ndarray, frames: list[PolarFrame], site: ObservingSite) -> np.ndarray:
    step = 1e-5
    base = _residuals(params, frames, site)
    columns = []
    for index in range(len(params)):
        shifted = params.copy()
        shifted[index] += step
        columns.append((_residuals(shifted, frames, site) - base) / step)
    return np.stack(columns, axis=1)


def fit_frames(frames: list[PolarFrame], site: ObservingSite) -> PolarFit:
    """Solve for the polar axis from two or more frames.

    Each frame is wherever the operator put the mount; nothing about the
    moves between them is assumed beyond what the mount's own readings
    say. Two frames determine the answer exactly; a third or more
    over-determine it, which averages solve noise down and gives a
    residual that says whether the frames agree.

    Gauss-Newton from a perfectly aligned axis, which is never far wrong -
    nobody's polar axis is out by tens of degrees - and which also picks
    the right one of the two axes that fit an RA-only move (the other is
    its mirror image, nowhere near the pole).
    """
    if len(frames) < 2:
        raise AstropiError("take at least two frames, with a move in RA between them")
    sides = {frame.pier_side for frame in frames} - {"unknown"}
    if len(sides) > 1:
        raise AstropiError("the mount crossed the meridian between frames - keep them all on one side")

    first = frames[0]
    turns = [wrap_symmetric_deg(frame.mechanical[0] - first.mechanical[0]) for frame in frames]
    rotation = max(turns) - min(turns)
    if rotation < MIN_ROTATION_DEG:
        raise AstropiError(
            f"the frames span only {rotation:.1f}° of RA - "
            f"move at least {MIN_ROTATION_DEG:.0f}° east or west, more if the window allows"
        )
    declinations = [frame.mechanical[1] for frame in frames]

    # Offsets from the first frame, as if the axis were on the pole.
    observed_first = true_pole(site.latitude_deg).axis_angles(_observed(first, site))
    ha, dec = first.mechanical
    params = np.array(
        [0.0, 0.0, wrap_symmetric_deg(observed_first[0] - ha), observed_first[1] - dec],
        dtype=float,
    )
    for _ in range(30):
        residual = _residuals(params, frames, site)
        jacobian = _jacobian(params, frames, site)
        delta, *_ = np.linalg.lstsq(jacobian, -residual, rcond=None)
        params = params + delta
        params[2] = wrap_symmetric_deg(params[2])
        if np.max(np.abs(delta)) < 1e-9:
            break
    alt, az, ha0, dec0 = (float(value) for value in params)
    if abs(alt) > 20 or abs(az) > 30:
        raise AstropiError(
            "the frames fit no plausible polar axis - check the mount reported its position "
            "and that only the mount moved between the frames"
        )

    # How far each solve sits from where the fitted axis says it should.
    # Nil with two frames, which fit exactly; with more, a large figure
    # means a frame was disturbed - a bumped tripod, a slipped clutch.
    leftover = _residuals(params, frames, site).reshape(-1, 3)
    residual_arcsec = math.degrees(float(np.sqrt(np.mean(np.sum(leftover**2, axis=1))))) * 3600.0

    jacobian = _jacobian(params, frames, site)
    uncertainty = _uncertainty_arcmin(jacobian, alt, az, site)
    axis = _axis(alt, az, site).axis
    return PolarFit(
        error=error_from_axis(axis, site),
        altitude_error_deg=alt,
        azimuth_error_deg=az,
        ha_offset_deg=ha0,
        dec_offset_deg=dec0,
        rotation_deg=rotation,
        dec_change_deg=max(declinations) - min(declinations),
        uncertainty_arcmin=uncertainty,
        frames=len(frames),
        residual_arcsec=residual_arcsec,
    )


def _uncertainty_arcmin(jacobian: np.ndarray, alt: float, az: float, site: ObservingSite) -> float:
    """Total-error scatter that solve noise alone would produce.

    The residual is in unit-vector components, so a solve error of sigma
    radians is sigma in each. Large when the frames are close together or
    the move was too short to define the circle.
    """
    sigma = math.radians(SOLVE_NOISE_ARCSEC / 3600.0)
    try:
        covariance = np.linalg.pinv(jacobian.T @ jacobian) * sigma**2
    except np.linalg.LinAlgError:
        return math.inf
    altitude_var = covariance[0, 0]
    # The azimuth figure is a bearing; on the sky it is foreshortened.
    foreshortening = math.cos(math.radians(abs(site.latitude_deg) + alt))
    azimuth_var = covariance[1, 1] * foreshortening**2
    return math.sqrt(max(altitude_var + azimuth_var, 0.0)) * 60.0


@dataclass(frozen=True, slots=True)
class Correction:
    """Where the field has to travel for the axis to reach the pole.

    Each point is a sky position; the caller draws them on the frame. The
    altitude knob takes the field from `now` to `after_altitude`, the
    azimuth knob from there to `aligned`.
    """

    error: PolarAlignmentError
    now: RaDec
    after_altitude: RaDec
    aligned: RaDec


def live_error(
    fit: PolarFit,
    frame: PolarFrame,
    site: ObservingSite,
    *,
    previous: tuple[float, float] | None = None,
) -> tuple[PolarAlignmentError, tuple[float, float]]:
    """The axis now, from one more frame, while the knobs are being turned.

    Uses the mount's current reading rather than assuming it stood still,
    so a nudge to dodge the window frame after the fit does not throw the
    answer off.
    """
    ha, dec = frame.mechanical
    angles = (ha + fit.ha_offset_deg, dec + fit.dec_offset_deg)
    start = previous or (fit.altitude_error_deg, fit.azimuth_error_deg)
    axis = solve_axis_from_pointing(frame.solved, frame.lst_deg, angles, site, initial=start)
    error = error_from_axis(axis, site)
    # `error_from_axis` reports azimuth in the convention `misaligned_pole`
    # takes, so the pair goes straight back in as the next starting point.
    return error, (error.altitude_error_deg, error.azimuth_error_deg)


def correction(
    fit: PolarFit,
    frame: PolarFrame,
    knobs: tuple[float, float],
    site: ObservingSite,
) -> Correction:
    """Where this frame's centre would sit after each knob is corrected.

    Turning a knob rotates the whole mount, telescope and all, so the
    field moves with the axis - exactly by the rotation that carries the
    axis onto the pole. Evaluated at the mount's current axis angles, it
    gives the sky position the frame centre must reach.
    """
    alt, az = knobs
    ha, dec = frame.mechanical
    angles = (ha + fit.ha_offset_deg, dec + fit.dec_offset_deg)
    pole = true_pole(site.latitude_deg)

    def sky(alt_error: float, az_error: float) -> RaDec:
        return vector_to_radec(_axis(alt_error, az_error, site).pointing(*angles), frame.lst_deg, pole)

    error = error_from_axis(_axis(alt, az, site).axis, site)
    return Correction(error=error, now=sky(alt, az), after_altitude=sky(0.0, az), aligned=sky(0.0, 0.0))
