"""Target catalogue and search.

Data carried over from the previous frontend, where it lived as TypeScript
modules, and moved to the backend so that every device on the network reads
one catalogue and so ranking can use real altitudes rather than the
browser's approximation.

Sources and licences:
  * Messier catalogue, 110 objects, J2000.
  * OpenNGC (CC-BY-SA-4.0), filtered to named objects and anything brighter
    than magnitude 10 that Messier does not already cover.
  * IAU Working Group on Star Names (CC-BY), 434 approved proper names.
  * Planets come from astropy at query time, since unlike the rest they move.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import numpy as np

from astropi.core.geometry import RaDec
from astropi.services.ephemeris import EphemerisService

DATA_ROOT = Path(__file__).resolve().parents[3] / "data" / "catalog"

PLANET_MAGNITUDES = {
    "mercury": -0.4,
    "venus": -4.4,
    "mars": 0.7,
    "jupiter": -2.2,
    "saturn": 0.5,
    "uranus": 5.7,
    "neptune": 7.8,
}


class TargetSource(StrEnum):
    MESSIER = "messier"
    NGC = "ngc"
    STAR = "star"
    PLANET = "planet"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class Target:
    id: str
    name: str
    coord: RaDec
    source: TargetSource
    object_type: str = "Unknown"
    common_names: tuple[str, ...] = ()
    constellation: str | None = None
    magnitude: float = 99.0

    @property
    def display_name(self) -> str:
        return self.common_names[0] if self.common_names else self.name

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.common_names)


def _load_file(path: Path, source: TargetSource) -> list[Target]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Target(
            id=entry["id"],
            name=entry["name"],
            coord=RaDec(float(entry["ra"]), float(entry["dec"])),
            source=source,
            object_type=entry.get("type", "Unknown"),
            common_names=tuple(entry.get("commonNames", ())),
            constellation=entry.get("constellation"),
            magnitude=float(entry.get("magnitude", 99.0)),
        )
        for entry in raw
    ]


class CatalogService:
    """Loads the static catalogue and answers searches against it."""

    def __init__(self, ephemeris: EphemerisService, data_root: Path | None = None) -> None:
        self._ephemeris = ephemeris
        self._root = data_root or DATA_ROOT
        self._static: list[Target] = []
        self._custom: dict[str, Target] = {}
        self._load()

    def _load(self) -> None:
        self._static = [
            *_load_file(self._root / "messier.json", TargetSource.MESSIER),
            *_load_file(self._root / "ngc.json", TargetSource.NGC),
            *_load_file(self._root / "stars.json", TargetSource.STAR),
        ]
        self._static.sort(key=lambda t: t.magnitude)

    def set_ephemeris(self, ephemeris: EphemerisService) -> None:
        self._ephemeris = ephemeris

    def planets(self, when: datetime | None = None) -> list[Target]:
        """Planets, positioned at query time because they move."""
        positions = self._ephemeris.planet_positions(when)
        return [
            Target(
                id=name,
                name=name.capitalize(),
                coord=coord,
                source=TargetSource.PLANET,
                object_type="Planet",
                magnitude=PLANET_MAGNITUDES.get(name, 5.0),
            )
            for name, coord in positions.items()
        ]

    def all(self, *, include_planets: bool = True) -> list[Target]:
        targets = [*self._static, *self._custom.values()]
        if include_planets:
            targets = [*self.planets(), *targets]
        return targets

    def get(self, target_id: str) -> Target | None:
        if target_id in self._custom:
            return self._custom[target_id]
        lowered = target_id.lower()
        for target in self.all():
            if target.id.lower() == lowered:
                return target
        return None

    def add_custom(self, target: Target) -> Target:
        """Register an ad-hoc target, e.g. a comet or a bare coordinate."""
        self._custom[target.id] = target
        return target

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        min_altitude_deg: float | None = None,
        when: datetime | None = None,
    ) -> list[tuple[Target, float]]:
        """Rank targets by relevance, returning each with its altitude.

        `min_altitude_deg` filters to what is actually observable, which is
        usually the question being asked - a perfect name match on something
        below the horizon is not a useful first result.
        """
        candidates = self.all()
        scored: list[tuple[float, Target]] = []
        normalized = query.strip().lower()

        for target in candidates:
            score = 0.0 if not normalized else _match_score(target, normalized)
            if score is None:
                continue
            scored.append((score, target))

        if not scored:
            return []

        # Magnitude breaks ties, so an equally good name match prefers the
        # brighter object - almost always the one meant.
        scored.sort(key=lambda pair: (pair[0], pair[1].magnitude))
        shortlist = [target for _, target in scored[: max(limit * 4, limit)]]

        altitudes = self._ephemeris.altitudes_bulk([t.coord for t in shortlist], when)
        paired = list(zip(shortlist, altitudes.tolist(), strict=True))
        if min_altitude_deg is not None:
            paired = [entry for entry in paired if entry[1] >= min_altitude_deg]
        return paired[:limit]

    def recommended(
        self, *, limit: int = 20, min_altitude_deg: float = 30.0, max_magnitude: float = 10.0
    ) -> list[tuple[Target, float]]:
        """What is worth pointing at right now.

        Ranked by altitude rather than fame: the highest object is the one
        seen through the least atmosphere, which matters more to a result
        than which list it appears on.
        """
        candidates = [
            t
            for t in self.all()
            if t.magnitude <= max_magnitude and t.source is not TargetSource.STAR
        ]
        if not candidates:
            return []
        altitudes = self._ephemeris.altitudes_bulk([t.coord for t in candidates])
        paired = [
            (target, float(altitude))
            for target, altitude in zip(candidates, altitudes.tolist(), strict=True)
            if altitude >= min_altitude_deg
        ]
        paired.sort(key=lambda entry: entry[1], reverse=True)
        return paired[:limit]

    def as_star_array(self, *, max_magnitude: float = 12.0) -> np.ndarray:
        """Catalogue rows packed for the simulator's renderer."""
        rows = [
            (t.coord.ra_deg, t.coord.dec_deg, t.magnitude)
            for t in self._static
            if t.magnitude <= max_magnitude and not math.isnan(t.magnitude)
        ]
        return np.asarray(rows, dtype=float) if rows else np.empty((0, 3))

    def __len__(self) -> int:
        return len(self._static) + len(self._custom)


def _match_score(target: Target, query: str) -> float | None:
    """Lower is a better match, or None for no match at all.

    Name and common-name matches always beat a constellation match, so
    "andromeda" surfaces the Andromeda Galaxy itself ahead of the dozens of
    unrelated stars that merely sit in that constellation.
    """
    name = target.name.lower()
    commons = [n.lower() for n in target.common_names]

    if name == query:
        return 0.0
    if query in commons:
        return 1.0
    if name.startswith(query):
        return 2.0
    if any(n.startswith(query) for n in commons):
        return 3.0
    if name.replace(" ", "") == query.replace(" ", ""):
        return 3.5
    if query in name:
        return 4.0
    if any(query in n for n in commons):
        return 5.0
    if target.constellation and query in target.constellation.lower():
        return 6.0
    return None
