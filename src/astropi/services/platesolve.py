"""Plate solving: turning a frame into a known sky position.

The protocol is what the rest of the application depends on. Three
implementations sit behind it:

* `SimulatedPlateSolver`, which reads the truth the simulator recorded and
  degrades it realistically, so the centring loop can be developed offline;
* `AstapSolver`, a subprocess wrapper around ASTAP - the intended field
  solver, because it works offline from local star indexes and runs on ARM;
* `AstrometryNetSolver`, the web service, as a fallback when there is
  internet and no local index.

A solve takes a *hint* wherever possible. Blind solving searches the entire
sky and can take minutes; told roughly where to look and at what image
scale, ASTAP usually answers in under a second. Since the mount always has
some idea where it is pointing, there is rarely a reason to solve blind.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import shutil
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from astropi.core.errors import SolveFailedError
from astropi.core.events import EventBus, Topic
from astropi.core.geometry import RaDec, normalize_deg
from astropi.devices.camera import Frame
from astropi.services.stardetect import detect_stars

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SolveHint:
    """What we already believe, to keep the search local."""

    center: RaDec | None = None
    radius_deg: float = 10.0
    pixel_scale_arcsec: float | None = None
    scale_tolerance: float = 0.2


@dataclass(frozen=True, slots=True)
class SolveResult:
    center: RaDec
    pixel_scale_arcsec: float
    rotation_deg: float
    flipped: bool
    stars_detected: int
    solve_time_s: float
    solver: str

    @property
    def field_rotation_deg(self) -> float:
        """Camera angle east of north, normalised to [0, 360)."""
        return normalize_deg(self.rotation_deg)


@runtime_checkable
class PlateSolver(Protocol):
    @property
    def name(self) -> str: ...

    async def available(self) -> bool: ...

    async def solve(self, frame: Frame, hint: SolveHint | None = None) -> SolveResult:
        """Return the frame's true centre, or raise `SolveFailedError`."""
        ...


class SimulatedPlateSolver:
    """Solves by reading the ground truth the simulated camera recorded.

    Not cheating so much as standing in for optics: the simulator already
    knows where it rendered, and re-deriving that from the pixels would be
    reimplementing ASTAP to no benefit. What it does model is the part that
    affects the code above it - solves are not exact, they take time, and
    they fail on frames with too few stars, which is precisely the behaviour
    a centring loop has to cope with.
    """

    #: Residual error of a real solve, in arcseconds.
    ACCURACY_ARCSEC = 2.5
    #: Frames with fewer stars than this cannot be solved.
    MIN_STARS = 8

    def __init__(self, *, seed: int | None = None, duration_s: float = 0.4) -> None:
        self._random = random.Random(seed)
        self._duration_s = duration_s

    @property
    def name(self) -> str:
        return "simulator"

    async def available(self) -> bool:
        return True

    async def solve(self, frame: Frame, hint: SolveHint | None = None) -> SolveResult:
        started = time.monotonic()
        stars = await asyncio.to_thread(
            detect_stars, frame.data, max_stars=60, bit_depth=frame.sensor.bit_depth
        )
        await asyncio.sleep(self._duration_s)

        if len(stars) < self.MIN_STARS:
            raise SolveFailedError(
                f"only {len(stars)} stars detected, need {self.MIN_STARS} - "
                "too short an exposure, clouds, or badly out of focus"
            )

        true_ra = frame.metadata.get("sim_true_ra_deg")
        true_dec = frame.metadata.get("sim_true_dec_deg")
        if true_ra is None or true_dec is None:
            raise SolveFailedError("frame carries no simulated pointing; not a simulated frame")

        jitter_deg = self.ACCURACY_ARCSEC / 3600.0
        dec = max(-90.0, min(90.0, true_dec + self._random.gauss(0.0, jitter_deg)))
        # An RA error is a smaller angle on sky the closer you are to a pole,
        # so scale it by 1/cos(dec) to keep the on-sky residual constant.
        ra_jitter = self._random.gauss(0.0, jitter_deg) / max(math.cos(math.radians(dec)), 0.02)

        return SolveResult(
            center=RaDec(normalize_deg(true_ra + ra_jitter), dec),
            pixel_scale_arcsec=float(frame.metadata.get("pixel_scale_arcsec", 1.0)),
            rotation_deg=float(frame.metadata.get("rotation_deg", 0.0)),
            flipped=False,
            stars_detected=len(stars),
            solve_time_s=time.monotonic() - started,
            solver=self.name,
        )


class AstapSolver:
    """ASTAP, run as a subprocess against local star index files.

    The intended solver in the field: no internet, no upload, and fast
    enough on a Pi to sit inside a centring loop. Requires the `astap`
    binary and an index (H17/H18) installed, so `available()` is checked
    before it is offered rather than failing at the worst moment.
    """

    def __init__(self, binary: str = "astap", *, timeout_s: float = 60.0) -> None:
        self._binary = binary
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return "astap"

    async def available(self) -> bool:
        return shutil.which(self._binary) is not None

    async def solve(self, frame: Frame, hint: SolveHint | None = None) -> SolveResult:
        raise SolveFailedError(
            "ASTAP backend is not wired up yet. It needs the frame written to "
            "FITS, `astap -f <file> -ra <h> -spd <deg> -r <deg> -fov <deg>` run "
            "as a subprocess, and the resulting .wcs file parsed back into a "
            "SolveResult. Use the simulator solver until hardware is connected."
        )


class AstrometryNetSolver:
    """astrometry.net's web service: a blind-solve fallback with internet."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "astrometry.net"

    async def available(self) -> bool:
        return self._api_key is not None

    async def solve(self, frame: Frame, hint: SolveHint | None = None) -> SolveResult:
        raise SolveFailedError(
            "astrometry.net backend is not wired up yet: it needs an API key, "
            "a frame upload and submission polling. Prefer ASTAP in the field - "
            "an upload per solve is not workable on a dark-site connection."
        )


class PlateSolveService:
    """Picks a working solver and reports every solve on the event bus."""

    def __init__(self, solvers: list[PlateSolver], events: EventBus) -> None:
        if not solvers:
            raise ValueError("at least one solver is required")
        self._solvers = solvers
        self._events = events

    async def preferred(self) -> PlateSolver:
        """First available solver, in the order they were configured."""
        for solver in self._solvers:
            if await solver.available():
                return solver
        raise SolveFailedError("no plate solver is available")

    async def solve(self, frame: Frame, hint: SolveHint | None = None) -> SolveResult:
        solver = await self.preferred()
        try:
            result = await solver.solve(frame, hint)
        except SolveFailedError as error:
            self._events.publish(Topic.SOLVE_RESULT, success=False, solver=solver.name, error=str(error))
            raise

        self._events.publish(
            Topic.SOLVE_RESULT,
            success=True,
            solver=result.solver,
            ra_deg=result.center.ra_deg,
            dec_deg=result.center.dec_deg,
            pixel_scale_arcsec=result.pixel_scale_arcsec,
            rotation_deg=result.field_rotation_deg,
            stars_detected=result.stars_detected,
            solve_time_s=round(result.solve_time_s, 3),
        )
        return result
