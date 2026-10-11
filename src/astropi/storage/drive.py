"""The drive frames are written to, and mounting it from the dashboard.

On the Pi the frames go to a USB SSD at /mnt/ssd, listed in /etc/fstab.
That entry is a systemd mount unit (`mnt-ssd.mount`), so mounting it is
starting that unit: no shell, no sudo, and the mount point, filesystem
and ownership all come from fstab rather than from here. Without a
password it needs a polkit rule letting the rig's user start that one
unit; without the rule systemd refuses, and the error says so.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from astropi.storage.archive import ArchiveError, _mount_point

#: Roots where a separate drive is expected rather than the SD card.
DRIVE_ROOTS = ("/mnt/", "/media/")


@dataclass(frozen=True, slots=True)
class DriveStatus:
    path: str
    #: True when frames would land somewhere other than the root filesystem.
    mounted: bool
    #: Whether this path is meant to be on a separate drive at all.
    needs_drive: bool
    mount_point: str | None
    mount_unit: str | None
    free_bytes: int | None
    total_bytes: int | None

    def as_dict(self) -> dict:
        return asdict(self)


def expected_mount_point(root: Path) -> Path | None:
    """Where the drive for `root` should be mounted: /mnt/<name> or /media/<name>."""
    parts = root.parts
    if len(parts) >= 3 and parts[1] in ("mnt", "media"):
        return Path(*parts[:3])
    return None


def mount_unit_name(mount_point: Path) -> str:
    """The systemd unit name for a mount point, as `systemd-escape --path --suffix=mount` gives it."""
    escaped = []
    for part in mount_point.parts[1:]:
        out = ""
        for i, char in enumerate(part):
            if char.isalnum() or char in "_:" or (char == "." and i > 0):
                out += char
            else:
                out += f"\\x{ord(char):02x}"
        escaped.append(out)
    return "-".join(escaped) + ".mount"


def status(root: Path, unit: str | None = None) -> DriveStatus:
    needs_drive = str(root).startswith(DRIVE_ROOTS)
    expected = expected_mount_point(root)
    existing = root
    while not existing.exists():
        existing = existing.parent
    point = _mount_point(existing)
    mounted = point != Path("/") if needs_drive else True
    free = total = None
    if mounted:
        usage = shutil.disk_usage(existing)
        free, total = usage.free, usage.total
    if unit is None and expected is not None:
        unit = mount_unit_name(expected)
    return DriveStatus(
        path=str(root),
        mounted=mounted,
        needs_drive=needs_drive,
        mount_point=str(point) if mounted and needs_drive else (str(expected) if expected else None),
        mount_unit=unit,
        free_bytes=free,
        total_bytes=total,
    )


async def mount(unit: str, *, timeout_s: float = 30.0) -> None:
    """Start the drive's mount unit. Raises `ArchiveError` with systemd's reason."""
    if not unit.endswith(".mount") or "/" in unit:
        raise ArchiveError(f"not a mount unit: {unit!r}")
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise ArchiveError("systemctl is not available on this machine")
    process = await asyncio.create_subprocess_exec(
        systemctl,
        "start",
        "--no-ask-password",
        unit,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "SYSTEMD_PAGER": ""},
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        raise ArchiveError(
            f"mounting {unit} took longer than {timeout_s:.0f} s - is the SSD plugged in?"
        ) from None
    if process.returncode != 0:
        reason = stderr.decode(errors="replace").strip() or f"exit status {process.returncode}"
        if "interactive authentication required" in reason.lower():
            reason += " (the polkit rule that lets the rig mount the SSD is not installed)"
        raise ArchiveError(f"could not mount {unit}: {reason}")
