from dataclasses import dataclass, field

from .camera import Camera
from .mount import Mount, RaDec
from .platesolve import PlateSolver

TOLERANCE_DEG = 0.05
MAX_ITERATIONS = 6


@dataclass
class AlignStep:
    solved: RaDec
    error_deg: float


@dataclass
class AlignResult:
    converged: bool
    final_position: RaDec
    steps: list[AlignStep] = field(default_factory=list)


def _distance_deg(a: RaDec, b: RaDec) -> float:
    return ((a.ra_deg - b.ra_deg) ** 2 + (a.dec_deg - b.dec_deg) ** 2) ** 0.5


def goto_and_align(target: RaDec, mount: Mount, camera: Camera, solver: PlateSolver) -> AlignResult:
    """GoTo a target, then iteratively plate-solve and correct until the
    solved position is within tolerance of the target - replaces manual
    star-centering alignment with a closed feedback loop.
    """
    mount.goto(target)
    steps: list[AlignStep] = []

    for _ in range(MAX_ITERATIONS):
        frame = camera.capture()
        solved = solver.solve(frame)
        if solved is None:
            # Solve failed (e.g. too few stars) - stop rather than guess.
            return AlignResult(converged=False, final_position=mount.position(), steps=steps)

        error = _distance_deg(solved, target)
        steps.append(AlignStep(solved=solved, error_deg=error))

        if error <= TOLERANCE_DEG:
            mount.sync(solved)
            return AlignResult(converged=True, final_position=solved, steps=steps)

        # Sync to what we actually see, then re-issue the goto - the
        # corrected systemic error now points the next attempt closer.
        mount.sync(solved)
        mount.goto(target)

    return AlignResult(converged=False, final_position=mount.position(), steps=steps)
