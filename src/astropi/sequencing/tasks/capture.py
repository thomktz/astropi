"""Imaging sequences.

Shoots a series of exposures, dithering between them when guiding is
running. Frames are handed to the frame store as they arrive rather than
accumulated, so a long run has a bounded memory footprint and a crash
mid-sequence does not lose everything shot so far.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from astropi.core.errors import AstropiError
from astropi.devices.camera import ExposureRequest, FrameKind
from astropi.devices.guider import GuidingState
from astropi.sequencing.task import Task

if TYPE_CHECKING:
    from astropi.runtime import Observatory


@dataclass(slots=True)
class CapturePlan:
    count: int
    exposure_s: float
    gain: int | None = None
    offset: int | None = None
    binning: int = 1
    kind: FrameKind = FrameKind.LIGHT
    #: Dither every N frames; zero disables dithering.
    dither_every: int = 3
    dither_px: float = 12.0
    name: str = "Light frames"


@dataclass(slots=True)
class CaptureResult:
    requested: int
    captured: int
    frame_ids: list[str] = field(default_factory=list)


class CaptureSequenceTask(Task):
    kind = "capture"

    def __init__(self, observatory: Observatory, plan: CapturePlan) -> None:
        super().__init__(name=plan.name)
        self._observatory = observatory
        self._plan = plan

    async def run(self) -> CaptureResult:
        camera = self._observatory.camera()
        store = self._observatory.frames
        plan = self._plan
        result = CaptureResult(requested=plan.count, captured=0)

        for index in range(1, plan.count + 1):
            if plan.dither_every and index > 1 and (index - 1) % plan.dither_every == 0:
                await self._dither(index)

            self.report(
                "exposing",
                fraction=(index - 1) / plan.count,
                message=f"Frame {index}/{plan.count}, {plan.exposure_s:g}s",
                frame=index,
                total=plan.count,
                exposure_s=plan.exposure_s,
            )
            frame = await camera.expose(
                ExposureRequest(
                    duration_s=plan.exposure_s,
                    gain=plan.gain,
                    offset=plan.offset,
                    binning=plan.binning,
                    kind=plan.kind,
                )
            )
            frame_id = store.add(frame)
            result.frame_ids.append(frame_id)
            result.captured = index
            self.report(
                "captured",
                fraction=index / plan.count,
                message=f"Frame {index}/{plan.count} captured",
                frame_id=frame_id,
            )

        self.report("done", fraction=1.0, message=f"Captured {result.captured} frames")
        return result

    async def _dither(self, index: int) -> None:
        guider = self._observatory.guider
        if guider is None:
            return
        status = await guider.status()
        if status.state not in (GuidingState.GUIDING, GuidingState.SETTLING):
            # Dithering without guiding just moves the target and never
            # recovers, so skip it rather than corrupt the run.
            return
        self.report("dithering", message=f"Dithering before frame {index}")
        try:
            await guider.dither(self._plan.dither_px, settle_px=1.5, settle_time_s=8.0)
        except AstropiError as error:
            self.report("dithering", message=f"Dither skipped: {error}")
            await asyncio.sleep(0.5)
