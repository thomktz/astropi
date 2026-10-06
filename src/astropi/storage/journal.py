"""The night's record, kept beside its frames.

Two files in the session's folder (`<night>_<Target>`, or the bare night
when no session is open), both plain text so they open anywhere:

- `log.txt`, what happened and when: every task and what it said, plate
  solves, guiding starting and stopping, the target changing, devices
  dropping out, and every few minutes the conditions - sensor temperature,
  cooler power, guiding RMS, where the mount points.
- `frames.csv`, one row per saved frame with what it was shot at and how
  the guiding was doing while it was shot: the table to sort when deciding
  which subs to throw away.

Written from the event bus, so nothing that does the work has to know it
is being recorded - and a write that fails is logged and dropped rather
than allowed anywhere near the work itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astropi.core.events import Topic
from astropi.storage.naming import session_folder

if TYPE_CHECKING:
    from astropi.runtime import Observatory

logger = logging.getLogger(__name__)

#: How often the conditions line is written while a task is running.
CONDITIONS_EVERY_S = 300.0

FRAME_COLUMNS = [
    "file",
    "utc",
    "kind",
    "target",
    "filter",
    "exposure_s",
    "gain",
    "offset",
    "binning",
    "sensor_c",
    "ra_deg",
    "dec_deg",
    "guiding",
    "rms_ra_arcsec",
    "rms_dec_arcsec",
    "rms_total_arcsec",
]


def night_of(moment: dt.datetime) -> str:
    """The date the evening started: a night past midnight stays one night."""
    return (moment.astimezone() - dt.timedelta(hours=12)).date().isoformat()


class NightJournal:
    def __init__(self, observatory: Observatory) -> None:
        self._observatory = observatory
        self._task: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._seen_messages: dict[str, int] = {}
        self._seen_states: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._observatory.archive.root

    def folder(self) -> Path:
        """The open session's folder, beside its frames; else tonight's."""
        session = self._observatory.current_session
        if session is not None:
            return self.root / session_folder(session["night"], session["target_name"])
        return self.root / night_of(dt.datetime.now())

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen())
        self._ticker = asyncio.create_task(self._conditions())

    async def stop(self) -> None:
        for task in (self._task, self._ticker):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # ---------------------------------------------------------------- writing

    def write(self, text: str) -> None:
        """One timestamped line in tonight's log."""
        try:
            self._observatory.archive.check()
            folder = self.folder()
            folder.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().astimezone().strftime("%H:%M:%S")
            with (folder / "log.txt").open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp}  {text}\n")
        except Exception:
            logger.warning("could not write the night log", exc_info=True)

    def record_frame(self, path: Path, row: dict[str, Any]) -> None:
        """One row in the frames table of the night the frame belongs to."""
        try:
            # The night is the first folder under the archive, whether the
            # frame is a light in a target folder or a calibration frame.
            table = self.root / path.relative_to(self.root).parts[0] / "frames.csv"
            new = not table.exists()
            with table.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FRAME_COLUMNS, extrasaction="ignore")
                if new:
                    writer.writeheader()
                writer.writerow({"file": str(path.relative_to(table.parent)), **row})
        except Exception:
            logger.warning("could not record %s in the frames table", path, exc_info=True)

    # --------------------------------------------------------------- listening

    async def _listen(self) -> None:
        async with self._observatory.events.subscribe() as events:
            async for event in events:
                try:
                    self._handle(event.topic, event.payload)
                except Exception:
                    logger.warning("night log could not handle %s", event.topic, exc_info=True)

    def _handle(self, topic: Topic, payload: dict[str, Any]) -> None:
        if topic is Topic.TASK_UPDATE:
            task_id, name = payload["id"], payload["name"]
            state = payload["state"]
            if self._seen_states.get(task_id) != state:
                self._seen_states[task_id] = state
                if state == "running":
                    self.write(f"[{name}] started")
                elif state in ("succeeded", "cancelled"):
                    self.write(f"[{name}] {state}")
                elif state == "failed":
                    error = (payload.get("error") or "").strip().splitlines()
                    self.write(f"[{name}] FAILED: {error[-1] if error else 'no reason given'}")
            messages = payload.get("messages") or []
            for message in messages[self._seen_messages.get(task_id, 0) :]:
                self.write(f"[{name}] {message}")
            self._seen_messages[task_id] = len(messages)
        elif topic is Topic.SOLVE_RESULT:
            if payload.get("success"):
                self.write(
                    f"solved: RA {payload['ra_deg']:.4f} Dec {payload['dec_deg']:+.4f}, "
                    f"angle {payload.get('rotation_deg', 0):.1f}, "
                    f"{payload.get('stars_detected', '?')} stars ({payload.get('solver')})"
                )
            else:
                self.write(f"solve failed ({payload.get('solver')}): {payload.get('error')}")
        elif topic is Topic.GUIDING_STATE:
            self.write(f"guiding: {payload.get('state')}")
        elif topic is Topic.TARGET:
            target = payload.get("target")
            self.write(f"target: {target['display_name'] if target else 'none'}")
        elif topic is Topic.DEVICE_STATE and payload.get("connection") not in (None, "connected"):
            self.write(f"{payload.get('role')}: {payload.get('connection')}")

    async def _conditions(self) -> None:
        while True:
            await asyncio.sleep(CONDITIONS_EVERY_S)
            if self._observatory.tasks.current is None:
                continue
            try:
                self.write(await self._observatory.conditions_line())
            except Exception:
                logger.warning("could not take a conditions reading", exc_info=True)
