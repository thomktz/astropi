"""Device registry.

One place that knows which concrete backend is filling which role, so the
rest of the application asks for "the mount" instead of constructing one.
Swapping the simulator for real hardware is a registration change and
nothing else.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from astropi.core.errors import DeviceNotFoundError, NotConnectedError
from astropi.core.events import EventBus, Topic
from astropi.devices.base import ConnectionState, Device, DeviceDescriptor, DeviceRole

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Device)


class DeviceRegistry:
    """Holds the device filling each role and reports connection changes."""

    def __init__(self, events: EventBus) -> None:
        self._events = events
        self._devices: dict[DeviceRole, Device] = {}

    def register(self, role: DeviceRole, device: Device) -> None:
        """Bind a device to a role.

        A device may be registered under several roles - the ASI2600MC Duo's
        two sensors arrive as one object serving `CAMERA` and `GUIDE_CAMERA`
        - so this takes the role explicitly rather than reading it off the
        descriptor.
        """
        self._devices[role] = device
        logger.info("registered %s as %s", device.descriptor.name, role)
        self._publish(role, device)

    def unregister(self, role: DeviceRole) -> None:
        self._devices.pop(role, None)

    def get(self, role: DeviceRole, kind: type[T]) -> T:
        """Return the device for a role, or raise if absent.

        `kind` is the protocol the caller needs; it narrows the static type
        and documents the requirement at the call site.
        """
        device = self._devices.get(role)
        if device is None:
            raise DeviceNotFoundError(f"no device registered for role {role!r}")
        return device  # type: ignore[return-value]

    def require_connected(self, role: DeviceRole, kind: type[T]) -> T:
        device = self.get(role, kind)
        if device.connection_state is not ConnectionState.CONNECTED:
            raise NotConnectedError(f"{role} is {device.connection_state}")
        return device

    def has(self, role: DeviceRole) -> bool:
        return role in self._devices

    def descriptors(self) -> dict[DeviceRole, DeviceDescriptor]:
        return {role: device.descriptor for role, device in self._devices.items()}

    def roles(self) -> list[DeviceRole]:
        return list(self._devices)

    async def connect_all(self) -> None:
        for role, device in self._devices.items():
            try:
                await device.connect()
            except Exception:
                # One dead device must not prevent the rest of the rig from
                # coming up - a failed focuser should still leave a usable
                # mount and camera.
                logger.exception("failed to connect %s", role)
            self._publish(role, device)

    async def disconnect_all(self) -> None:
        for role, device in self._devices.items():
            try:
                await device.disconnect()
            except Exception:
                logger.exception("failed to disconnect %s", role)
            self._publish(role, device)

    def _publish(self, role: DeviceRole, device: Device) -> None:
        descriptor = device.descriptor
        self._events.publish(
            Topic.DEVICE_STATE,
            role=str(role),
            id=descriptor.id,
            name=descriptor.name,
            driver=descriptor.driver,
            capabilities=sorted(str(c) for c in descriptor.capabilities),
            connection=str(device.connection_state),
        )
