"""Simulated focuser.

Exists mainly so autofocus has something to optimise. Star size follows a
V-curve around the true focus position, with a little backlash and some
noise, which is the shape every autofocus routine is built to find.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from astropi.devices.base import Capability, ConnectionState, DeviceDescriptor, DeviceRole
from astropi.devices.focuser import FocuserStatus


@dataclass(slots=True)
class SimulatedFocuserConfig:
    max_position: int = 60_000
    best_position: int = 31_400
    #: Half-flux diameter in pixels at perfect focus, seeing-limited.
    best_hfd_px: float = 2.4
    #: How many steps away from focus doubles the star size.
    critical_focus_steps: float = 900.0
    steps_per_second: float = 4_000.0
    temperature_c: float = 8.0


class SimulatedFocuser:
    def __init__(self, config: SimulatedFocuserConfig | None = None, *, seed: int | None = None) -> None:
        self._config = config or SimulatedFocuserConfig()
        self._random = random.Random(seed)
        self._position = self._config.best_position - 2_500
        self._connection = ConnectionState.DISCONNECTED
        self._move_task: asyncio.Task[None] | None = None

    @property
    def descriptor(self) -> DeviceDescriptor:
        return DeviceDescriptor(
            id="sim-focuser",
            role=DeviceRole.FOCUSER,
            name="Simulated focuser",
            driver="simulator",
            capabilities=frozenset({Capability.ABSOLUTE_POSITION, Capability.TEMPERATURE}),
        )

    @property
    def connection_state(self) -> ConnectionState:
        return self._connection

    async def connect(self) -> None:
        self._connection = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        self._connection = ConnectionState.DISCONNECTED

    async def status(self) -> FocuserStatus:
        return FocuserStatus(
            position=self._position,
            is_moving=self._move_task is not None and not self._move_task.done(),
            max_position=self._config.max_position,
            temperature_c=self._config.temperature_c,
        )

    @property
    def hfd_px(self) -> float:
        """Star size at the current position - the V-curve the sim exposes.

        Hyperbolic rather than a literal V: real defocus is asymptotically
        linear but rounds off near the bottom, and a routine that assumed a
        sharp vertex would fit the real curve badly.
        """
        error = abs(self._position - self._config.best_position) / self._config.critical_focus_steps
        hfd = self._config.best_hfd_px * (1.0 + error * error) ** 0.5
        return hfd * (1.0 + self._random.gauss(0.0, 0.02))

    async def move_to(self, position: int) -> None:
        target = max(0, min(self._config.max_position, int(position)))
        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
        self._move_task = asyncio.create_task(self._run_move(target))

    async def move_by(self, steps: int) -> None:
        await self.move_to(self._position + int(steps))

    async def _run_move(self, target: int) -> None:
        distance = abs(target - self._position)
        await asyncio.sleep(min(distance / self._config.steps_per_second, 5.0))
        self._position = target

    async def wait_for_move(self, *, timeout_s: float = 60.0) -> None:
        task = self._move_task
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)

    async def halt(self) -> None:
        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
