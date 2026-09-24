"""What the mount and the sky are doing when nothing is correcting them.

The measurement PHD2 calls the Guiding Assistant: stop guiding, watch the
star for a couple of minutes, and separate what the loop *should* chase
from what it should not.

Three things come out of that, and they are the three questions a guide
loop cannot answer about itself:

* **Seeing** - the high-frequency wobble left after the drift is taken
  out. Guiding cannot remove it and chasing it makes the image worse, so
  it sets the floor for the minimum-move threshold.
* **Drift** - the steady slope underneath. In declination that is mostly
  polar misalignment, and it converts to an alignment error in arcminutes.
* **Backlash** - how much travel a reversing declination correction loses
  in the gears before the axis moves at all.

The maths lives here as functions over a list of samples so it can be
tested against series with a known answer; the measuring is a task,
because it takes minutes and has to be cancellable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Declination drift, in arcseconds per minute, caused by one arcminute
#: of polar misalignment on a star that is well placed to show it.
#:
#: One arcminute is 2.909e-4 radians; the sky turns at 15.04 arcsec per
#: second; the product is 0.262 arcsec per minute. Its reciprocal, 3.81,
#: is the number every drift-alignment guide quotes without deriving.
ARCMIN_PER_ARCSEC_PER_MIN = 3.81


@dataclass(frozen=True, slots=True)
class DriftSample:
    """One unguided look at the star."""

    timestamp: float
    ra_arcsec: float
    dec_arcsec: float


@dataclass(frozen=True, slots=True)
class AxisMeasurement:
    """What one axis did while nothing was correcting it."""

    drift_arcsec_per_min: float
    #: The wobble left once the drift is removed. Seeing, and whatever
    #: the mount does at frequencies guiding cannot reach.
    seeing_rms_arcsec: float
    peak_to_peak_arcsec: float
    max_rate_arcsec_per_min: float
    #: The largest single frame-to-frame step, and the typical one. In
    #: arcseconds, so the two can be compared with each other and with
    #: the seeing - which a rate per minute cannot be.
    max_step_arcsec: float = 0.0
    typical_step_arcsec: float = 0.0


@dataclass(frozen=True, slots=True)
class BacklashMeasurement:
    """How much a declination reversal loses before the axis moves."""

    arcsec: float
    milliseconds: float
    #: How confident that figure is: the spread of the reversals measured.
    uncertainty_arcsec: float


@dataclass(frozen=True, slots=True)
class AssistantReport:
    samples: int
    seconds: float
    ra: AxisMeasurement
    dec: AxisMeasurement
    #: Polar misalignment implied by the declination drift, and whether
    #: the geometry was good enough for it to mean much.
    polar_error_arcmin: float | None
    polar_error_confidence: str
    backlash: BacklashMeasurement | None = None
    recommendations: list[str] = field(default_factory=list)
    suggested_min_move_arcsec: float | None = None
    suggested_dec_mode: str | None = None


def measure_axis(samples: list[DriftSample], pick) -> AxisMeasurement:
    """Split one axis into the part that drifts and the part that does not."""
    if len(samples) < 3:
        return AxisMeasurement(0.0, 0.0, 0.0, 0.0)

    start = samples[0].timestamp
    minutes = [(sample.timestamp - start) / 60.0 for sample in samples]
    values = [pick(sample) for sample in samples]

    slope, intercept = _least_squares(minutes, values)
    # The residual *is* the interesting part: what is left when the
    # steady component has been taken out is what guiding would be
    # chasing if it chased everything.
    residuals = [value - (intercept + slope * minute) for minute, value in zip(minutes, values, strict=True)]
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals))

    steps = [abs(values[index] - values[index - 1]) for index in range(1, len(values))]
    rates = [
        step / max(minutes[index + 1] - minutes[index], 1e-6) for index, step in enumerate(steps)
    ]
    typical_step = math.sqrt(sum(step * step for step in steps) / len(steps)) if steps else 0.0

    return AxisMeasurement(
        drift_arcsec_per_min=slope,
        seeing_rms_arcsec=rms,
        peak_to_peak_arcsec=max(values) - min(values),
        max_rate_arcsec_per_min=max(rates) if rates else 0.0,
        max_step_arcsec=max(steps) if steps else 0.0,
        typical_step_arcsec=typical_step,
    )


def polar_error_arcmin(dec_drift_arcsec_per_min: float, declination_deg: float) -> float:
    """Polar misalignment implied by a declination drift rate.

    Attributes *all* of the drift to the polar axis, which is the usual
    assumption and is wrong in the presence of refraction, flexure or a
    mount with a tilted declination axis. It is a measurement of the
    drift with a conversion applied, not an independent truth - which is
    why the three-point alignment routine still exists.
    """
    cos_dec = math.cos(math.radians(declination_deg))
    if cos_dec < 0.15:  # within about 8 degrees of the pole
        return float("inf")
    return abs(dec_drift_arcsec_per_min) * ARCMIN_PER_ARCSEC_PER_MIN / cos_dec


def polar_confidence(declination_deg: float, hour_angle_deg: float) -> str:
    """How much the polar figure is worth, given where the star is.

    Declination drift shows the azimuth error near the meridian and the
    altitude error near the horizon, and shows neither cleanly in
    between. Near the pole it shows almost nothing at all: the cosine in
    the conversion runs away, and a tiny measurement error becomes a
    large alignment error.
    """
    if abs(declination_deg) > 65:
        return "poor - too close to the pole for declination drift to say much"
    hours = abs(((hour_angle_deg + 180) % 360 - 180) / 15.0)
    if hours < 1.5:
        return "good - near the meridian, so this is mostly the azimuth error"
    if hours > 4.5:
        return "good - well off the meridian, so this is mostly the altitude error"
    return "fair - between the meridian and the horizon, so it mixes azimuth and altitude"


def analyse(
    samples: list[DriftSample],
    *,
    declination_deg: float,
    hour_angle_deg: float,
    backlash: BacklashMeasurement | None = None,
    current_min_move_arcsec: float = 0.15,
) -> AssistantReport:
    """Turn a drift run into numbers and advice."""
    ra = measure_axis(samples, lambda s: s.ra_arcsec)
    dec = measure_axis(samples, lambda s: s.dec_arcsec)
    seconds = samples[-1].timestamp - samples[0].timestamp if len(samples) > 1 else 0.0

    error = polar_error_arcmin(dec.drift_arcsec_per_min, declination_deg)
    recommendations: list[str] = []

    # Minimum move: do not correct what is only seeing. A threshold near
    # the high-frequency RMS spends the loop's effort on the drift and
    # leaves the wobble alone, which is the whole trick.
    suggested_min_move = round(max(ra.seeing_rms_arcsec, dec.seeing_rms_arcsec), 2)
    if suggested_min_move > current_min_move_arcsec * 1.5:
        recommendations.append(
            f"Raise the dead band to about {suggested_min_move:.2f}″. Below that the loop is "
            f"correcting seeing, which moves the mount without improving the image."
        )
    elif suggested_min_move < current_min_move_arcsec * 0.5:
        recommendations.append(
            f"The sky is steadier than the dead band assumes; {suggested_min_move:.2f}″ would "
            "let the loop correct real error it is currently ignoring."
        )

    suggested_dec_mode = None
    if backlash is not None and backlash.milliseconds > 3000:
        suggested_dec_mode = "north" if dec.drift_arcsec_per_min < 0 else "south"
        recommendations.append(
            f"Declination backlash is {backlash.milliseconds:.0f} ms - too much to guide through. "
            f"Guide declination one way only ({suggested_dec_mode}), against the drift."
        )
    elif backlash is not None and backlash.arcsec > 0.5:
        recommendations.append(
            f"Declination loses {backlash.arcsec:.1f}″ on a reversal. Expect a pause after "
            "every change of direction, and prefer one-way declination guiding."
        )

    if math.isfinite(error) and error > 5:
        recommendations.append(
            f"Declination is drifting {abs(dec.drift_arcsec_per_min):.2f}″ a minute, which is "
            f"about {error:.0f} arcmin of polar misalignment. Guiding will hide it, but "
            "the field will still rotate."
        )

    # An outlier against the *other steps*, not against a rate per minute
    # - which is a different unit and was firing this advice on ordinary
    # Gaussian noise.
    if ra.typical_step_arcsec > 0 and ra.max_step_arcsec > 4 * ra.typical_step_arcsec:
        recommendations.append(
            f"One right ascension frame jumped {ra.max_step_arcsec:.2f}\u2033 against a typical "
            f"{ra.typical_step_arcsec:.2f}\u2033 - look for a sticky worm, cable drag or wind "
            "before blaming the guide settings."
        )

    return AssistantReport(
        samples=len(samples),
        seconds=seconds,
        ra=ra,
        dec=dec,
        polar_error_arcmin=None if math.isinf(error) else round(error, 2),
        polar_error_confidence=polar_confidence(declination_deg, hour_angle_deg),
        backlash=backlash,
        recommendations=recommendations,
        suggested_min_move_arcsec=suggested_min_move,
        suggested_dec_mode=suggested_dec_mode,
    )


def _least_squares(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Slope and intercept, by the usual closed form."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    top = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    bottom = sum((x - mean_x) ** 2 for x in xs)
    if bottom <= 0:
        return 0.0, mean_y
    slope = top / bottom
    return slope, mean_y - slope * mean_x
