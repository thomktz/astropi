"""Running a session plan.

Walks the blocks in order: centre on the target, optionally focus, then
shoot. It is one task from the operator's point of view, so the centring
and capture tasks it runs report into it rather than publishing under ids
of their own.

A block that fails does not end the night. An unattended run that abandons
four hours of good targets because one solve failed on the first is worse
than one that records the failure and moves on.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from astropi.core.errors import AstropiError
from astropi.sequencing.task import Task
from astropi.sequencing.tasks.capture import CapturePlan, CaptureSequenceTask
from astropi.sequencing.tasks.centering import GotoAndCenterTask
from astropi.sequencing.tasks.focus import AutofocusTask
from astropi.services.planning import SessionPlan

if TYPE_CHECKING:
    from astropi.runtime import Observatory


@dataclass(slots=True)
class BlockOutcome:
    block_id: str
    target_name: str
    frames_requested: int
    frames_captured: int = 0
    centred: bool = False
    error: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass(slots=True)
class SessionOutcome:
    plan_name: str
    blocks: list[BlockOutcome] = field(default_factory=list)

    @property
    def frames_captured(self) -> int:
        return sum(block.frames_captured for block in self.blocks)


class SessionRunTask(Task):
    kind = "session"

    def __init__(self, observatory: Observatory, plan: SessionPlan) -> None:
        super().__init__(name=plan.name)
        self._observatory = observatory
        self._plan = plan

    async def run(self) -> SessionOutcome:
        outcome = SessionOutcome(plan_name=self._plan.name)
        total = len(self._plan.blocks)
        if not total:
            raise AstropiError("the plan has no blocks")

        for index, block in enumerate(self._plan.blocks, start=1):
            result = BlockOutcome(
                block_id=block.id,
                target_name=block.target_name,
                frames_requested=block.frames,
                started_at=time.time(),
            )
            outcome.blocks.append(result)
            base = (index - 1) / total

            self.report(
                "block",
                fraction=base,
                message=f"Block {index}/{total}: {block.target_name}",
                block=index,
                total=total,
                target=block.target_name,
            )

            try:
                await self._run_block(block, index, total)
                result.centred = block.center
                result.frames_captured = block.frames
            except asyncio.CancelledError:
                result.error = "cancelled"
                result.finished_at = time.time()
                raise
            except Exception as error:
                # Recorded and stepped over. The rest of the night is worth
                # more than this block.
                result.error = str(error)
                self.report("block_failed", message=f"{block.target_name} failed: {error}")
            result.finished_at = time.time()

        failed = [block for block in outcome.blocks if block.error]
        self.report(
            "done",
            fraction=1.0,
            message=(
                f"Captured {outcome.frames_captured} frames across {total} block(s)"
                + (f"; {len(failed)} failed" if failed else "")
            ),
        )
        return outcome

    async def _run_block(self, block, index: int, total: int) -> None:
        """Centre, focus and shoot one block."""
        prefix = f"[{index}/{total}] "

        if block.center:
            centring = GotoAndCenterTask(
                self._observatory,
                block.coord,
                name=f"Centre {block.target_name}",
                catalog_target=self._observatory.catalog.get(block.target_id)
                if block.target_id
                else None,
            )
            await self._run_sub(centring, prefix)

        if block.autofocus:
            await self._run_sub(AutofocusTask(self._observatory), prefix)

        capture = CaptureSequenceTask(
            self._observatory,
            CapturePlan(
                count=block.frames,
                exposure_s=block.exposure_s,
                gain=block.gain,
                offset=block.offset,
                binning=block.binning,
                dither_every=block.dither_every,
                name=f"{block.target_name} lights",
            ),
        )
        await self._run_sub(capture, prefix)

    async def _run_sub(self, task: Task, prefix: str) -> None:
        """Run a task inside this one, folding its messages into ours."""
        task.bind(self._observatory.events)
        task.report_into(lambda _step, message: self._forward_message(prefix, message))
        await task.run()

    def _forward_message(self, prefix: str, message: str | None) -> None:
        if message:
            self.report(self.progress.step, message=f"{prefix}{message}")
