"""The device contract every hardware backend implements.

The whole rest of the application is written against these protocols, never
against a vendor SDK. A ZWO camera reached over the ASI SDK, the same camera
reached through INDI, and the built-in simulator are interchangeable from
the point of view of a sequence, a route handler or the UI.

Capabilities are advertised rather than assumed. Mounts differ enormously in
what they will admit to - a Star Adventurer GTi has no pier side to report
and no absolute encoder to query - so callers ask before they act, and the
UI hides controls that the connected hardware cannot honour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DeviceRole(StrEnum):
    """What a device is used *for* in the rig.

    Roles are distinct from devices because one piece of hardware can fill
    several. The ASI2600MC Duo carries an imaging sensor and a guide sensor
    in one body on one USB connection, so a single backend object registers
    under both `CAMERA` and `GUIDE_CAMERA`.
    """

    MOUNT = "mount"
    CAMERA = "camera"
    GUIDE_CAMERA = "guide_camera"
    GUIDER = "guider"
    FOCUSER = "focuser"
    FILTER_WHEEL = "filter_wheel"
    ROTATOR = "rotator"


class Capability(StrEnum):
    """Optional behaviours a device may support."""

    # Mount
    SLEW = "slew"
    SYNC = "sync"
    PARK = "park"
    TRACKING_RATES = "tracking_rates"
    PULSE_GUIDE = "pulse_guide"
    PIER_SIDE = "pier_side"
    MERIDIAN_FLIP = "meridian_flip"
    # Camera
    COOLING = "cooling"
    GAIN = "gain"
    OFFSET = "offset"
    BINNING = "binning"
    SUBFRAME = "subframe"
    BAYER = "bayer"
    # Focuser
    ABSOLUTE_POSITION = "absolute_position"
    TEMPERATURE = "temperature"


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    """Identity and advertised abilities of a device."""

    id: str
    role: DeviceRole
    name: str
    driver: str
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    #: Whatever the hardware says about itself that has no field of its
    #: own - a firmware version, a serial port, counts per revolution. For
    #: showing in the setup panel and for putting in a bug report, never
    #: for deciding behaviour: that is what capabilities are for.
    details: dict[str, str] = field(default_factory=dict)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities


@runtime_checkable
class Device(Protocol):
    """Lifecycle shared by every device."""

    @property
    def descriptor(self) -> DeviceDescriptor: ...

    @property
    def connection_state(self) -> ConnectionState: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...
