"""Session plans: an ordered list of targets and what to shoot on each.

A plan is built before the night starts and then run unattended, so the
useful work happens at planning time, not at run time. Two questions have
to be answered while it is still being edited:

  * how long will this take, and
  * will it actually work?

The second is the one worth having a computer for. A block is only
shootable if its target is high enough for the whole slot, and a plan is
only realistic if it finishes before dawn. Both follow from the ephemeris,
so the planner checks them rather than leaving an operator to discover at
three in the morning that the third target set an hour ago.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from astropi.core.geometry import RaDec
from astropi.core.timekeeping import hour_angle_deg
from astropi.services.ephemeris import EphemerisService

#: Seconds of overhead per frame: sensor readout, download, and the pause
#: before the next exposure starts.
FRAME_OVERHEAD_S = 4.0
#: Allowance for slewing and plate-solve centring at the start of a block.
CENTRING_S = 90.0
#: Allowance for an autofocus run.
AUTOFOCUS_S = 150.0
#: Allowance for a dither and its settle.
DITHER_S = 15.0

#: A block is flagged if its target drops below this at any point in it.
MIN_ALTITUDE_DEG = 20.0
#: ...and noted, less severely, below this.
COMFORTABLE_ALTITUDE_DEG = 30.0


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    PROBLEM = "problem"


@dataclass(frozen=True, slots=True)
class PlanIssue:
    severity: Severity
    message: str


@dataclass(slots=True)
class PlanBlock:
    """One target, and what to shoot on it."""

    target_id: str | None
    target_name: str
    coord: RaDec
    frames: int
    exposure_s: float
    gain: int | None = None
    offset: int | None = None
    binning: int = 1
    dither_every: int = 3
    #: Slew and plate-solve centre before shooting. Off for a block that
    #: continues on the target the previous one just centred.
    center: bool = True
    autofocus: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def integration_s(self) -> float:
        """Shutter-open time. What actually ends up in the stack."""
        return self.frames * self.exposure_s

    @property
    def duration_s(self) -> float:
        """Wall-clock time, including everything that is not integration."""
        total = self.frames * (self.exposure_s + FRAME_OVERHEAD_S)
        if self.center:
            total += CENTRING_S
        if self.autofocus:
            total += AUTOFOCUS_S
        if self.dither_every > 0:
            total += (self.frames // self.dither_every) * DITHER_S
        return total


@dataclass(slots=True)
class SessionPlan:
    name: str
    blocks: list[PlanBlock] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = 0.0

    @property
    def duration_s(self) -> float:
        return sum(block.duration_s for block in self.blocks)

    @property
    def integration_s(self) -> float:
        return sum(block.integration_s for block in self.blocks)


@dataclass(frozen=True, slots=True)
class BlockSchedule:
    """A block placed on the clock, with whatever is wrong with it."""

    block: PlanBlock
    starts_at: datetime
    ends_at: datetime
    min_altitude_deg: float
    max_altitude_deg: float
    crosses_meridian: bool
    issues: list[PlanIssue]


@dataclass(frozen=True, slots=True)
class PlanSchedule:
    starts_at: datetime
    ends_at: datetime
    blocks: list[BlockSchedule]
    issues: list[PlanIssue]

    @property
    def duration_s(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds()


class PlannerService:
    """Puts a plan on the clock and says what is wrong with it."""

    #: Altitude is sampled this many times across a block. Three - start,
    #: middle, end - is not enough: a target can be comfortably up at both
    #: ends of a long block and still clip the horizon in between.
    SAMPLES_PER_BLOCK = 9

    def __init__(self, ephemeris: EphemerisService) -> None:
        self._ephemeris = ephemeris

    def schedule(self, plan: SessionPlan, *, start: datetime | None = None) -> PlanSchedule:
        """Lay the blocks out in order and check each one.

        Starts at dusk when the plan is built in daylight, which is the
        normal case - planning happens over breakfast or in the afternoon,
        and estimating from "now" would put every block in the wrong part
        of the sky, or in broad daylight.
        """
        begin = start or datetime.now(UTC)
        night = self._ephemeris.night_window(begin)
        if night.astronomical_dawn and begin > night.astronomical_dawn:
            # The night that window describes is already over - it is the
            # morning after. Planning at breakfast is planning for tonight,
            # not for the next eight hours of daylight.
            night = self._ephemeris.night_window(begin + timedelta(hours=12))
        if night.astronomical_dusk and begin < night.astronomical_dusk:
            begin = night.astronomical_dusk

        cursor = begin
        scheduled: list[BlockSchedule] = []
        for block in plan.blocks:
            ends = cursor + timedelta(seconds=block.duration_s)
            scheduled.append(self._check_block(block, cursor, ends, night.astronomical_dawn))
            cursor = ends

        issues: list[PlanIssue] = []
        if not plan.blocks:
            issues.append(PlanIssue(Severity.INFO, "Plan is empty."))
        elif night.astronomical_dawn and cursor > night.astronomical_dawn:
            over = (cursor - night.astronomical_dawn).total_seconds() / 3600.0
            issues.append(
                PlanIssue(
                    Severity.WARNING,
                    f"Plan runs {over:.1f}h past astronomical dawn.",
                )
            )

        return PlanSchedule(starts_at=begin, ends_at=cursor, blocks=scheduled, issues=issues)

    def _check_block(
        self,
        block: PlanBlock,
        starts_at: datetime,
        ends_at: datetime,
        dawn: datetime | None,
    ) -> BlockSchedule:
        altitudes: list[float] = []
        hour_angles: list[float] = []
        span = (ends_at - starts_at).total_seconds()

        for index in range(self.SAMPLES_PER_BLOCK):
            moment = starts_at + timedelta(seconds=span * index / (self.SAMPLES_PER_BLOCK - 1))
            altitude, _ = self._ephemeris.altaz_now(block.coord, moment)
            altitudes.append(altitude)
            hour_angles.append(
                hour_angle_deg(
                    block.coord.ra_deg,
                    self._ephemeris.site.longitude_deg,
                    moment.timestamp(),
                )
            )

        lowest = min(altitudes)
        highest = max(altitudes)
        # A sign change in hour angle is the meridian crossing. Guarded
        # against the wrap at +/-180, which is the anti-meridian and not
        # the same event at all.
        crosses = any(
            first < 0 <= second and abs(second - first) < 180
            for first, second in pairwise(hour_angles)
        )

        issues: list[PlanIssue] = []
        if lowest < 0:
            issues.append(
                PlanIssue(Severity.PROBLEM, "Target is below the horizon for part of this block.")
            )
        elif lowest < MIN_ALTITUDE_DEG:
            issues.append(
                PlanIssue(
                    Severity.PROBLEM,
                    f"Drops to {lowest:.0f}° - below {MIN_ALTITUDE_DEG:.0f}° is a lot of atmosphere.",
                )
            )
        elif lowest < COMFORTABLE_ALTITUDE_DEG:
            issues.append(PlanIssue(Severity.WARNING, f"Gets down to {lowest:.0f}°."))

        if crosses:
            issues.append(
                PlanIssue(Severity.WARNING, "Crosses the meridian during this block.")
            )
        if dawn and ends_at > dawn:
            issues.append(PlanIssue(Severity.WARNING, "Finishes after astronomical dawn."))

        return BlockSchedule(
            block=block,
            starts_at=starts_at,
            ends_at=ends_at,
            min_altitude_deg=lowest,
            max_altitude_deg=highest,
            crosses_meridian=crosses,
            issues=issues,
        )
