"""What each guide axis is doing on its own, and what corrections do to it.

The error the loop measures on an axis is where the sky has drifted to,
minus everything the loop has pushed back since, plus seeing:

    error(t) = start + drift(t) + efficiency x pushed(t) + seeing

where `pushed(t)` is the running total of what the calibration says every
correction so far should have moved the star, and `drift(t)` is a gentle
curve - polar misalignment is a straight line, periodic error is a slow
sinusoid that looks like a parabola over a minute or two. Fitting that
over the recent cycles separates the two things the loop otherwise never
looks at: the baseline the mount drifts at when left alone, and how much
of each correction actually arrives.

Why levels against running totals, not frame-to-frame changes against
the last push. The obvious version - "the error went from 3 to 1 after a
2 arcsec push, so the push worked" - is fooled by seeing: a frame that
reads high because of a gust gets a large correction *and* is followed by
a frame that reads lower regardless. Fitted that way the corrections look
roughly twice as effective as they are. Here the seeing of the current
frame cannot have influenced any push already in the running total, so it
cannot masquerade as their effect.

Pure maths, no I/O, so it can be tested with made-up numbers.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

#: How strongly the efficiency is held at one - what the calibration said -
#: in arcsec of equivalent evidence. When every correction is the same
#: size, as against a steady drift, pushes and time rise together and the
#: data cannot tell a strong drift with weak corrections from a weak drift
#: with strong ones; this keeps the fit at the calibration then, rather
#: than undefined.
EFFICIENCY_PRIOR_ARCSEC = 1.0


@dataclass(frozen=True, slots=True)
class AxisFit:
    #: Arcsec per second the axis is drifting, with no correction.
    drift_arcsec_per_s: float
    #: One standard error on that. A drift smaller than a couple of these
    #: is indistinguishable from none.
    drift_error_arcsec_per_s: float
    #: Fraction of each predicted correction that actually arrives, once
    #: enough cycles are in to say; until then, `None`.
    efficiency: float | None
    efficiency_error: float | None
    #: Cycles the fit is based on.
    samples: int
    #: Scatter of what the model does not explain - mostly seeing.
    residual_arcsec: float

    @property
    def drift_is_real(self) -> bool:
        return abs(self.drift_arcsec_per_s) > 2 * self.drift_error_arcsec_per_s


class AxisModel:
    """A sliding fit of one axis: drift, plus efficiency x pushes.

    Two windows, because the two change at different speeds. Efficiency
    is a property of the mount and changes slowly, and needs a long window
    anyway: fitted over a few dozen cycles a closed loop overstates it,
    which shrinks as the window grows - at a hundred cycles it is small.
    Drift changes over minutes - periodic error is a sinusoid - so it is
    fitted over the recent cycles only, with the pushes taken out at the
    efficiency the long window found.
    """

    def __init__(
        self,
        window: int = 100,
        drift_window: int = 20,
        min_samples: int = 10,
        min_efficiency_samples: int = 40,
    ) -> None:
        self._rows: deque[tuple[float, float, float]] = deque(maxlen=window)
        self._drift_window = drift_window
        self._min_samples = min_samples
        self._min_efficiency_samples = min_efficiency_samples
        self._pushed = 0.0
        self._offset = 0.0

    def clear(self) -> None:
        self._rows.clear()
        self._pushed = 0.0
        self._offset = 0.0

    def observe(self, timestamp: float, error_arcsec: float) -> None:
        """A measured error, before this frame's correction is sent."""
        if math.isfinite(error_arcsec):
            self._rows.append((timestamp, error_arcsec + self._offset, self._pushed))

    def pushed(self, predicted_arcsec: float) -> None:
        """A correction sent: the change in error it should cause."""
        self._pushed += predicted_arcsec

    def move_reference(self, shift_arcsec: float) -> None:
        """The lock point moved by this much along the axis, as a dither does.

        The error jumps by exactly that without the sky or the mount doing
        anything, so it is folded back out rather than fitted as drift.
        """
        self._offset += shift_arcsec

    def fit(self, *, assume_efficiency: float | None = None) -> AxisFit | None:
        """Drift and efficiency over the recent cycles.

        The efficiency is always fitted and reported, but a closed loop
        cannot measure it well: tested against known truth it reads high,
        and when every push is the same size - a steady drift, a steady
        correction - it cannot be told from the drift at all. So the
        drift can instead be computed with the pushes taken at face
        value, `assume_efficiency`, which is what the calibration says.
        """
        rows = self._rows
        if len(rows) < self._min_samples:
            return None
        t = np.array([r[0] for r in rows])
        y = np.array([r[1] for r in rows])
        pushed = np.array([r[2] for r in rows])

        efficiency, efficiency_error = self._efficiency(t, y, pushed)

        # What the sky did on its own: the error with every push taken
        # back out, at the efficiency they are known to arrive with.
        recent = slice(-self._drift_window, None)
        taken = assume_efficiency if assume_efficiency is not None else efficiency
        unguided = y[recent] - (1.0 if taken is None else taken) * pushed[recent]
        tr = t[recent] - t[recent][0]
        if len(tr) < 3 or float(np.ptp(tr)) <= 0:
            return None
        design = np.column_stack([np.ones_like(tr), tr])
        coef, *_ = np.linalg.lstsq(design, unguided, rcond=None)
        residuals = unguided - design @ coef
        variance = float(residuals @ residuals) / max(1, len(tr) - 2)
        spread = float(((tr - tr.mean()) ** 2).sum())
        return AxisFit(
            drift_arcsec_per_s=float(coef[1]),
            drift_error_arcsec_per_s=math.sqrt(variance / spread),
            efficiency=efficiency,
            efficiency_error=efficiency_error,
            samples=len(rows),
            residual_arcsec=math.sqrt(float(residuals @ residuals) / len(tr)),
        )

    def _efficiency(
        self, t: np.ndarray, y: np.ndarray, pushed: np.ndarray
    ) -> tuple[float | None, float | None]:
        if len(t) < self._min_efficiency_samples:
            return None, None
        # A slow curve for the drift across the long window, plus the pushes.
        span = max(float(t[-1] - t[0]), 1e-6)
        u = (t - t[-1]) / span
        design = np.column_stack([np.ones_like(u), u, u * u, pushed])
        prior = np.array([[0.0, 0.0, 0.0, EFFICIENCY_PRIOR_ARCSEC]])
        a = np.vstack([design, prior])
        b = np.concatenate([y, [EFFICIENCY_PRIOR_ARCSEC]])
        try:
            coef, *_ = np.linalg.lstsq(a, b, rcond=None)
            inverse = np.linalg.inv(a.T @ a)
        except np.linalg.LinAlgError:
            return None, None
        residuals = y - design @ coef
        variance = float(residuals @ residuals) / max(1, len(t) - 4)
        return float(coef[3]), math.sqrt(max(variance * float(inverse[3, 3]), 0.0))
