"""The dark library: darks shot once, reused across nights.

A dark only has to match the lights' exposure, gain, offset and sensor
temperature, so a set shot on a cloudy night serves every session at
those settings until the sensor ages. Each set is a folder under
`DARKS_LIBRARY` with a `set.json` beside the frames saying how many there
are, when they were shot, on which camera and how cold it actually was.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

from astropi.storage.naming import DARK_LIBRARY, settings_label

logger = logging.getLogger(__name__)

META = "set.json"
#: A set's temperature may differ from the lights' by this much and still match.
TEMP_TOLERANCE_C = 1.0
#: Older than this, a set is still offered, with a note that hot pixels change.
STALE_AFTER_DAYS = 270


class DarkLibrary:
    def __init__(self, root: Path) -> None:
        self._archive_root = root

    @property
    def root(self) -> Path:
        return self._archive_root / DARK_LIBRARY

    def folder_for(self, exposure_s: float, gain: object, offset: object, temp_c: float) -> Path:
        return self.root / settings_label(exposure_s, gain, offset, temp_c)

    def sets(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        out = []
        for meta in sorted(self.root.glob(f"*/{META}")):
            try:
                data = json.loads(meta.read_text())
            except (OSError, json.JSONDecodeError):
                logger.warning("ignoring unreadable %s", meta)
                continue
            data["path"] = str(meta.parent)
            # Counted, not trusted: frames may have been deleted by hand.
            data["count"] = sum(1 for _ in meta.parent.glob("*.fits"))
            age_days = (time.time() - float(data.get("shot_at", time.time()))) / 86400.0
            data["age_days"] = round(age_days)
            data["stale"] = age_days > STALE_AFTER_DAYS
            out.append(data)
        return out

    def match(
        self, exposure_s: float, gain: object, offset: object, temp_c: float | None
    ) -> dict[str, Any] | None:
        """The set that calibrates lights shot at these settings, if any."""
        if temp_c is None:
            return None
        candidates = [
            s
            for s in self.sets()
            if s["count"] > 0
            and abs(float(s["exposure_s"]) - exposure_s) < 1e-3
            and s.get("gain") == gain
            and s.get("offset") == offset
            and abs(float(s["temp_c"]) - temp_c) <= TEMP_TOLERANCE_C
        ]
        # The closest temperature, then the newest.
        candidates.sort(key=lambda s: (abs(float(s["temp_c"]) - temp_c), -float(s.get("shot_at", 0))))
        return candidates[0] if candidates else None

    def record(
        self,
        folder: Path,
        *,
        exposure_s: float,
        gain: object,
        offset: object,
        temp_c: float,
        camera: str,
        sensor_temps: list[float],
    ) -> None:
        """Write or refresh a set's metadata after shooting into it."""
        count = sum(1 for _ in folder.glob("*.fits"))
        meta = {
            "exposure_s": exposure_s,
            "gain": gain,
            "offset": offset,
            "temp_c": temp_c,
            "count": count,
            "shot_at": time.time(),
            "shot_on": dt.date.today().isoformat(),
            "camera": camera,
            "mean_sensor_c": round(sum(sensor_temps) / len(sensor_temps), 2) if sensor_temps else None,
        }
        (folder / META).write_text(json.dumps(meta, indent=2))
