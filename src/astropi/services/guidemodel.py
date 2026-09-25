"""What an axis is drifting at while it is being guided.

While guiding, the error the loop measures is where the sky has drifted
to minus everything the loop has pushed back since, plus seeing. Adding
the running total of those pushes back in - at what the calibration says
they do - leaves the drift on its own, which is fitted as a line over the
recent frames.

This is a readout, not what the loop steers by. The drift the loop
cancels is measured before guiding starts, with no corrections running,
where it is a plain slope and needs nothing taken out; see
`GuidingService._null_drift`.

Pure maths, no I/O, so it can be tested with made-up numbers.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AxisFit:
    #: Arcsec per second the axis drifts with no correction.
    drift_arcsec_per_s: float
    #: One standard error on that. A drift smaller than a couple of these
    #: is indistinguishable from none.
    drift_error_arcsec_per_s: float
    #: Frames the fit is based on.
    samples: int
    #: Scatter of what the line does not explain - mostly seeing.
    residual_arcsec: float

    @property
    def drift_is_real(self) -> bool:
        return abs(self.drift_arcsec_per_s) > 2 * self.drift_error_arcsec_per_s


class AxisModel:
    """The recent errors of one axis, with the pushes added back in."""

    def __init__(self, window: int = 20, min_samples: int = 8) -> None:
        self._rows: deque[tuple[float, float]] = deque(maxlen=window)
        self._min_samples = min_samples
        self._pushed = 0.0
        self._offset = 0.0

    def clear(self) -> None:
        self._rows.clear()
        self._pushed = 0.0
        self._offset = 0.0

    def observe(self, timestamp: float, error_arcsec: float) -> None:
        """A measured error, before this frame's correction is sent."""
        if math.isfinite(error_arcsec):
            self._rows.append((timestamp, error_arcsec + self._offset - self._pushed))

    def pushed(self, predicted_arcsec: float) -> None:
        """A correction sent: the change in error it should cause."""
        self._pushed += predicted_arcsec

    def move_reference(self, shift_arcsec: float) -> None:
        """The lock point moved by this much along the axis, as a dither does.

        The error jumps by exactly that without the sky or the mount doing
        anything, so it is folded back out rather than fitted as drift.
        """
        self._offset += shift_arcsec

    def fit(self) -> AxisFit | None:
        rows = self._rows
        n = len(rows)
        if n < self._min_samples:
            return None
        t_mean = sum(t for t, _ in rows) / n
        y_mean = sum(y for _, y in rows) / n
        stt = sum((t - t_mean) ** 2 for t, _ in rows)
        if stt <= 0:
            return None
        slope = sum((t - t_mean) * (y - y_mean) for t, y in rows) / stt
        squares = sum((y - y_mean - slope * (t - t_mean)) ** 2 for t, y in rows)
        return AxisFit(
            drift_arcsec_per_s=slope,
            drift_error_arcsec_per_s=math.sqrt(squares / (n - 2) / stt),
            samples=n,
            residual_arcsec=math.sqrt(squares / n),
        )


class DriftCanceller:
    """One axis's cancel-and-re-measure rounds, and what they teach.

    Each round reports the drift left over while cancelling `rate`. The
    first round, cancelling nothing, is the drift itself. From the second
    on, two rounds at two different rates say how much of a change in the
    cancelling rate actually reaches the sky:

        response = (left over before - left over now) / (rate now - rate before)

    One when the calibration is right, a half when every pulse moves the
    star half as far as calibrated. Measured this way - between rounds,
    with no position corrections mixed in - it is a plain comparison of
    two slopes, not something to be teased out of a running loop. The
    next rate then aims for the whole of what is left, at that response,
    instead of creeping up on it a fraction per round.

    Not every drift holds still long enough to cancel. Right ascension
    carries the worm gear's periodic error, a slope that swings back and
    forth over minutes: cancel this round's and the next round finds a
    different one. Two rounds fix both unknowns - the drift and the
    response - so there is nothing to check them against; from the third
    on, each round has a prediction from the earlier ones. A round that
    misses it by more than the uncertainties allow means the drift moved
    under the measurement: the axis is called unsteady, and only the
    average drift is cancelled. The swinging part is position guiding's.
    """

    #: A round must miss its prediction by this many standard errors
    #: before the drift is called unsteady.
    UNSTEADY_SIGMA = 4.0

    #: Responses outside this are a measurement gone wrong - a gust, a
    #: lost frame, backlash - rather than a mount.
    RESPONSE_RANGE = (0.25, 2.5)

    def __init__(self, tolerance: float) -> None:
        self.tolerance = tolerance
        #: The drift being cancelled, in arcsec per second.
        self.rate = 0.0
        #: Fraction of the calibrated effect a pulse has; one until measured.
        self.response = 1.0
        self.response_measured = False
        # Every pair of rounds gives an estimate; a big step between them
        # gives a good one and a small step a poor one. Weighted by that,
        # rather than the latest winning - the last step before settling
        # is usually the smallest, and would otherwise overrule the first.
        self._weighted = 0.0
        self._weights = 0.0
        #: Every round: the rate it cancelled, what was left, and how well
        #: that was measured.
        self._rounds: list[tuple[float, float, float]] = []
        self.unsteady = False

    def update(self, left_over: float, error: float) -> bool:
        """A round's result; whether there is still drift to cancel."""
        if self.unsteady:
            return False
        if len(self._rounds) >= 2 and self._missed_prediction(left_over, error):
            self._rounds.append((self.rate, left_over, error))
            self.unsteady = True
            # The drift each round saw, at the response learned so far.
            skies = [left + self.response * rate for rate, left, _ in self._rounds]
            self.rate = sum(skies) / len(skies) / self.response
            return False
        if self._rounds:
            self._learn_response(*self._rounds[-1], left_over, error)
        self._rounds.append((self.rate, left_over, error))
        remaining = abs(left_over) > max(self.tolerance, 2 * error)
        if remaining:
            self.rate += left_over / self.response
        return remaining

    def _missed_prediction(self, left_over: float, error: float) -> bool:
        """Whether this round is not what the earlier ones predicted."""
        rate, left, previous_error = self._rounds[-1]
        predicted = left - self.response * (self.rate - rate)
        miss = abs(left_over - predicted)
        return miss > max(self.tolerance, self.UNSTEADY_SIGMA * math.hypot(error, previous_error))

    def _learn_response(
        self, before_rate: float, before_left: float, before_error: float, left_over: float, error: float
    ) -> None:
        step = self.rate - before_rate
        if step == 0:
            return
        response = (before_left - left_over) / step
        uncertainty = math.hypot(before_error, error) / abs(step)
        low, high = self.RESPONSE_RANGE
        if uncertainty < 0.25 and low <= response <= high:
            weight = 1.0 / uncertainty**2
            self._weighted += weight * response
            self._weights += weight
            self.response = self._weighted / self._weights
            self.response_measured = True
