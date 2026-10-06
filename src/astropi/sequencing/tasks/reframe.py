"""Resume a target: put the camera back where an earlier frame was.

Adding a second night to a target only works if the new frames overlap
the old ones - the same centre and the same camera angle, or the stack is
cropped to whatever the two nights happen to share. So this takes one of
the saved frames as the reference and:

1. plate solves it, for its exact centre and camera angle;
2. slews there and centres by plate solving, as a GoTo does;
3. keeps solving while you turn the camera, saying which way and how far,
   until the angle matches - then centres once more, since turning the
   camera rarely leaves the centre exactly where it was.

New lights then go into the reference's target folder, beside the old ones.

Which way is "clockwise" depends on the optical train - a diagonal or an
off-axis mirror reverses it - so the direction is checked against what
actually happens after the first turn, and corrected and remembered if it
was the wrong way round.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING

from astropi.core.errors import AstropiError, SolveFailedError
from astropi.core.geometry import RaDec
from astropi.devices.camera import ExposureRequest, FrameKind
from astropi.sequencing.task import Task
from astropi.sequencing.tasks.centering import GotoAndCenterTask
from astropi.services.platesolve import SolveHint, SolveResult

if TYPE_CHECKING:
    from astropi.runtime import Observatory

#: A difference this close to half a turn is the mount on the other side
#: of the meridian: the same framing upside down, which stacking undoes.
HALF_TURN_SLACK_DEG = 2.0


def position_angle(result: SolveResult) -> float:
    """The direction of the frame's rows-increasing axis, east of north.

    Taken from the WCS rather than the solver's rotation field: solvers
    disagree on that one's sign and origin, while this is the same
    measurement whichever produced the solve.
    """
    if result.cd is None:
        return result.field_rotation_deg
    east, north = result.cd[0][1], result.cd[1][1]
    return math.degrees(math.atan2(east, north)) % 360.0


def mirrored(result: SolveResult) -> bool:
    if result.cd is None:
        return result.flipped
    (a, b), (c, d) = result.cd
    return a * d - b * c > 0


def angle_between(current: float, reference: float) -> float:
    """`current - reference`, in (-180, 180]."""
    delta = (current - reference + 180.0) % 360.0 - 180.0
    return 180.0 if delta == -180.0 else delta


class ReframeTask(Task):
    kind = "reframe"

    def __init__(
        self,
        observatory: Observatory,
        reference: str,
        *,
        tolerance_deg: float = 1.0,
        tolerance_arcmin: float = 1.0,
        exposure_s: float = 4.0,
        accept_half_turn: bool = True,
    ) -> None:
        super().__init__(name="Resume framing")
        self._observatory = observatory
        self._reference = reference
        self._tolerance_deg = tolerance_deg
        self._tolerance_arcmin = tolerance_arcmin
        self._exposure_s = exposure_s
        self._accept_half_turn = accept_half_turn

    async def run(self) -> dict:
        observatory = self._observatory
        solver = observatory.plate_solver

        self.report("solving reference", fraction=0.0, message=f"Plate solving {self._reference}")
        frame, header = await asyncio.to_thread(observatory.archive.load, self._reference)
        hint = None
        if header.get("RA") is not None and header.get("DEC") is not None:
            hint = SolveHint(
                center=RaDec(float(header["RA"]), float(header["DEC"])),
                radius_deg=10.0,
                pixel_scale_arcsec=frame.metadata.get("pixel_scale_arcsec"),
            )
        try:
            reference = await solver.solve(frame, hint)
        except SolveFailedError as error:
            raise AstropiError(f"the reference frame would not solve: {error}") from error
        target_angle = position_angle(reference)
        name = str(header.get("OBJECT") or "") or None
        self.report(
            "solving reference",
            message=(
                f"Reference is at {reference.center}, camera angle {target_angle:.1f}°"
                + (f" ({name})" if name else "")
            ),
            reference_ra_deg=reference.center.ra_deg,
            reference_dec_deg=reference.center.dec_deg,
            reference_angle_deg=round(target_angle, 2),
            target_name=name,
        )

        await self._centre(reference.center, name, "Slewing to the reference")
        # The GoTo names the target from the coordinate; put the
        # reference's own name back so the new lights join the old ones.
        observatory.set_active_target(observatory.target_for_coord(reference.center, name=name))

        sense = float(observatory.state.get("reframe").get("rotation_sense", 1.0))
        previous: float | None = None
        learned = False
        while True:
            result = await self._solve_now()
            if mirrored(result) != mirrored(reference):
                raise AstropiError(
                    "this frame is mirrored relative to the reference - a different optical train "
                    "(a diagonal added or removed) cannot be matched by turning the camera"
                )
            delta = angle_between(position_angle(result), target_angle)
            half_turn = abs(abs(delta) - 180.0) <= HALF_TURN_SLACK_DEG

            # Learn which way "clockwise" is: after the operator turned the
            # way they were told, the error should have shrunk. Judged once,
            # on the first clear change, so a slip of the hand later cannot
            # flip it back.
            moved = previous is not None and abs(abs(delta) - abs(previous)) > self._tolerance_deg
            if moved and not learned:
                learned = True
                if abs(delta) > abs(previous) and abs(delta) < 179.0:
                    sense = -sense
                    observatory.state.put("reframe", {"rotation_sense": sense})
                    self.report(
                        "rotate",
                        message="That made it worse: the directions were the wrong way round "
                        "for this setup. Corrected, and remembered.",
                    )

            if abs(delta) <= self._tolerance_deg or (self._accept_half_turn and half_turn):
                break

            turn = -delta * sense
            direction = "clockwise" if turn < 0 else "anticlockwise"
            previous = delta
            self.report(
                "rotate",
                message=f"Turn the camera {abs(delta):.1f}° {direction}, seen from behind the camera",
                rotate_deg=round(abs(delta), 2),
                direction=direction,
                angle_deg=round(position_angle(result), 2),
                reference_angle_deg=round(target_angle, 2),
                offset_arcmin=round(result.center.separation_deg(reference.center) * 60.0, 2),
            )

        matched = "matched, upside down (other side of the meridian)" if half_turn else "matched"
        self.report("rotate", message=f"Camera angle {matched}: {delta:+.1f}° from the reference")
        result = await self._solve_now()
        if result.center.separation_deg(reference.center) * 60.0 > self._tolerance_arcmin:
            await self._centre(reference.center, name, "Centring again after the turn")
            observatory.set_active_target(observatory.target_for_coord(reference.center, name=name))

        self.report(
            "done",
            fraction=1.0,
            message=f"Framing matches {self._reference}; new lights go to {name or 'the same coordinates'}",
        )
        return {
            "reference": self._reference,
            "angle_error_deg": round(delta, 2),
            "upside_down": half_turn,
            "target_name": name,
        }

    async def _centre(self, coord: RaDec, name: str | None, message: str) -> None:
        self.report("centring", message=message)
        task = GotoAndCenterTask(
            self._observatory,
            coord,
            name=f"Centre {name or coord}",
            tolerance_arcmin=self._tolerance_arcmin,
            exposure_s=self._exposure_s,
        )
        task.bind(self._observatory.events)
        task.report_into(lambda _step, msg: msg and self.report("centring", message=msg))
        await task.run()

    async def _solve_now(self) -> SolveResult:
        observatory = self._observatory
        camera = observatory.camera()
        frame = await camera.expose(ExposureRequest(duration_s=self._exposure_s, kind=FrameKind.PREVIEW))
        observatory.frames.add(frame)
        status = await observatory.mount().status()
        hint = SolveHint(
            center=status.position,
            radius_deg=5.0,
            pixel_scale_arcsec=frame.metadata.get("pixel_scale_arcsec"),
        )
        while True:
            try:
                return await observatory.plate_solver.solve(frame, hint)
            except SolveFailedError as error:
                # A blurred frame while the camera is being turned is
                # expected; say so and look again rather than give up.
                self.report("rotate", message=f"No solve ({error}) - trying again")
                frame = await camera.expose(
                    ExposureRequest(duration_s=self._exposure_s, kind=FrameKind.PREVIEW)
                )
                observatory.frames.add(frame)
