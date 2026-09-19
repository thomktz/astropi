"""Focuser contract.

Absolute positioning is the assumption; relative-only focusers emulate it by
tracking their own step count, which is what every autofocus routine needs
in order to return to a known best position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from astropi.devices.base import Device


@dataclass(frozen=True, slots=True)
class FocuserStatus:
    position: int
    is_moving: bool
    max_position: int
    temperature_c: float | None = None


@runtime_checkable
class Focuser(Device, Protocol):
    async def status(self) -> FocuserStatus: ...

    async def move_to(self, position: int) -> None: ...

    async def move_by(self, steps: int) -> None: ...

    async def wait_for_move(self, *, timeout_s: float = 60.0) -> None: ...

    async def halt(self) -> None: ...
