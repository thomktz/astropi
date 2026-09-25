"""A running estimate of where a guide star is and how fast it is drifting.

One Kalman filter per axis, with two things in its state: the star's
error from the lock point, and the rate it is drifting at. Every frame it

1. predicts where the star should be now - where it was, plus the drift
   over the time since, plus whatever correction was sent meanwhile - and
2. compares that with where the frame says it is, and moves both
   estimates towards the frame by as much as the frame deserves: little
   when the estimate is already well known and the seeing is bad, more
   early on or when the frames are clean.

So the drift estimate never restarts, is refined by every single frame,
and knows how uncertain it is. Corrections are part of the prediction,
which is what lets it keep measuring the drift while the loop is
cancelling it.

`agility` is how fast the drift itself may change, in arcsec per second
per square-root second. Small for declination, where the drift is polar
misalignment and holds steady for hours; larger for right ascension,
where the worm gear's periodic error swings the rate back and forth over
minutes and a filter that trusted its past too much would lag behind it.
Tuned against a simulated sky: at 0.0003 a steady 6"/min drift is known
to within 1"/min after about 50 s and then moves by 0.05"/min per frame;
at 0.003 a +/-7"/min worm swing is followed to within 2-3"/min.

Pure maths, no I/O, so it can be tested with made-up numbers.
"""

from __future__ import annotations

import math

#: Before the first frame: the drift could be anything up to about this,
#: in arcsec per second (12"/min).
INITIAL_DRIFT_SIGMA = 0.2
#: Star motion the model does not explain even over a moment - a gust, a
#: cable tug - in arcsec per square-root second.
POSITION_JITTER = 0.05


class DriftFilter:
    """Error and drift of one axis, estimated from every frame."""

    def __init__(self, agility: float, seeing_arcsec: float = 1.0) -> None:
        self._qv = agility**2
        self._qp = POSITION_JITTER**2
        self._initial_seeing = seeing_arcsec
        self.clear()

    def clear(self) -> None:
        self._x: tuple[float, float] | None = None
        self._p = ((0.0, 0.0), (0.0, 0.0))
        self._t: float | None = None
        self._pending = 0.0
        #: Scatter of a single frame's measurement, learned as it goes.
        self.seeing = self._initial_seeing

    @property
    def ready(self) -> bool:
        return self._x is not None

    @property
    def position(self) -> float:
        """Arcsec from the lock point, with the seeing averaged out."""
        return 0.0 if self._x is None else self._x[0]

    @property
    def drift(self) -> float:
        """Arcsec per second the axis is drifting, corrections aside."""
        return 0.0 if self._x is None else self._x[1]

    @property
    def drift_error(self) -> float:
        """One standard error on the drift."""
        return INITIAL_DRIFT_SIGMA if self._x is None else math.sqrt(max(self._p[1][1], 0.0))

    @property
    def drift_is_real(self) -> bool:
        return self.ready and abs(self.drift) > 2 * self.drift_error

    def pushed(self, predicted_arcsec: float) -> None:
        """A correction sent: the change in error it should cause."""
        if self._x is not None:
            self._pending += predicted_arcsec

    def move_reference(self, shift_arcsec: float) -> None:
        """The lock point moved by this much, as a dither does.

        The error jumps by exactly that with nothing on the sky moving,
        so the position estimate jumps with it and the drift is untouched.
        """
        if self._x is not None:
            self._x = (self._x[0] - shift_arcsec, self._x[1])

    def observe(self, timestamp: float, error_arcsec: float) -> None:
        """One frame's measured error."""
        if not math.isfinite(error_arcsec):
            return
        if self._x is None or self._t is None:
            self._x = (error_arcsec, 0.0)
            self._p = ((self.seeing**2, 0.0), (0.0, INITIAL_DRIFT_SIGMA**2))
            self._t = timestamp
            self._pending = 0.0
            return

        dt = max(timestamp - self._t, 1e-3)
        self._t = timestamp

        # Predict: drift over the interval, plus what was pushed.
        position, drift = self._x
        position += drift * dt + self._pending
        self._pending = 0.0
        (p00, p01), (_, p11) = self._p
        p00 = p00 + 2 * dt * p01 + dt * dt * p11 + self._qp * dt + self._qv * dt**3 / 3
        p01 = p01 + dt * p11 + self._qv * dt**2 / 2
        p11 = p11 + self._qv * dt

        # Update: move towards the frame by as much as it deserves.
        variance = self.seeing**2
        spread = p00 + variance
        gain_position = p00 / spread
        gain_drift = p01 / spread
        surprise = error_arcsec - position
        self._x = (position + gain_position * surprise, drift + gain_drift * surprise)
        self._p = (
            ((1 - gain_position) * p00, (1 - gain_position) * p01),
            ((1 - gain_position) * p01, p11 - gain_drift * p01),
        )

        # Learn the seeing from how surprised it keeps being: on average
        # the squared surprise should equal the spread it expected.
        variance += 0.05 * (surprise * surprise - spread)
        self.seeing = math.sqrt(min(max(variance, 0.01), 25.0))
