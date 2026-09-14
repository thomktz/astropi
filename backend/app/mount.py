from dataclasses import dataclass
from typing import Protocol


@dataclass
class RaDec:
    ra_deg: float
    dec_deg: float


PARK_POSITION = RaDec(ra_deg=0.0, dec_deg=90.0)


class Mount(Protocol):
    def goto(self, target: RaDec) -> None: ...
    def sync(self, actual: RaDec) -> None: ...
    def position(self) -> RaDec: ...
    def park(self) -> None: ...


class MockMount:
    """Stands in for a real mount driver (pysynscan or INDI) until the
    USB cable arrives. Simulates imperfect alignment: commanding a GoTo
    doesn't land exactly on target, mimicking real-world polar alignment
    and backlash error, which is what the plate-solve loop corrects for.
    """

    def __init__(self, mechanical_bias: RaDec = RaDec(ra_deg=0.4, dec_deg=-0.25)):
        # Fixed, unknown-to-the-driver error (imperfect polar alignment,
        # backlash). `_compensation` is what sync() has learned so far to
        # cancel it out - starts at zero, i.e. uncalibrated.
        self._mechanical_bias = mechanical_bias
        self._compensation = RaDec(ra_deg=0.0, dec_deg=0.0)
        self._position = RaDec(ra_deg=0.0, dec_deg=90.0)
        self._commanded = RaDec(ra_deg=0.0, dec_deg=90.0)

    def goto(self, target: RaDec) -> None:
        self._commanded = target
        self._position = RaDec(
            ra_deg=target.ra_deg + self._mechanical_bias.ra_deg - self._compensation.ra_deg,
            dec_deg=target.dec_deg + self._mechanical_bias.dec_deg - self._compensation.dec_deg,
        )

    def sync(self, actual: RaDec) -> None:
        # Tell the mount "you are actually here" - folds the observed
        # residual (actual vs. what was commanded) into the compensation,
        # so the *next* goto lands closer to its target.
        self._compensation = RaDec(
            ra_deg=self._compensation.ra_deg + (actual.ra_deg - self._commanded.ra_deg),
            dec_deg=self._compensation.dec_deg + (actual.dec_deg - self._commanded.dec_deg),
        )
        self._position = actual

    def position(self) -> RaDec:
        return self._position

    def park(self) -> None:
        self._commanded = PARK_POSITION
        self._position = PARK_POSITION
