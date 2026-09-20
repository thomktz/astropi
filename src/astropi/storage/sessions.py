"""Session plans on disk.

One JSON file per plan, in a directory. No database: a plan is a few
kilobytes, there are a handful of them, and being able to read one in a
text editor - or copy last week's and change the targets - is worth more
here than anything a schema would buy.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from astropi.core.geometry import RaDec
from astropi.services.planning import PlanBlock, SessionPlan

logger = logging.getLogger(__name__)


def _block_to_dict(block: PlanBlock) -> dict:
    return {
        "id": block.id,
        "target_id": block.target_id,
        "target_name": block.target_name,
        "ra_deg": block.coord.ra_deg,
        "dec_deg": block.coord.dec_deg,
        "frames": block.frames,
        "exposure_s": block.exposure_s,
        "gain": block.gain,
        "offset": block.offset,
        "binning": block.binning,
        "dither_every": block.dither_every,
        "center": block.center,
        "autofocus": block.autofocus,
    }


def _block_from_dict(raw: dict) -> PlanBlock:
    return PlanBlock(
        id=raw.get("id") or uuid.uuid4().hex[:8],
        target_id=raw.get("target_id"),
        target_name=raw.get("target_name", "Unnamed"),
        coord=RaDec(float(raw["ra_deg"]), float(raw["dec_deg"])),
        frames=int(raw.get("frames", 1)),
        exposure_s=float(raw.get("exposure_s", 60.0)),
        gain=raw.get("gain"),
        offset=raw.get("offset"),
        binning=int(raw.get("binning", 1)),
        dither_every=int(raw.get("dither_every", 3)),
        center=bool(raw.get("center", True)),
        autofocus=bool(raw.get("autofocus", False)),
    )


class SessionStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, plan_id: str) -> Path:
        # The id is generated here and never taken from a URL segment
        # unchecked; strip anyway so a crafted id cannot escape the folder.
        safe = "".join(c for c in plan_id if c.isalnum() or c in "-_")
        return self._root / f"{safe}.json"

    def save(self, plan: SessionPlan) -> SessionPlan:
        if not plan.created_at:
            plan.created_at = time.time()
        payload = {
            "id": plan.id,
            "name": plan.name,
            "created_at": plan.created_at,
            "updated_at": time.time(),
            "blocks": [_block_to_dict(block) for block in plan.blocks],
        }
        path = self._path(plan.id)
        # Written to a neighbouring file and moved into place, so a crash
        # midway leaves the previous plan intact rather than a half file.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
        return plan

    def load(self, plan_id: str) -> SessionPlan | None:
        path = self._path(plan_id)
        if not path.exists():
            return None
        return self._parse(path)

    def list(self) -> list[SessionPlan]:
        plans = []
        for path in sorted(self._root.glob("*.json")):
            plan = self._parse(path)
            if plan is not None:
                plans.append(plan)
        plans.sort(key=lambda p: p.created_at, reverse=True)
        return plans

    def delete(self, plan_id: str) -> bool:
        path = self._path(plan_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _parse(self, path: Path) -> SessionPlan | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return SessionPlan(
                id=raw["id"],
                name=raw.get("name", path.stem),
                created_at=float(raw.get("created_at", 0.0)),
                blocks=[_block_from_dict(block) for block in raw.get("blocks", [])],
            )
        except (OSError, ValueError, KeyError):
            # A hand-edited file with a typo should not stop the others
            # from loading, or take the dashboard down with it.
            logger.exception("could not read session plan %s", path)
            return None
