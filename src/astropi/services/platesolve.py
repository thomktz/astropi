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
import os
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from astropi.core.errors import SolveFailedError
from astropi.core.events import EventBus, Topic
from astropi.core.geometry import RaDec, normalize_deg
from astropi.devices.camera import Frame
from astropi.services.stardetect import detect_stars

logger = logging.getLogger(__name__)

#: A 2x2 matrix, row by row.
Matrix2 = tuple[tuple[float, float], tuple[float, float]]


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
    #: The solve's linear WCS, when the solver produced one: the pixel the
    #: centre sits on (0-based column, row of the frame as captured) and
    #: the matrix taking a pixel offset from it to tangent-plane degrees,
    #: east and north - FITS `CRPIX` and `CD`, in numpy's orientation.
    #: What lets a point on the sky be drawn on the frame it was solved
    #: from, which the centre and a rotation alone cannot do once the
    #: image may be mirrored.
    reference_pixel: tuple[float, float] | None = None
    cd: Matrix2 | None = None

    @property
    def field_rotation_deg(self) -> float:
        """Camera angle east of north, normalised to [0, 360)."""
        return normalize_deg(self.rotation_deg)

    def pixel_of(self, coord: RaDec) -> tuple[float, float] | None:
        """Where a sky position falls on the solved frame, in pixels.

        Off the frame is fine - an arrow pointing at somewhere outside the
        field is still the right arrow. `None` without a WCS, or for a
        point more than 90 degrees away, which has no tangent-plane image.
        """
        if self.reference_pixel is None or self.cd is None:
            return None
        ra, dec = math.radians(coord.ra_deg), math.radians(coord.dec_deg)
        ra0, dec0 = math.radians(self.center.ra_deg), math.radians(self.center.dec_deg)
        denominator = math.sin(dec0) * math.sin(dec) + math.cos(dec0) * math.cos(dec) * math.cos(ra - ra0)
        if denominator <= 0:
            return None
        xi = math.degrees(math.cos(dec) * math.sin(ra - ra0) / denominator)
        north = math.cos(dec0) * math.sin(dec) - math.sin(dec0) * math.cos(dec) * math.cos(ra - ra0)
        eta = math.degrees(north / denominator)
        (a, b), (c, d) = self.cd
        determinant = a * d - b * c
        if determinant == 0:
            return None
        dx = (d * xi - b * eta) / determinant
        dy = (-c * xi + a * eta) / determinant
        return self.reference_pixel[0] + dx, self.reference_pixel[1] + dy


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

        scale_arcsec = float(frame.metadata.get("pixel_scale_arcsec", 1.0))
        rotation_deg = float(frame.metadata.get("rotation_deg", 0.0))
        return SolveResult(
            center=RaDec(normalize_deg(true_ra + ra_jitter), dec),
            pixel_scale_arcsec=scale_arcsec,
            rotation_deg=rotation_deg,
            flipped=False,
            stars_detected=len(stars),
            solve_time_s=time.monotonic() - started,
            solver=self.name,
            reference_pixel=(frame.shape[1] / 2.0, frame.shape[0] / 2.0),
            cd=_simulated_cd(scale_arcsec, rotation_deg),
        )


def _simulated_cd(scale_arcsec: float, rotation_deg: float) -> Matrix2:
    """The CD matrix of the simulator's own projection (`sky.project`).

    That projection puts east to the left and turns by the camera angle;
    the matrix doing both is its own inverse, so it serves either way.
    """
    theta = math.radians(rotation_deg)
    scale = scale_arcsec / 3600.0
    return (
        (-math.cos(theta) * scale, -math.sin(theta) * scale),
        (-math.sin(theta) * scale, math.cos(theta) * scale),
    )


class AstapSolver:
    """ASTAP, run as a subprocess against local star index files.

    The intended solver in the field: no internet, no upload, and fast
    enough on a Pi to sit inside a centring loop. Requires the `astap` (or
    command-line only `astap_cli`) binary and a star database installed, so
    `available()` is checked before it is offered rather than failing at
    the worst moment.

    The frame is binned before it is written. A full ASI2600 frame is 52 MB
    of FITS on an SD card per solve, and ASTAP would only bin it again on
    the way in; binning here also merges each Bayer cell into one
    luminance pixel, so a colour sensor solves on stars rather than on a
    checkerboard.
    """

    #: Longest side of the image ASTAP is given, after binning.
    SOLVE_DIMENSION = 2048
    #: Where `astap_cli` lands when unpacked without root - the second is
    #: the rig's own install, a .deb extracted into `~/astap`.
    FALLBACK_LOCATIONS = (
        "astap_cli",
        "~/astap/opt/astap/astap_cli",
        "~/astap/astap_cli",
        "/opt/astap/astap_cli",
        "/opt/astap/astap",
    )

    def __init__(
        self,
        binary: str = "astap",
        *,
        timeout_s: float = 60.0,
        database_dir: str | None = None,
        blind_fallback: bool = True,
    ) -> None:
        self._binary = binary
        self._timeout_s = timeout_s
        self._database_dir = database_dir
        self._blind_fallback = blind_fallback

    @property
    def name(self) -> str:
        return "astap"

    def _resolve(self) -> str | None:
        for candidate in (self._binary, *self.FALLBACK_LOCATIONS):
            found = shutil.which(candidate)
            if found:
                return found
            expanded = os.path.expanduser(candidate)
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
        return None

    async def available(self) -> bool:
        return self._resolve() is not None

    def _database(self, binary: str) -> str | None:
        """The star database, if not where ASTAP looks by default.

        Unpacked without root, the database sits beside the binary, which
        ASTAP does not search on its own.
        """
        if self._database_dir:
            return os.path.expanduser(self._database_dir)
        beside = Path(binary).parent
        if any(beside.glob("*.1476")) or any(beside.glob("*.290")):
            return str(beside)
        return None

    async def solve(self, frame: Frame, hint: SolveHint | None = None) -> SolveResult:
        binary = self._resolve()
        if binary is None:
            raise SolveFailedError("ASTAP is not installed - see docs/hardware.md")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="astropi-astap-") as directory:
            path = Path(directory) / "frame.fits"
            factor, binned_height = await asyncio.to_thread(
                _write_solve_fits, frame, path, self.SOLVE_DIMENSION
            )
            try:
                fields = await self._run(binary, path, hint, factor, binned_height)
            except SolveFailedError:
                # The mount's idea of where it points can be far off before
                # the first sync - an unaligned rig at dusk is the usual
                # case - so a local search that finds nothing earns one
                # look at the whole sky before giving up.
                if not self._blind_fallback or hint is None or hint.center is None:
                    raise
                logger.info("ASTAP found nothing near the hint; retrying blind")
                fields = await self._run(binary, path, SolveHint(radius_deg=180.0), factor, binned_height)
        return _astap_result(fields, factor, time.monotonic() - started)

    async def _run(
        self, binary: str, path: Path, hint: SolveHint | None, factor: int, binned_height: int
    ) -> dict[str, str]:
        args = [binary, "-f", str(path)]
        scale = hint.pixel_scale_arcsec if hint else None
        # Field height of the image ASTAP actually sees; 0 asks it to find
        # the scale itself, which is slower but needs no optics configured.
        args += ["-fov", f"{scale * factor * binned_height / 3600.0:.4f}" if scale else "0"]
        if hint is not None and hint.center is not None:
            args += [
                "-ra", f"{hint.center.ra_deg / 15.0:.6f}",
                "-spd", f"{hint.center.dec_deg + 90.0:.6f}",
                "-r", f"{min(hint.radius_deg, 180.0):.2f}",
            ]  # fmt: skip
        else:
            args += ["-r", "180"]
        database = self._database(binary)
        if database:
            args += ["-d", database]

        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout_s)
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise SolveFailedError(f"ASTAP gave up after {self._timeout_s:g}s") from error

        fields = _read_astap_ini(path.with_suffix(".ini"))
        # The result file carries no star count; ASTAP only prints it.
        counts = re.findall(r"(\d+) stars, \d+ quads selected in the image", output.decode(errors="replace"))
        if counts:
            fields["STARS"] = counts[-1]
        if fields.get("PLTSOLVD", "F").upper() != "T":
            reason = fields.get("ERROR") or fields.get("WARNING") or output.decode(errors="replace").strip()
            raise SolveFailedError(f"ASTAP could not solve the frame: {reason or 'no match'}")
        return fields


def _write_solve_fits(frame: Frame, path: Path, dimension: int) -> tuple[int, int]:
    """Bin the frame and write it as a FITS file; return the factor and height."""
    from astropy.io import fits

    data = frame.data
    height, width = data.shape[:2]
    factor = max(2 if frame.sensor.has_color_filter_array else 1, math.ceil(max(height, width) / dimension))
    rows, columns = height // factor, width // factor
    binned = (
        data[: rows * factor, : columns * factor]
        .astype(np.float32)
        .reshape(rows, factor, columns, factor)
        .mean(axis=(1, 3))
    )
    # Row 0 stays FITS row 1, so the WCS that comes back is already in the
    # orientation of the array everything else here indexes.
    fits.PrimaryHDU(np.clip(binned, 0, 65535).astype(np.uint16)).writeto(path, overwrite=True)
    return factor, rows


def _read_astap_ini(path: Path) -> dict[str, str]:
    """ASTAP's result file: one KEY=VALUE per line."""
    fields: dict[str, str] = {}
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return fields
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip().upper()] = value.strip().strip("'").strip()
    return fields


def _astap_result(fields: dict[str, str], factor: int, elapsed_s: float) -> SolveResult:
    """Turn ASTAP's fields, for the binned image, into a full-frame result."""
    try:
        crval = (float(fields["CRVAL1"]), float(fields["CRVAL2"]))
        crpix = (float(fields["CRPIX1"]), float(fields["CRPIX2"]))
        cd = (
            (float(fields["CD1_1"]) / factor, float(fields["CD1_2"]) / factor),
            (float(fields["CD2_1"]) / factor, float(fields["CD2_2"]) / factor),
        )
    except (KeyError, ValueError) as error:
        raise SolveFailedError(f"ASTAP reported a solve but no usable WCS ({error})") from error

    # FITS pixels are 1-based and centred on integers; a binned pixel
    # covers `factor` full ones, so its centre is half a bin in.
    reference = ((crpix[0] - 0.5) * factor - 0.5, (crpix[1] - 0.5) * factor - 0.5)
    determinant = cd[0][0] * cd[1][1] - cd[0][1] * cd[1][0]
    rotation = float(fields.get("CROTA2", math.degrees(math.atan2(cd[1][0], cd[1][1]))))
    return SolveResult(
        center=RaDec(normalize_deg(crval[0]), crval[1]),
        pixel_scale_arcsec=math.sqrt(abs(determinant)) * 3600.0,
        rotation_deg=rotation,
        # A sky image seen from the front has east to the left of north,
        # which in FITS's y-up convention is a negative determinant.
        flipped=determinant > 0,
        stars_detected=int(float(fields.get("STARS", fields.get("NSTARS", "0")) or 0)),
        solve_time_s=elapsed_s,
        solver="astap",
        reference_pixel=reference,
        cd=cd,
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
