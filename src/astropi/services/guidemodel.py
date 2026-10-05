"""Where the guide star drifts, and what corrections do to it - in pixels.

For each sensor axis, over the last few minutes of frames:

    offset(t) = start + drift x t + ra_response x RA_sent + dec_response x Dec_sent
                + ra_calibrated x worm(t)

where RA_sent and Dec_sent are the running totals of corrections sent and
worm(t) is the RA worm's periodic error, a sinusoid of the worm's period.
Before any correction the totals are zero and this is a straight line
through the star's track, plus the worm's swing: the drift. Once
corrections go out, the same fit separates what the sky did from what the
corrections did - and the guide loop steers by it: the move wanted before
the next frame, solved for the RA and Dec corrections that make it.

Pure maths, no I/O, so it can be tested with made-up numbers.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Response:
    """How far one unit of each correction moves the star, in pixels.

    `ra` and `dec` are (dx, dy) on the sensor per unit of that axis's
    correction - an arcsecond of RA axis turned and one declination step
    with the fine controls, a millisecond of pulse without them. Positive
    units are west and north.
    """

    ra: tuple[float, float]
    dec: tuple[float, float]

    def solve(self, dx: float, dy: float) -> tuple[float, float] | None:
        """The (RA, Dec) units that move the star by (dx, dy)."""
        (a, c), (b, d) = self.ra, self.dec
        det = a * d - b * c
        if abs(det) < 1e-12:
            return None
        return (d * dx - b * dy) / det, (-c * dx + a * dy) / det


@dataclass(frozen=True, slots=True)
class Worm:
    """The RA worm's periodic error, as fitted.

    The worm turns the RA axis back and forth by the same amount every
    revolution, so it is a sinusoid of a known period with an unknown
    size and phase:

        worm(t) = sin_units x sin(2 pi t / period) + cos_units x cos(2 pi t / period)

    in RA correction units, with t in Unix seconds. It moves the star the
    way an RA correction does, along the calibrated RA response.
    """

    period_s: float
    sin_units: float
    cos_units: float
    #: Pixels one RA unit moves the star: the direction the worm pushes it.
    direction: tuple[float, float]
    #: One standard error on the amplitude, in RA units.
    amplitude_error: float

    @property
    def amplitude(self) -> float:
        """Half the peak-to-peak swing, in RA units."""
        return math.hypot(self.sin_units, self.cos_units)

    @property
    def amplitude_px(self) -> float:
        return self.amplitude * math.hypot(*self.direction)

    def units(self, t: float) -> float:
        phase = 2 * math.pi * t / self.period_s
        return self.sin_units * math.sin(phase) + self.cos_units * math.cos(phase)

    def change_px(self, t0: float, t1: float) -> tuple[float, float]:
        """How far the worm moves the star between two moments."""
        delta = self.units(t1) - self.units(t0)
        return self.direction[0] * delta, self.direction[1] * delta


@dataclass(frozen=True, slots=True)
class PixelFit:
    #: Where the star is now, in pixels from the lock point: the fit's
    #: value now, plus how far the last few frames sit off it - so a
    #: sudden move shows within a few frames, while the seeing is still
    #: averaged over them.
    position: tuple[float, float]
    #: Pixels per second the star drifts with no correction, and one
    #: standard error on each. Steady drift only: the worm's swing is
    #: fitted separately, when its period is known.
    drift: tuple[float, float]
    drift_error: tuple[float, float]
    #: What the window says each correction does - pulled towards the
    #: calibration by as much as the window cannot tell.
    response: Response
    #: One standard error on each of those four numbers. Close to the
    #: prior's own width means the window could not tell, and the value
    #: is mostly the calibration's.
    response_error: Response
    #: Scatter of the frames about the fit, in pixels: seeing, mostly.
    scatter: float
    frames: int
    #: Seconds the window spans.
    span_s: float
    #: When the newest frame was taken, Unix seconds.
    time: float = 0.0
    #: The worm's periodic error; `None` when its period is not known.
    worm: Worm | None = None

    def motion(self, seconds: float) -> tuple[float, float]:
        """Where the sky will take the star over the next `seconds`, with
        no correction: the drift, plus the worm's swing over that time."""
        dx, dy = self.drift[0] * seconds, self.drift[1] * seconds
        if self.worm is not None:
            wx, wy = self.worm.change_px(self.time, self.time + seconds)
            dx, dy = dx + wx, dy + wy
        return dx, dy


class PixelModel:
    """Drift, worm and correction response, fitted over the last few minutes.

    Both sensor axes are fitted together, over the window:

        offset(t) = start + drift x t + ra_response x RA_sent + dec_response x Dec_sent
                    + ra_calibrated x worm(t)

    where RA_sent and Dec_sent are the running totals of corrections sent
    since the window began. Before any correction goes out the totals are
    zero and this is a straight line through the track - the drift - plus
    the worm. Once corrections go out, the same fit separates what the sky
    did from what the corrections did.

    The worm is fitted only when its period is known. A straight line
    through a sinusoid is not flat even over exactly one period - its
    slope swings with the phase the window starts at - so without it,
    the worm's error leaks into the drift and moves with the worm.

    The responses start at the calibration and are held there with a
    weight worth `prior_fraction` of their size; they move only as far as
    the window can show they should. With corrections that are all the
    same size - a steady drift being cancelled steadily - the window
    cannot tell drift from response, and they stay at the calibration.
    """

    def __init__(
        self,
        calibration: Response,
        *,
        window_s: float = 300.0,
        max_frames: int = 2000,
        prior_fraction: float = 0.2,
        min_frames: int = 6,
        worm_period_s: float | None = None,
        worm_prior_px: float = 10.0,
        recent_frames: int = 6,
        recent_threshold: float = 2.0,
    ) -> None:
        self.calibration = calibration
        #: Seconds of frames the fit spans. Long, because the drift is a
        #: slow thing and is known better the longer it is watched.
        self.window_s = window_s
        #: The worm's period, when it is known; `None` fits no worm.
        self.worm_period_s = worm_period_s
        self._rows: deque[tuple[float, float, float, float, float]] = deque(maxlen=max_frames)
        self._prior_fraction = prior_fraction
        self._min_frames = min_frames
        #: How large a worm swing is believable before the window says so:
        #: the width of the prior holding its amplitude at zero, in pixels.
        #: It is what keeps a window shorter than one worm turn from
        #: trading drift for worm freely.
        self.worm_prior_px = worm_prior_px
        self._recent_frames = recent_frames
        self._recent_threshold = recent_threshold
        self._ra_sent = 0.0
        self._dec_sent = 0.0

    def clear(self) -> None:
        self._rows.clear()
        self._ra_sent = 0.0
        self._dec_sent = 0.0

    def sent(self, ra_units: float, dec_units: float) -> None:
        """Corrections delivered since the last frame."""
        self._ra_sent += ra_units
        self._dec_sent += dec_units

    def observe(self, timestamp: float, dx: float, dy: float) -> None:
        """The star's offset from the lock point in one frame, in pixels."""
        if math.isfinite(dx) and math.isfinite(dy):
            self._rows.append((timestamp, dx, dy, self._ra_sent, self._dec_sent))
            while self._rows and self._rows[0][0] < timestamp - self.window_s:
                self._rows.popleft()

    def move_reference(self, shift_x: float, shift_y: float) -> None:
        """The lock point moved by this much - a dither: every offset in
        the window is re-expressed against the new one."""
        self._rows = deque(
            ((t, x - shift_x, y - shift_y, r, d) for t, x, y, r, d in self._rows),
            maxlen=self._rows.maxlen,
        )

    def fit(self) -> PixelFit | None:
        rows = np.array(self._rows, dtype=float).reshape(-1, 5)
        count = len(rows)
        if count < self._min_frames:
            return None
        t_now = float(rows[-1, 0])
        span = t_now - float(rows[0, 0])
        if span <= 0:
            return None
        cal = self.calibration
        # Time and corrections both measured back from the newest frame, so
        # the fitted start is where the star is *now*, with everything sent
        # so far already in it. Referenced to the oldest frame instead, it
        # was where the star would have been had nothing been sent since -
        # and a loop steering by that corrected every correction again.
        times = rows[:, 0] - t_now
        ra = rows[:, 3] - rows[-1, 3]
        dec = rows[:, 4] - rows[-1, 4]
        zero, one = np.zeros(count), np.ones(count)
        # Parameters: start x, y; drift x, y; RA response x, y; Dec
        # response x, y; and, with a worm, its sine and cosine terms.
        columns_x = [one, zero, times, zero, ra, zero, dec, zero]
        columns_y = [zero, one, zero, times, zero, ra, zero, dec]
        # Priors, per parameter: a fraction of that column's calibrated size.
        ra_scale = max(math.hypot(*cal.ra) * self._prior_fraction, 1e-9)
        dec_scale = max(math.hypot(*cal.dec) * self._prior_fraction, 1e-9)
        priors = [
            (4, cal.ra[0], ra_scale),
            (5, cal.ra[1], ra_scale),
            (6, cal.dec[0], dec_scale),
            (7, cal.dec[1], dec_scale),
        ]
        period = self.worm_period_s
        worm_on = period is not None and period > 0 and math.hypot(*cal.ra) > 0
        if worm_on:
            assert period is not None
            phase = 2 * math.pi * rows[:, 0] / period
            phase_now = 2 * math.pi * t_now / period
            # Zero at the newest frame, like every other column, so the
            # start stays the position now.
            sine = np.sin(phase) - math.sin(phase_now)
            cosine = np.cos(phase) - math.cos(phase_now)
            columns_x += [cal.ra[0] * sine, cal.ra[0] * cosine]
            columns_y += [cal.ra[1] * sine, cal.ra[1] * cosine]
            worm_scale = max(self.worm_prior_px / math.hypot(*cal.ra), 1e-9)
            priors += [(8, 0.0, worm_scale), (9, 0.0, worm_scale)]
        design_x = np.column_stack(columns_x)
        design_y = np.column_stack(columns_y)
        values_x, values_y = rows[:, 1], rows[:, 2]

        # Two passes: how much each frame counts against the prior depends
        # on the scatter, which the first pass estimates.
        noise = (0.3, 0.3)
        solved = None
        for _ in range(2):
            solved = _solve(design_x, design_y, values_x, values_y, noise, priors)
            if solved is None:
                return None
            _, _, residual_x, residual_y = solved
            dof = max(count - design_x.shape[1] / 2, 1.0)
            noise = (
                max(math.sqrt(float(residual_x @ residual_x) / dof), 0.02),
                max(math.sqrt(float(residual_y @ residual_y) / dof), 0.02),
            )
        assert solved is not None
        coef, covariance, residual_x, residual_y = solved

        # The fit's value now is smooth - a few minutes of frames behind
        # it - and so slow to show anything it does not model: a gust, a
        # cable snagging, a worm harmonic. Where the last few frames sit
        # off the fit is that, averaged over enough frames to calm the
        # seeing and few enough to see it within seconds.
        recent = min(self._recent_frames, count)
        position = (float(coef[0]), float(coef[1]))
        if recent > 0:
            position = (
                position[0] + _beyond_seeing(residual_x[-recent:], noise[0], self._recent_threshold),
                position[1] + _beyond_seeing(residual_y[-recent:], noise[1], self._recent_threshold),
            )
        worm = None
        if worm_on:
            assert period is not None
            worm = Worm(
                period_s=period,
                sin_units=float(coef[8]),
                cos_units=float(coef[9]),
                direction=cal.ra,
                amplitude_error=math.sqrt(max((covariance[8, 8] + covariance[9, 9]) / 2, 0.0)),
            )
        return PixelFit(
            position=position,
            drift=(float(coef[2]), float(coef[3])),
            drift_error=(_sd(covariance, 2), _sd(covariance, 3)),
            response=Response(ra=(float(coef[4]), float(coef[5])), dec=(float(coef[6]), float(coef[7]))),
            response_error=Response(
                ra=(_sd(covariance, 4), _sd(covariance, 5)),
                dec=(_sd(covariance, 6), _sd(covariance, 7)),
            ),
            scatter=math.sqrt((noise[0] ** 2 + noise[1] ** 2) / 2),
            frames=count,
            span_s=span,
            time=t_now,
            worm=worm,
        )


def _solve(design_x, design_y, values_x, values_y, noise, priors):
    """Least squares for both sensor axes at once, with the priors: minimises

        sum((x - model_x) / noise_x)^2 + sum((y - model_y) / noise_y)^2
        + sum(((parameter - prior) / width)^2)

    Both axes in one solve because the worm's two terms are shared: it is
    one turn of the RA axis, seen in x and y.
    """
    width = design_x.shape[1]
    prior_rows = np.zeros((len(priors), width))
    prior_target = np.zeros(len(priors))
    for row, (index, mean, scale) in enumerate(priors):
        prior_rows[row, index] = 1.0 / scale
        prior_target[row] = mean / scale
    a = np.vstack([design_x / noise[0], design_y / noise[1], prior_rows])
    b = np.concatenate([values_x / noise[0], values_y / noise[1], prior_target])
    try:
        # Covariance in real units: the rows were scaled by the noise.
        covariance = np.linalg.inv(a.T @ a)
    except np.linalg.LinAlgError:
        return None
    coef = covariance @ (a.T @ b)
    return coef, covariance, values_x - design_x @ coef, values_y - design_y @ coef


def _beyond_seeing(residuals, noise: float, threshold: float) -> float:
    """How far recent frames sit off the fit, less what seeing explains.

    Their mean, shrunk towards zero by `threshold` standard errors: frames
    scattered by seeing alone give nothing, so the loop does not chase
    it, while a real move - the fit lagging something it does not model -
    comes through, less that margin.
    """
    mean = float(residuals.mean())
    margin = threshold * noise / math.sqrt(len(residuals))
    return math.copysign(max(abs(mean) - margin, 0.0), mean)


def _sd(covariance, index: int) -> float:
    return math.sqrt(max(float(covariance[index, index]), 0.0))
