"""Camera contract.

Covers both jobs a sensor does here: long imaging exposures and the short,
fast, small-ROI frames a guide loop needs. They are the same operation with
very different parameters, so one protocol serves both rather than splitting
into an imaging and a guiding interface that would drift apart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from astropi.devices.base import Device

if TYPE_CHECKING:
    import numpy as np


class FrameKind(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    FLAT = "flat"
    BIAS = "bias"
    PREVIEW = "preview"
    GUIDE = "guide"


class CameraState(StrEnum):
    IDLE = "idle"
    EXPOSING = "exposing"
    READING = "reading"
    DOWNLOADING = "downloading"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Roi:
    """A sensor region, in unbinned pixels from the top-left."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ExposureRequest:
    duration_s: float
    gain: int | None = None
    offset: int | None = None
    binning: int = 1
    roi: Roi | None = None
    kind: FrameKind = FrameKind.LIGHT


@dataclass(frozen=True, slots=True)
class SensorInfo:
    width: int
    height: int
    pixel_size_um: float
    bit_depth: int
    has_color_filter_array: bool = False
    bayer_pattern: str | None = None
    max_binning: int = 4


@dataclass(slots=True)
class Frame:
    """One captured image plus the metadata needed to interpret it.

    `data` is a 2-D array of raw ADU, kept undebayered and unstretched.
    Everything downstream - plate solving, star detection, the preview
    renderer - wants the raw sensor values; each applies its own
    transformation, and doing it once up front would throw away
    information one of them needs.
    """

    data: np.ndarray
    request: ExposureRequest
    sensor: SensorInfo
    started_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.data.shape[0]), int(self.data.shape[1]))


@dataclass(frozen=True, slots=True)
class CoolingStatus:
    """The cooler, as every driver models it.

    `sensor_c` and `power_percent` are read-only measurements, not settings:
    ZWO reports them as `ASI_TEMPERATURE` and `ASI_COOLER_POWER_PERC`, INDI
    as `CCD_TEMPERATURE` and `CCD_COOLER_POWER`. Power is the one worth
    watching - a cooler pinned near 100% has no headroom and will drift off
    setpoint as the night warms or cools.
    """

    supported: bool
    enabled: bool = False
    target_c: float | None = None
    sensor_c: float | None = None
    power_percent: float | None = None
    dew_heater: bool | None = None
    """The window heater, where there is one. `None` means no such control."""


class ControlKind(StrEnum):
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class ControlSpec:
    """One named camera setting, with its limits and whether it can be set.

    Deliberately the shape the real drivers already use, so an adapter is a
    translation rather than a design: ZWO's `ASIGetControlCaps` hands back
    exactly this (name, min, max, default, `IsWritable`, `IsAutoSupported`),
    INDI publishes number and switch vectors carrying the same, and ASCOM
    exposes per-property ranges.

    Read-only entries belong in this list too. A sensor temperature and a
    cooler duty cycle are as much part of the control set as gain is; the
    only difference is which direction they travel, and `writable` says so
    rather than the value being hidden from the client entirely.
    """

    name: str
    label: str
    value: float | None
    writable: bool
    kind: ControlKind = ControlKind.NUMBER
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None
    step: float = 1.0
    unit: str | None = None
    supports_auto: bool = False
    auto: bool = False
    description: str | None = None


@dataclass(frozen=True, slots=True)
class CameraStatus:
    state: CameraState
    sensor: SensorInfo
    cooling: CoolingStatus
    gain: int | None = None
    offset: int | None = None
    binning: int = 1
    exposure_progress: float | None = None


@runtime_checkable
class Camera(Protocol):
    """An imaging or guiding sensor."""

    @property
    def sensor(self) -> SensorInfo: ...

    async def status(self) -> CameraStatus: ...

    async def expose(self, request: ExposureRequest) -> Frame:
        """Capture one frame and return it. Cancellable via task cancellation."""
        ...

    async def abort_exposure(self) -> None: ...

    async def set_cooling(self, enabled: bool, target_c: float | None = None) -> None:
        """Raise `CapabilityError` if the camera has no cooler."""
        ...

    async def controls(self) -> list[ControlSpec]:
        """Every setting this camera has, writable or not.

        Advertised rather than assumed, because the panel that drives a
        cooled colour main camera is the same panel that drives an uncooled
        mono guide head.
        """
        ...

    async def set_control(self, name: str, value: float) -> ControlSpec:
        """Set one control by name, returning it as the camera now reports it.

        Raise `CapabilityError` for a name the camera does not have or a
        control it will not let you write.
        """
        ...


@runtime_checkable
class CameraDevice(Camera, Device, Protocol):
    """A camera that is also a managed device. What the registry stores."""
