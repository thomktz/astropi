"""Mount contract.

Written in equatorial coordinates throughout. Mounts that think in motor-axis
angles (the Star Adventurer GTi speaks Sky-Watcher's raw axis protocol) do
that conversion inside their own backend, so nothing above this line needs to
know about mount geometry, hemispheres or gear ratios.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from astropi.core.geometry import AltAz, RaDec
from astropi.devices.base import Device


class MountState(StrEnum):
    IDLE = "idle"
    SLEWING = "slewing"
    TRACKING = "tracking"
    PARKED = "parked"
    ERROR = "error"


class TrackingRate(StrEnum):
    SIDEREAL = "sidereal"
    LUNAR = "lunar"
    SOLAR = "solar"
    KING = "king"


class PierSide(StrEnum):
    EAST = "east"
    WEST = "west"
    UNKNOWN = "unknown"


class GuideDirection(StrEnum):
    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"


@dataclass(frozen=True, slots=True)
class MountStatus:
    state: MountState
    position: RaDec
    horizontal: AltAz | None = None
    target: RaDec | None = None
    tracking: bool = False
    tracking_rate: TrackingRate = TrackingRate.SIDEREAL
    pier_side: PierSide = PierSide.UNKNOWN
    slewing: bool = False
    #: Why the mount halted itself and is refusing to move, if it has.
    #: A mount that can detect a runaway reports it here with the state
    #: set to ERROR, and offers `clear_fault()` for the operator.
    fault: str | None = None


@runtime_checkable
class Mount(Device, Protocol):
    """A telescope mount."""

    async def status(self) -> MountStatus: ...

    async def slew_to(self, target: RaDec) -> None:
        """Begin slewing. Returns once the move is *commanded*, not finished.

        Callers that need completion await `wait_for_slew`. Keeping the two
        apart lets a sequence abort or report progress during a long slew
        instead of blocking on a single opaque call.
        """
        ...

    async def wait_for_slew(self, *, timeout_s: float = 120.0) -> None: ...

    async def sync_to(self, actual: RaDec) -> None:
        """Tell the mount it is actually pointing at `actual`.

        This is how a plate solve feeds back into the pointing model: the
        solved position is ground truth, and the mount's own idea of where
        it is gets corrected to match.
        """
        ...

    async def abort_slew(self) -> None: ...

    async def set_tracking(self, enabled: bool, rate: TrackingRate = TrackingRate.SIDEREAL) -> None: ...

    async def park(self) -> None: ...

    async def unpark(self) -> None: ...

    async def move_by(self, direction: GuideDirection, degrees: float) -> None:
        """Move one axis by a fixed angle, from wherever it is.

        Framing, not guiding. `pulse_guide` is a correction measured in
        milliseconds at the guide rate, which is the right primitive for a
        closed loop and a useless one for a person trying to put something
        in the frame: a full minute of it moves an eighth of a degree.

        Returns once the move is done - these are seconds, not minutes.
        """
        ...

    # Finer guiding controls, for mounts with `Capability.GUIDE_RATE_OFFSET`
    # and `Capability.AXIS_STEPS`. A guider uses them instead of pulses
    # when both are there:
    #
    #     async def set_ra_rate_offset(self, arcsec_per_s: float) -> float
    #         Run the RA axis this much faster (positive - westward, as a
    #         west pulse) than its tracking rate, until changed; zero is
    #         plain tracking. In arcsec of axis rotation per second. Returns
    #         the offset actually applied, which a mount stepping in whole
    #         timer ticks can only approximate.
    #
    #     async def step_dec(self, direction: GuideDirection, steps: int) -> int
    #         Move declination north or south by exactly this many motor
    #         steps, and return how many the counters say it moved.
    #
    #     dec_step_arcsec: float
    #         One declination step, in arcsec of axis rotation.
    #
    # For a mount that can be told where it is standing:
    #
    #     async def set_home(self) -> None
    #         The mount is physically at home - counterweight down, the
    #         telescope on the pole. Forget where it thought it pointed,
    #         move nothing, and end parked.
    #
    # And, for any mount that knows it:
    #
    #     worm_period_s: float | None
    #         Seconds the RA worm takes to turn once at sidereal rate - the
    #         period of its periodic error, which a guider can then fit and
    #         cancel. `None` when the mount cannot say.

    async def pulse_guide(self, direction: GuideDirection, duration_ms: int) -> None:
        """Nudge the mount for a fixed duration - the guiding primitive.

        Expressed in milliseconds of motor time rather than arcseconds
        because that is what every mount protocol actually accepts; turning
        an angular error into a pulse duration is the guider's calibration
        job, not the mount's.
        """
        ...
