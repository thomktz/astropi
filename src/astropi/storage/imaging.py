"""Imaging sessions: one target, four groups of frames, started by hand.

A session is what a night on one target needs to end up stackable:
lights, and the darks, flats and dark flats that calibrate them. Each
group is started on its own when the rig is ready for it - the cap on,
the flat panel on - so nothing here runs in a sequence.

The calibration groups follow the lights unless edited by hand:

- darks take the lights' exposure, gain, offset and temperature;
- flats take the lights' gain, offset and temperature, and find their own
  exposure;
- dark flats take the flats' exposure, gain, offset and temperature.

A group that has been edited is unlinked and keeps its own values until
linked again. One JSON file per session, like the plans before them.

The filter is the session's, unless a group names its own; flats only
calibrate lights shot through the same one. Darks ignore it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KINDS = ("light", "flat", "darkflat", "dark")
DEFAULT_COUNTS = {"light": 60, "flat": 30, "darkflat": 30, "dark": 20}
#: What each calibration group copies, and from which group.
LINKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "dark": ("light", ("exposure_s", "gain", "offset", "temp_c", "binning")),
    "flat": ("light", ("gain", "offset", "temp_c", "binning")),
    "darkflat": ("flat", ("exposure_s", "gain", "offset", "temp_c", "binning")),
}


def new_group(kind: str, **values: Any) -> dict[str, Any]:
    group = {
        "count": DEFAULT_COUNTS[kind],
        "exposure_s": None if kind in ("flat", "darkflat") else 120.0,
        "gain": None,
        "offset": None,
        "temp_c": None,
        "binning": 1,
        "captured": 0,
        "linked": kind != "light",
    }
    if kind != "dark":
        # None: the session's filter.
        group["filter"] = None
    if kind == "light":
        group["dither_every"] = 3
    if kind == "flat":
        # Found by test frames when started, then remembered here.
        group["auto_exposure"] = True
    group.update({k: v for k, v in values.items() if k in group})
    return group


def relink(session: dict[str, Any]) -> dict[str, Any]:
    """Copy the lights' settings into the groups still following them.

    In dependency order, so dark flats see the flats' fresh values.
    """
    groups = session["groups"]
    for kind in ("dark", "flat", "darkflat"):
        source, fields = LINKS[kind]
        if groups[kind].get("linked"):
            for field in fields:
                if field == "exposure_s" and source == "flat" and groups["flat"].get("exposure_s") is None:
                    continue
                groups[kind][field] = groups[source][field]
    return session


def group_filter(session: dict[str, Any], kind: str) -> str | None:
    """The filter a group shoots through; dark flats follow the flats."""
    if kind == "dark":
        return None
    groups = session["groups"]
    own = groups[kind].get("filter")
    if own:
        return own
    if kind == "darkflat" and groups["flat"].get("filter"):
        return groups["flat"]["filter"]
    return session.get("filter")


def new_session(
    *,
    target_name: str,
    night: str,
    filter_name: str = "None",
    target_id: str | None = None,
    ra_deg: float | None = None,
    dec_deg: float | None = None,
    light: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.time()
    session = {
        "id": uuid.uuid4().hex[:12],
        "target_id": target_id,
        "target_name": target_name,
        "ra_deg": ra_deg,
        "dec_deg": dec_deg,
        "night": night,
        "filter": filter_name,
        "created_at": now,
        "updated_at": now,
        "groups": {kind: new_group(kind, **(light or {}) if kind == "light" else {}) for kind in KINDS},
    }
    return relink(session)


class ImagingStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, session_id: str) -> Path:
        if not session_id.isalnum():
            raise ValueError(f"bad session id {session_id!r}")
        return self._root / f"{session_id}.json"

    def list(self) -> list[dict[str, Any]]:
        if not self._root.exists():
            return []
        out = []
        for path in self._root.glob("*.json"):
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                logger.warning("ignoring unreadable session %s", path)
        out.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return out

    def load(self, session_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(session_id).read_text())
        except (OSError, ValueError):
            return None

    def save(self, session: dict[str, Any]) -> dict[str, Any]:
        self._root.mkdir(parents=True, exist_ok=True)
        session["updated_at"] = time.time()
        path = self._path(session["id"])
        partial = path.with_suffix(".json.part")
        partial.write_text(json.dumps(session, indent=2))
        partial.replace(path)
        return session

    def delete(self, session_id: str) -> bool:
        try:
            self._path(session_id).unlink()
            return True
        except (OSError, ValueError):
            return False
