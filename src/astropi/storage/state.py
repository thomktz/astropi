"""The handful of settings the rig remembers between restarts.

Environment variables describe the machine; this describes the evening.
Where you are standing and whether the mount is the real one are decisions
made in the dashboard, and losing them on every restart - which is what
happened before this file existed - means setting them again in the dark.

Deliberately small and deliberately not a database: a few fields, written
whole, tolerant of being absent or corrupt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILENAME = "state.json"


class StateStore:
    def __init__(self, directory: Path) -> None:
        self._path = directory / FILENAME
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            with self._path.open() as handle:
                loaded = json.load(handle)
            return loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must never stop the rig coming up; the
            # defaults are all usable.
            logger.exception("ignoring unreadable %s", self._path)
            return {}

    def get(self, section: str) -> dict[str, Any]:
        value = self._data.get(section)
        return dict(value) if isinstance(value, dict) else {}

    def put(self, section: str, values: dict[str, Any]) -> None:
        self._data[section] = values
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the target and moved over it, so an interrupted
            # write cannot leave a half-file that fails to parse next boot.
            temporary = self._path.with_suffix(".tmp")
            with temporary.open("w") as handle:
                json.dump(self._data, handle, indent=2)
            temporary.replace(self._path)
        except OSError:
            logger.exception("could not write %s", self._path)
