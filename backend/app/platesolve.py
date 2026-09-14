from __future__ import annotations

from typing import Protocol

from .camera import Frame
from .mount import RaDec

# Real solvers (ASTAP) aren't pixel-perfect - simulate their residual error.
SOLVE_NOISE_DEG = 0.02


class PlateSolver(Protocol):
    def solve(self, frame: Frame) -> RaDec | None: ...


class MockPlateSolver:
    """Stands in for ASTAP. Real plate-solving can fail to find a match
    (too few stars, cloud, wrong exposure); this always succeeds, which
    is a simplification worth revisiting once real solving is wired in.
    """

    def solve(self, frame: Frame) -> RaDec | None:
        return RaDec(
            ra_deg=frame.true_position.ra_deg + SOLVE_NOISE_DEG,
            dec_deg=frame.true_position.dec_deg - SOLVE_NOISE_DEG,
        )
