"""Bringing lights saved in the old layout into the current one.

Before sessions, lights were saved as

    <root>/<night>/<Target_Name>/light_<Target_Name>_<exp>s_g<gain>_<n>.fits

and are moved here to

    <root>/<night>_<Target-Name>/LIGHT/<Target-Name>_LIGHT_<Filter>_<exp>s_G<gain>_O<offset>_<temp>C_<time>_<n>.fits

with the name built from each frame's own header, and a session made for
every night and target so it opens like any other: flats and darks can be
shot into it, and the dark library matched against it.

Files are renamed, never rewritten or copied, and every rename goes in a
log that `undo` plays backwards. Anything that cannot be read is skipped
and reported, not guessed at. Old calibration frames (`<night>/calibration/`)
belonged to the night rather than a target, so they are reported and left.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astropi.storage.imaging import ImagingStore, new_session
from astropi.storage.journal import FRAME_COLUMNS
from astropi.storage.naming import ImageType, frame_stem, session_folder, target_slug

logger = logging.getLogger(__name__)

NIGHT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOG_COLUMNS = ["action", "old", "new"]


@dataclass(slots=True)
class Move:
    old: Path
    new: Path
    row: dict[str, Any]


@dataclass(slots=True)
class Group:
    """The lights of one target on one night: one session after the move."""

    night: str
    target: str
    folder: Path
    moves: list[Move] = field(default_factory=list)
    light: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Plan:
    groups: list[Group] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def moves(self) -> list[Move]:
        return [move for group in self.groups for move in group.moves]


def _header(path: Path) -> dict[str, Any]:
    from astropy.io import fits

    with fits.open(path, memmap=False) as hdul:
        return dict(hdul[0].header)


def _started(header: dict[str, Any]) -> dt.datetime:
    stamp = str(header["DATE-OBS"]).replace("Z", "")
    return dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.UTC).astimezone()


def plan(root: Path, filter_name: str = "None") -> Plan:
    """Every old light, where it would go, and what cannot be moved."""
    out = Plan()
    taken: set[Path] = set()
    for night_dir in sorted(p for p in root.iterdir() if p.is_dir() and NIGHT.match(p.name)):
        for target_dir in sorted(p for p in night_dir.iterdir() if p.is_dir()):
            if target_dir.name == "calibration":
                out.skipped.extend(
                    (path, "old calibration frame: belongs to the night, not a target - left in place")
                    for path in sorted(target_dir.rglob("*.fits"))
                )
                continue
            files = sorted(target_dir.glob("*.fits"))
            if not files:
                continue
            group: Group | None = None
            for path in files:
                if not path.name.startswith("light_"):
                    out.skipped.append((path, "not a light frame"))
                    continue
                try:
                    header = _header(path)
                    started = _started(header)
                    exposure = float(header["EXPTIME"])
                except Exception as error:
                    out.skipped.append((path, f"unreadable header: {error}"))
                    continue
                target = str(header.get("OBJECT") or "").strip() or target_dir.name.replace("_", " ")
                if group is None:
                    group = Group(
                        night=night_dir.name,
                        target=target,
                        folder=root / session_folder(night_dir.name, target),
                    )
                    out.groups.append(group)
                set_temp = header.get("SET-TEMP")
                sensor = header.get("CCD-TEMP")
                # The set point if the header has one; else the sensor, rounded.
                temp = set_temp if set_temp is not None else None if sensor is None else round(float(sensor))
                directory = group.folder / str(ImageType.LIGHT)
                stem = frame_stem(
                    ImageType.LIGHT,
                    target=group.target,
                    exposure_s=exposure,
                    gain=header.get("GAIN"),
                    offset=header.get("OFFSET"),
                    temp_c=temp,
                    started=started,
                    filter_name=str(header.get("FILTER") or filter_name),
                )
                existing = len(list(directory.glob("*.fits"))) if directory.exists() else 0
                index = existing + 1 + sum(1 for move in group.moves)
                new = directory / f"{stem}_{index:04d}.fits"
                if new.exists() or new in taken:
                    out.skipped.append((path, f"{new} already exists"))
                    continue
                taken.add(new)
                group.moves.append(
                    Move(
                        old=path,
                        new=new,
                        row={
                            "utc": started.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                            "kind": "light",
                            "target": group.target,
                            "filter": str(header.get("FILTER") or filter_name),
                            "exposure_s": exposure,
                            "gain": header.get("GAIN"),
                            "offset": header.get("OFFSET"),
                            "binning": header.get("XBINNING", 1),
                            "sensor_c": sensor,
                            "ra_deg": header.get("RA"),
                            "dec_deg": header.get("DEC"),
                            "rms_total_arcsec": header.get("GUIDERMS"),
                            "rms_ra_arcsec": header.get("GUIDERA"),
                            "rms_dec_arcsec": header.get("GUIDEDEC"),
                        },
                    )
                )
                if not group.light:
                    group.light = {
                        "exposure_s": exposure,
                        "gain": header.get("GAIN"),
                        "offset": header.get("OFFSET"),
                        # Old headers have no set point; the sensor, rounded,
                        # is what the darks and the library have to match.
                        "temp_c": None if temp is None else float(temp),
                        "binning": int(header.get("XBINNING", 1)),
                    }
    return out


def _backup(path: Path, log_path: Path, log) -> None:
    """Keep a copy of a file about to change, for `undo` to put back."""
    copy = log_path.with_suffix("") / path.relative_to(path.anchor)
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, copy)
    log.writerow(["backup", str(copy), str(path)])


def apply(result: Plan, store: ImagingStore, log_path: Path, filter_name: str = "None") -> list[str]:
    """Do the moves, make or extend the sessions, and log all of it for `undo`.

    A night and target that already has a session - the lights shot in the
    old layout, the flats in the new one - gets the lights added to it
    rather than a second session beside it.
    """
    sessions = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", newline="") as handle:
        log = csv.writer(handle)
        log.writerow(LOG_COLUMNS)
        for group in result.groups:
            if not group.moves:
                continue
            directory = group.folder / str(ImageType.LIGHT)
            directory.mkdir(parents=True, exist_ok=True)
            table = group.folder / "frames.csv"
            fields = FRAME_COLUMNS
            if table.exists():
                _backup(table, log_path, log)
                with table.open(newline="", encoding="utf-8") as existing:
                    # Appended under the table's own columns, whatever
                    # version of the app wrote it.
                    fields = next(csv.reader(existing), None) or FRAME_COLUMNS
                new_table = False
            else:
                new_table = True
            with table.open("a", newline="", encoding="utf-8") as frames:
                writer = csv.DictWriter(frames, fieldnames=fields, extrasaction="ignore")
                if new_table:
                    writer.writeheader()
                    log.writerow(["created", "", str(table)])
                for move in group.moves:
                    # A rename on one filesystem: the frame is either at its
                    # old name or its new one, never half copied.
                    os.rename(move.old, move.new)
                    log.writerow(["move", str(move.old), str(move.new)])
                    handle.flush()
                    writer.writerow({"file": str(move.new.relative_to(group.folder)), **move.row})
            for old_dir in {move.old.parent for move in group.moves}:
                if old_dir.exists() and not any(old_dir.iterdir()):
                    old_dir.rmdir()

            if target_slug(group.target) == "no-target":
                # Test frames with nothing framed: filed, but not a session.
                continue
            existing = next(
                (
                    s
                    for s in store.list()
                    if s.get("night") == group.night and s.get("target_name") == group.target
                ),
                None,
            )
            if existing is not None:
                _backup(store._path(existing["id"]), log_path, log)
                light = existing["groups"]["light"]
                light["captured"] = int(light.get("captured", 0)) + len(group.moves)
                light["count"] = max(int(light.get("count", 0)), light["captured"])
                existing.setdefault("migrated_from", []).extend(
                    sorted({str(m.old.parent) for m in group.moves})
                )
                store.save(existing)
                sessions.append(existing["id"])
                continue

            session = new_session(
                target_name=group.target,
                night=group.night,
                filter_name=filter_name,
                light={k: v for k, v in group.light.items() if v is not None},
            )
            session["groups"]["light"].update(count=len(group.moves), captured=len(group.moves))
            session["migrated_from"] = sorted({str(m.old.parent) for m in group.moves})
            store.save(session)
            log.writerow(["session", "", session["id"]])
            meta = group.folder / "session.json"
            if not meta.exists():
                meta.write_text(json.dumps(session, indent=2))
                log.writerow(["created", "", str(meta)])
            sessions.append(session["id"])
    return sessions


def undo(log_path: Path, store: ImagingStore) -> tuple[int, list[str]]:
    """Put everything a migration log records back where it was."""
    with log_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    restored, problems = 0, []
    for row in reversed(rows):
        old, new = Path(row["old"]) if row["old"] else None, Path(row["new"])
        if row["action"] == "move":
            if not new.exists():
                problems.append(f"{new} is gone; cannot put it back at {old}")
                continue
            if old.exists():
                problems.append(f"{old} exists again; left {new} where it is")
                continue
            old.parent.mkdir(parents=True, exist_ok=True)
            os.rename(new, old)
            restored += 1
            for parent in (new.parent, new.parent.parent):
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
        elif row["action"] == "created":
            if new.exists():
                new.unlink()
                parent = new.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
        elif row["action"] == "session":
            store.delete(str(new))
        elif row["action"] == "backup":
            if old.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, new)
    return restored, problems
