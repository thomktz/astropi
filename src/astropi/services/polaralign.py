"""Polar alignment by plate solving.

Three-point method, the same geometry SharpCap and NINA use, and the reason
no polar scope or view of Polaris is needed.

The idea: rotate *only* the right ascension axis between exposures. Because
the telescope is then sweeping a cone about the mount's true rotation axis,
the three solved positions must lie on a circle centred on that axis - so
the normal of the plane through them *is* the axis. Comparing it with the
celestial pole gives the altitude and azimuth error directly, in the units
of the two adjustment knobs.

After the measurement, `refine` keeps solving while the knobs are turned and
reports the remaining error live, which is what makes the adjustment
convergent rather than guesswork.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

from astropi.core.errors import AstropiError
from astropi.core.events import EventBus, Topic
from astropi.core.geometry import RaDec, normalize_deg, wrap_symmetric_deg
from astropi.core.pointing import (
    PolarAxis,
    Vec3,
    cross,
    dot,
    normalize,
    radec_to_vector,
    true_pole,
    vector_to_alt_az,
)
from astropi.core.site import ObservingSite
from astropi.core.timekeeping import local_sidereal_time_deg

logger = logging.getLogger(__name__)

#: Hour angle step between the three measurement points.
DEFAULT_SEPARATION_DEG = 25.0
#: Declination to measure at. Well off the pole, where the cone is wide
#: enough that solve noise barely rotates the fitted plane.
DEFAULT_DECLINATION_DEG = 30.0


@dataclass(frozen=True, slots=True)
class PolarMeasurement:
    """One solved point of the sweep."""

    solved: RaDec
    lst_deg: float
    timestamp: float


@dataclass(frozen=True, slots=True)
class PolarAlignmentError:
    """How far the mount's rotation axis sits from the celestial pole."""

    altitude_error_deg: float
    azimuth_error_deg: float
    total_error_deg: float
    axis_altitude_deg: float
    axis_azimuth_deg: float

    @property
    def altitude_error_arcmin(self) -> float:
        return self.altitude_error_deg * 60.0

    @property
    def azimuth_error_arcmin(self) -> float:
        """Bearing error at the azimuth knob, in arcminutes."""
        return self.azimuth_error_deg * 60.0

    @property
    def azimuth_error_on_sky_arcmin(self) -> float:
        """The same error as an angle on sky, foreshortened by axis altitude.

        This is the figure that belongs in a total error budget; the knob
        needs `azimuth_error_arcmin` instead.
        """
        return self.azimuth_error_arcmin * math.cos(math.radians(self.axis_altitude_deg))

    @property
    def total_error_arcmin(self) -> float:
        return self.total_error_deg * 60.0

    def instructions(self, hemisphere: str = "north") -> list[str]:
        """Plain directions for the two adjustment knobs."""
        steps: list[str] = []
        if abs(self.altitude_error_deg) * 60 >= 0.5:
            direction = "down" if self.altitude_error_deg > 0 else "up"
            steps.append(f"Altitude: move {direction} by {abs(self.altitude_error_arcmin):.1f}'")
        if abs(self.azimuth_error_deg) * 60 >= 0.5:
            toward = "west" if self.azimuth_error_deg > 0 else "east"
            steps.append(f"Azimuth: move {toward} by {abs(self.azimuth_error_arcmin):.1f}'")
        if not steps:
            steps.append("Within half an arcminute of the pole - nothing to adjust.")
        return steps


def fit_rotation_axis(measurements: list[PolarMeasurement], site: ObservingSite) -> Vec3:
    """Recover the mount's rotation axis from three or more solved points.

    The points sit on a circle about the axis, so the axis is the normal of
    the plane they define. With more than three points the normal is
    averaged over every triple, which damps the effect of one noisy solve.
    """
    if len(measurements) < 3:
        raise AstropiError("polar alignment needs at least three measurements")

    pole = true_pole(site.latitude_deg)
    vectors = [radec_to_vector(m.solved, m.lst_deg, pole) for m in measurements]

    normals: list[Vec3] = []
    for i in range(len(vectors) - 2):
        a, b, c = vectors[i], vectors[i + 1], vectors[i + 2]
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        normal = cross(ab, ac)
        if math.sqrt(dot(normal, normal)) < 1e-9:
            # Collinear points: the sweep was too short to define a plane.
            continue
        normal = normalize(normal)
        # The normal has two directions; keep the one near the pole.
        if dot(normal, pole.axis) < 0:
            normal = (-normal[0], -normal[1], -normal[2])
        normals.append(normal)

    if not normals:
        raise AstropiError(
            "measurement points are collinear - increase the hour angle separation between them"
        )

    summed = (
        sum(n[0] for n in normals),
        sum(n[1] for n in normals),
        sum(n[2] for n in normals),
    )
    return normalize(summed)


def error_from_axis(axis: Vec3, site: ObservingSite) -> PolarAlignmentError:
    """Express a fitted axis as altitude and azimuth error at the knobs."""
    altitude, azimuth = vector_to_alt_az(axis)

    if site.latitude_deg >= 0:
        altitude_error = altitude - abs(site.latitude_deg)
        azimuth_error = wrap_symmetric_deg(azimuth)
    else:
        altitude_error = altitude - abs(site.latitude_deg)
        azimuth_error = wrap_symmetric_deg(180.0 - azimuth)

    # `azimuth_error_deg` stays in bearing, because that is the unit the
    # azimuth knob turns in. On sky the same bearing subtends less the
    # higher the axis sits - by cos(altitude) - so reporting the
    # foreshortened figure at the knob would under-correct by that factor,
    # a third of the required turn at mid-northern latitudes.
    total = math.degrees(
        math.acos(max(-1.0, min(1.0, dot(axis, true_pole(site.latitude_deg).axis))))
    )
    return PolarAlignmentError(
        altitude_error_deg=altitude_error,
        azimuth_error_deg=azimuth_error,
        total_error_deg=total,
        axis_altitude_deg=altitude,
        axis_azimuth_deg=azimuth,
    )


def solve_axis_from_pointing(
    solved: RaDec,
    lst_deg: float,
    axis_angles: tuple[float, float],
    site: ObservingSite,
    *,
    initial: tuple[float, float] = (0.0, 0.0),
) -> Vec3:
    """Infer the polar axis from one solve, given fixed mechanical angles.

    Used while the knobs are being turned. The mount has not moved its own
    axes, so any change in where it points is caused entirely by the polar
    axis moving - two unknowns from two observables, found by Gauss-Newton
    on the angular residual.
    """
    pole = true_pole(site.latitude_deg)
    observed = radec_to_vector(solved, lst_deg, pole)
    hour_angle, declination = axis_angles
    base_altitude = abs(site.latitude_deg)
    northern = site.latitude_deg >= 0

    def predict(alt_error: float, az_error: float) -> Vec3:
        azimuth = az_error if northern else 180.0 - az_error
        candidate = PolarAxis.from_alt_az(base_altitude + alt_error, azimuth)
        return candidate.pointing(hour_angle, declination)

    def residual(alt_error: float, az_error: float) -> tuple[float, float]:
        predicted = predict(alt_error, az_error)
        return (predicted[0] - observed[0], predicted[1] - observed[1])

    alt_error, az_error = initial
    step = 1e-4
    for _ in range(12):
        r0 = residual(alt_error, az_error)
        if math.hypot(*r0) < 1e-10:
            break
        r_alt = residual(alt_error + step, az_error)
        r_az = residual(alt_error, az_error + step)
        # Numerical Jacobian of the two residual components.
        j = (
            ((r_alt[0] - r0[0]) / step, (r_az[0] - r0[0]) / step),
            ((r_alt[1] - r0[1]) / step, (r_az[1] - r0[1]) / step),
        )
        determinant = j[0][0] * j[1][1] - j[0][1] * j[1][0]
        if abs(determinant) < 1e-12:
            break
        d_alt = (-r0[0] * j[1][1] + r0[1] * j[0][1]) / determinant
        d_az = (-j[0][0] * r0[1] + j[1][0] * r0[0]) / determinant
        alt_error += d_alt
        az_error += d_az

    azimuth = az_error if northern else 180.0 - az_error
    return PolarAxis.from_alt_az(base_altitude + alt_error, azimuth).axis


class PolarAlignmentService:
    """Runs the sweep and reports progress on the event bus."""

    def __init__(self, site: ObservingSite, events: EventBus) -> None:
        self._site = site
        self._events = events
        self._measurements: list[PolarMeasurement] = []
        self._axis_angles: tuple[float, float] | None = None
        self._axis_lst_deg: float | None = None
        self._last_error: PolarAlignmentError | None = None

    @property
    def last_error(self) -> PolarAlignmentError | None:
        return self._last_error

    @property
    def measurements(self) -> list[PolarMeasurement]:
        return list(self._measurements)

    def reset(self) -> None:
        self._measurements.clear()
        self._axis_angles = None
        self._axis_lst_deg = None
        self._last_error = None

    def sweep_targets(
        self,
        start: RaDec | None = None,
        *,
        points: int = 3,
        separation_deg: float = DEFAULT_SEPARATION_DEG,
        at: float | None = None,
    ) -> list[RaDec]:
        """Where to point for each measurement.

        Declination is identical at every point - that is the whole
        requirement. Only the hour angle changes, and it steps east so the
        sweep climbs toward the meridian rather than sinking into the
        horizon murk.
        """
        lst = local_sidereal_time_deg(self._site.longitude_deg, at)
        declination = start.dec_deg if start else DEFAULT_DECLINATION_DEG
        if self._site.latitude_deg < 0:
            declination = -abs(declination)
        base_ra = start.ra_deg if start else normalize_deg(lst - separation_deg * (points - 1) / 2)
        return [RaDec(normalize_deg(base_ra + separation_deg * i), declination) for i in range(points)]

    def record(self, solved: RaDec, *, at: float | None = None) -> None:
        """Add a solved measurement point."""
        lst = local_sidereal_time_deg(self._site.longitude_deg, at)
        self._measurements.append(
            PolarMeasurement(solved=solved, lst_deg=lst, timestamp=at or __import__("time").time())
        )
        self._events.publish(
            Topic.POLAR_ALIGN,
            phase="measurement",
            index=len(self._measurements),
            ra_deg=solved.ra_deg,
            dec_deg=solved.dec_deg,
        )

    def compute(self) -> PolarAlignmentError:
        """Fit the axis and publish the resulting error.

        Also records where the mount's mechanical axes were left standing,
        read off the last measurement against the fitted axis. Deriving it
        here rather than asking the mount keeps `refine` working with any
        driver, including ones that will not report raw axis angles and
        whose reported coordinates carry an unknown sync offset.
        """
        axis = fit_rotation_axis(self._measurements, self._site)
        self._axis_angles = self._axis_angles_from(axis)
        self._axis_lst_deg = self._measurements[-1].lst_deg
        error = error_from_axis(axis, self._site)
        self._last_error = error
        self._publish(error, phase="measured")
        return error

    def _axis_angles_from(self, axis: Vec3) -> tuple[float, float]:
        """Hour angle and declination of the last measurement, on the fitted axis."""
        last = self._measurements[-1]
        altitude, azimuth = vector_to_alt_az(axis)
        basis = PolarAxis.from_alt_az(altitude, azimuth)
        pole = true_pole(self._site.latitude_deg)
        return basis.axis_angles(radec_to_vector(last.solved, last.lst_deg, pole))

    def refine(
        self, solved: RaDec, *, at: float | None = None, tracking: bool = True
    ) -> PolarAlignmentError:
        """Recompute the error from a single solve while knobs are turning.

        Assumes the mount has not been commanded to move since the
        measurement - only the knobs have turned. It *has* kept tracking,
        though, so its hour angle axis has advanced; since tracking runs at
        exactly the sidereal rate, that advance equals the change in local
        sidereal time. Ignoring it makes the fit drift by a quarter of a
        degree of hour angle per minute spent adjusting.
        """
        if self._axis_angles is None or self._axis_lst_deg is None:
            raise AstropiError("run the three-point measurement before refining")
        lst = local_sidereal_time_deg(self._site.longitude_deg, at)

        hour_angle, declination = self._axis_angles
        if tracking:
            hour_angle += wrap_symmetric_deg(lst - self._axis_lst_deg)
        axis_angles = (hour_angle, declination)

        previous = (
            (self._last_error.altitude_error_deg, self._last_error.azimuth_error_deg)
            if self._last_error
            else (0.0, 0.0)
        )
        axis = solve_axis_from_pointing(solved, lst, axis_angles, self._site, initial=previous)
        error = error_from_axis(axis, self._site)
        self._last_error = error
        self._publish(error, phase="refining")
        return error

    def _publish(self, error: PolarAlignmentError, *, phase: str) -> None:
        self._events.publish(
            Topic.POLAR_ALIGN,
            phase=phase,
            altitude_error_arcmin=round(error.altitude_error_arcmin, 2),
            azimuth_error_arcmin=round(error.azimuth_error_arcmin, 2),
            total_error_arcmin=round(error.total_error_arcmin, 2),
            instructions=error.instructions(self._site.hemisphere),
        )


async def measure(
    service: PolarAlignmentService,
    slew: callable,
    solve: callable,
    *,
    points: int = 3,
    separation_deg: float = DEFAULT_SEPARATION_DEG,
    start: RaDec | None = None,
) -> PolarAlignmentError:
    """Drive a full sweep: slew, solve, repeat, then fit.

    Takes the slew and solve steps as callables so this stays testable and
    free of any device imports - the sequencing layer supplies the real ones.
    """
    service.reset()
    for target in service.sweep_targets(start, points=points, separation_deg=separation_deg):
        await slew(target)
        # Let the mount settle before the exposure: a frame taken while the
        # gears are still relaxing solves to a position the mount is no
        # longer at, which would tilt the fitted plane.
        await asyncio.sleep(1.0)
        service.record(await solve())
    return service.compute()
