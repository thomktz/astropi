"""Equatorial mount geometry.

A mount has two mechanical axes and rotates about *its own* polar axis,
which is never quite the celestial pole. Almost everything this software
exists to correct follows from that one fact: GoTos miss, tracking drifts,
and the field rotates.

This is shared domain knowledge, not a simulator detail. The simulator uses
it in the forward direction, to produce those errors; polar alignment uses
it in reverse, to recover the rotation axis from three plate solves. Having
both read from one model is what makes the simulator a real test of the
alignment maths rather than a constant planted for it to find.

Frame convention: a right-handed horizontal frame with x toward north,
y toward west and z toward the zenith.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from astropi.core.geometry import RaDec, normalize_deg

Vec3 = tuple[float, float, float]


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def normalize(v: Vec3) -> Vec3:
    norm = math.sqrt(dot(v, v))
    if norm == 0.0:
        raise ValueError("cannot normalize a zero vector")
    return (v[0] / norm, v[1] / norm, v[2] / norm)


def horizontal_vector(alt_deg: float, az_deg: float) -> Vec3:
    """Unit vector from altitude and azimuth measured east of north."""
    alt = math.radians(alt_deg)
    az = math.radians(az_deg)
    return (math.cos(alt) * math.cos(az), -math.cos(alt) * math.sin(az), math.sin(alt))


@dataclass(frozen=True, slots=True)
class PolarAxis:
    """A rotation axis and the basis used to express pointings around it.

    `e1` points along the half-meridian above the axis (hour angle zero) and
    `e2` completes the right-handed set, so hour angle increases westward as
    the sky turns.
    """

    axis: Vec3
    e1: Vec3
    e2: Vec3

    @classmethod
    def from_alt_az(cls, alt_deg: float, az_deg: float) -> PolarAxis:
        axis = horizontal_vector(alt_deg, az_deg)
        zenith: Vec3 = (0.0, 0.0, 1.0)
        # Component of "up" perpendicular to the axis: the meridian above it.
        projection = dot(zenith, axis)
        residual = (
            zenith[0] - projection * axis[0],
            zenith[1] - projection * axis[1],
            zenith[2] - projection * axis[2],
        )
        if math.sqrt(dot(residual, residual)) < 1e-9:
            # Axis is straight up: the observer is at a geographic pole and
            # every direction is equally a meridian. Pick one arbitrarily.
            residual = (1.0, 0.0, 0.0)
        e1 = normalize(residual)
        e2 = cross(axis, e1)
        return cls(axis=axis, e1=e1, e2=e2)

    def pointing(self, hour_angle_deg: float, dec_deg: float) -> Vec3:
        """Where the telescope looks at these mechanical axis angles."""
        ha = math.radians(hour_angle_deg)
        dec = math.radians(dec_deg)
        cos_dec, sin_dec = math.cos(dec), math.sin(dec)
        cos_ha, sin_ha = math.cos(ha), math.sin(ha)
        return (
            sin_dec * self.axis[0] + cos_dec * (cos_ha * self.e1[0] - sin_ha * self.e2[0]),
            sin_dec * self.axis[1] + cos_dec * (cos_ha * self.e1[1] - sin_ha * self.e2[1]),
            sin_dec * self.axis[2] + cos_dec * (cos_ha * self.e1[2] - sin_ha * self.e2[2]),
        )

    def axis_angles(self, direction: Vec3) -> tuple[float, float]:
        """Inverse of `pointing`: the (hour angle, dec) that look this way."""
        sin_dec = max(-1.0, min(1.0, dot(direction, self.axis)))
        dec = math.degrees(math.asin(sin_dec))
        ha = math.degrees(math.atan2(-dot(direction, self.e2), dot(direction, self.e1)))
        return normalize_deg(ha + 180.0) - 180.0, dec


def true_pole(latitude_deg: float) -> PolarAxis:
    """The celestial pole basis: what a perfectly aligned mount rotates about."""
    return PolarAxis.from_alt_az(abs(latitude_deg), 0.0 if latitude_deg >= 0 else 180.0)


def misaligned_pole(latitude_deg: float, alt_error_deg: float, az_error_deg: float) -> PolarAxis:
    """The mount's actual rotation axis, offset from the true pole.

    Errors follow the convention an observer uses at the adjustment knobs:
    positive altitude means the polar axis sits too high, positive azimuth
    means it is rotated east of the pole.
    """
    if latitude_deg >= 0:
        return PolarAxis.from_alt_az(abs(latitude_deg) + alt_error_deg, az_error_deg)
    return PolarAxis.from_alt_az(abs(latitude_deg) + alt_error_deg, 180.0 - az_error_deg)


def radec_to_vector(coord: RaDec, lst_deg: float, pole: PolarAxis) -> Vec3:
    """Place an equatorial coordinate in the horizontal frame."""
    return pole.pointing(lst_deg - coord.ra_deg, coord.dec_deg)


def vector_to_radec(direction: Vec3, lst_deg: float, pole: PolarAxis) -> RaDec:
    """Read an equatorial coordinate back out of a horizontal direction."""
    hour_angle, dec = pole.axis_angles(direction)
    return RaDec(normalize_deg(lst_deg - hour_angle), dec)


def vector_to_alt_az(direction: Vec3) -> tuple[float, float]:
    """Altitude and azimuth (east of north) of a horizontal-frame vector."""
    x, y, z = normalize(direction)
    alt = math.degrees(math.asin(max(-1.0, min(1.0, z))))
    az = normalize_deg(math.degrees(math.atan2(-y, x)))
    return alt, az
