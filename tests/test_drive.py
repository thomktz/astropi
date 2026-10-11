"""The frames drive: is it there, and mounting it through systemd."""

from __future__ import annotations

from pathlib import Path

import pytest

from astropi.storage import drive
from astropi.storage.archive import ArchiveError


def test_the_mount_unit_is_named_as_systemd_names_it():
    assert drive.expected_mount_point(Path("/mnt/ssd/astropi")) == Path("/mnt/ssd")
    assert drive.mount_unit_name(Path("/mnt/ssd")) == "mnt-ssd.mount"
    assert drive.mount_unit_name(Path("/media/my-drive")) == "media-my\\x2ddrive.mount"
    assert drive.expected_mount_point(Path("/home/pi/frames")) is None


def test_a_missing_drive_is_reported_with_the_unit_to_start():
    # Nothing is mounted at /mnt/<this> on any test machine.
    status = drive.status(Path("/mnt/astropi-test-no-such-drive/frames"))
    assert not status.mounted and status.needs_drive
    assert status.mount_unit == "mnt-astropi\\x2dtest\\x2dno\\x2dsuch\\x2ddrive.mount"
    assert status.free_bytes is None


def test_a_local_folder_needs_no_drive(tmp_path):
    status = drive.status(tmp_path / "frames")
    assert status.mounted and not status.needs_drive
    assert status.free_bytes and status.total_bytes


async def test_only_mount_units_are_started():
    with pytest.raises(ArchiveError, match="not a mount unit"):
        await drive.mount("astropi.service")
