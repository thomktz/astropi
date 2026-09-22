"""Simulated camera.

Renders whatever the simulated mount is *truly* pointing at - never what it
claims to be pointing at. Everything downstream therefore has to discover
the pointing the same way it would on real hardware: by solving the frame.

One class covers both sensors. The ASI2600MC Duo carries an imaging chip
and a guide chip in one body, so the imaging and guiding paths differ only
in their configuration, not in their code.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import numpy as np

from astropi.core.errors import CapabilityError, DeviceBusyError
from astropi.core.events import EventBus, Topic
from astropi.devices.backends.simulator.sky import OpticalTrain, render, visible_catalog
from astropi.devices.base import Capability, ConnectionState, DeviceDescriptor, DeviceRole
from astropi.devices.camera import (
    CameraState,
    CameraStatus,
    ControlKind,
    ControlSpec,
    CoolingStatus,
    ExposureRequest,
    Frame,
    FrameKind,
    SensorInfo,
)

if TYPE_CHECKING:
    from astropi.devices.backends.simulator.mount import SimulatedMount


class _FocusSource(Protocol):
    @property
    def hfd_px(self) -> float: ...


@dataclass(slots=True)
class SimulatedCameraConfig:
    """Defaults describe an ASI2600MC-class sensor on a 400 mm refractor.

    Sensor geometry is configuration rather than a hard-coded model number,
    so pointing this at different optics or a different camera is a config
    change and nothing more.
    """

    device_id: str = "sim-camera"
    name: str = "Simulated imaging camera"
    role: DeviceRole = DeviceRole.CAMERA
    width: int = 6248
    height: int = 4176
    pixel_size_um: float = 3.76
    bit_depth: int = 16
    focal_length_mm: float = 400.0
    has_cooling: bool = True
    #: A resistive heater on the sensor window. Cooled ZWO bodies have one.
    has_dew_heater: bool = True
    bayer_pattern: str | None = "RGGB"
    default_gain: int = 100
    default_offset: int = 30
    #: Camera angle relative to north, as a rotator or a loose clamp sets it.
    rotation_deg: float = 0.0
    ambient_c: float = 12.0
    #: Seconds of wall-clock per second of simulated exposure.
    time_scale: float = 1.0
    #: Extra seconds per frame, standing in for sensor readout and USB transfer.
    readout_s: float = 0.35
    download_noise: bool = True
    catalog: np.ndarray | None = field(default=None)


def guide_camera_config(main: SimulatedCameraConfig) -> SimulatedCameraConfig:
    """The Duo's second sensor: small, mono, sharing the same optics."""
    return SimulatedCameraConfig(
        device_id="sim-guide-camera",
        name="Simulated guide sensor",
        role=DeviceRole.GUIDE_CAMERA,
        width=1280,
        height=960,
        pixel_size_um=4.0,
        bit_depth=12,
        focal_length_mm=main.focal_length_mm,
        has_cooling=False,
        has_dew_heater=False,
        bayer_pattern=None,
        default_gain=250,
        rotation_deg=main.rotation_deg,
        readout_s=0.05,
        time_scale=main.time_scale,
        catalog=main.catalog,
    )


class SimulatedCamera:
    def __init__(
        self,
        mount: SimulatedMount,
        events: EventBus,
        config: SimulatedCameraConfig | None = None,
        *,
        focuser: _FocusSource | None = None,
        seed: int | None = None,
    ) -> None:
        self._mount = mount
        self._events = events
        self._config = config or SimulatedCameraConfig()
        self._focuser = focuser
        self._rng = np.random.default_rng(seed)

        self._state = CameraState.IDLE
        self._connection = ConnectionState.DISCONNECTED
        self._gain = self._config.default_gain
        self._offset = self._config.default_offset
        self._binning = 1
        self._cooling_on = False
        self._cooling_target: float | None = None
        self._dew_heater = False
        # Driver-level transport settings. They change nothing about the
        # rendered frame, but they exist on the real camera and a client
        # that cannot see them cannot diagnose dropped frames.
        self._usb_bandwidth = 80
        self._high_speed = False
        self._sensor_c = self._config.ambient_c
        self._cooling_changed_at = time.time()
        self._exposure_started: float | None = None
        self._exposure_duration = 0.0
        # What the exposure in flight is for. A client cannot otherwise
        # tell a live-view frame from a light frame, and a preview loop
        # makes the state cycle every couple of seconds.
        self._exposure_kind: FrameKind | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- device

    def set_pointing_source(self, mount) -> None:
        """Render from a different mount from now on.

        Called when the rig is switched between the simulated mount and a
        real one: the sensor is still imaginary either way, and what it
        draws should follow whichever mount is actually pointing.
        """
        self._mount = mount

    @property
    def descriptor(self) -> DeviceDescriptor:
        capabilities = {Capability.GAIN, Capability.OFFSET, Capability.BINNING, Capability.SUBFRAME}
        if self._config.has_cooling:
            capabilities.add(Capability.COOLING)
        if self._config.bayer_pattern:
            capabilities.add(Capability.BAYER)
        return DeviceDescriptor(
            id=self._config.device_id,
            role=self._config.role,
            name=self._config.name,
            driver="simulator",
            capabilities=frozenset(capabilities),
        )

    @property
    def connection_state(self) -> ConnectionState:
        return self._connection

    async def connect(self) -> None:
        self._connection = ConnectionState.CONNECTING
        await asyncio.sleep(0.1)
        self._connection = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        self._connection = ConnectionState.DISCONNECTED

    # ---------------------------------------------------------------- camera

    @property
    def sensor(self) -> SensorInfo:
        return SensorInfo(
            width=self._config.width,
            height=self._config.height,
            pixel_size_um=self._config.pixel_size_um,
            bit_depth=self._config.bit_depth,
            has_color_filter_array=self._config.bayer_pattern is not None,
            bayer_pattern=self._config.bayer_pattern,
        )

    @property
    def optics(self) -> OpticalTrain:
        return OpticalTrain(
            focal_length_mm=self._config.focal_length_mm,
            pixel_size_um=self._config.pixel_size_um,
            width=self._config.width,
            height=self._config.height,
            rotation_deg=self._config.rotation_deg,
        )

    async def status(self) -> CameraStatus:
        progress = None
        if self._exposure_started is not None and self._exposure_duration > 0:
            elapsed = time.time() - self._exposure_started
            progress = max(0.0, min(1.0, elapsed / self._exposure_duration))
        return CameraStatus(
            state=self._state,
            sensor=self.sensor,
            cooling=self._cooling_status(),
            gain=self._gain,
            offset=self._offset,
            binning=self._binning,
            exposure_progress=progress,
        )

    async def expose(self, request: ExposureRequest) -> Frame:
        if self._lock.locked():
            raise DeviceBusyError("an exposure is already in progress")

        async with self._lock:
            self._gain = request.gain if request.gain is not None else self._gain
            self._offset = request.offset if request.offset is not None else self._offset
            self._binning = max(1, request.binning)

            duration = request.duration_s * self._config.time_scale
            self._state = CameraState.EXPOSING
            self._exposure_kind = request.kind
            self._exposure_started = time.time()
            self._exposure_duration = max(duration, 1e-6)
            self._publish_state()

            try:
                await asyncio.sleep(duration)
                # Sample the pointing at the *end* of the exposure. Anything
                # that moved during it - drift, a guide correction - has
                # already happened by the time the shutter closes.
                center = self._mount.true_position()

                self._state = CameraState.READING
                self._publish_state()
                data = await asyncio.to_thread(self._render, request, center)

                self._state = CameraState.DOWNLOADING
                self._publish_state()
                await asyncio.sleep(self._config.readout_s)
            finally:
                self._exposure_started = None
                self._state = CameraState.IDLE
                self._publish_state()
                self._exposure_kind = None

            frame = Frame(
                data=data,
                request=request,
                sensor=self.sensor,
                metadata={
                    "gain": self._gain,
                    "offset": self._offset,
                    "binning": self._binning,
                    "sensor_temp_c": round(self._sensor_c, 2),
                    "pixel_scale_arcsec": self.optics.pixel_scale_arcsec * self._binning,
                    "rotation_deg": self._config.rotation_deg,
                    # Ground truth, for the simulated plate solver only. Real
                    # hardware has no such field; nothing but the simulator's
                    # own solver may read it.
                    "sim_true_ra_deg": center.ra_deg,
                    "sim_true_dec_deg": center.dec_deg,
                },
            )
            self._events.publish(
                Topic.CAMERA_FRAME,
                role=str(self._config.role),
                width=frame.shape[1],
                height=frame.shape[0],
                duration_s=request.duration_s,
                kind=str(request.kind),
            )
            return frame

    def _render(self, request: ExposureRequest, center) -> np.ndarray:
        binning = max(1, request.binning)
        roi = request.roi
        optics = self.optics

        width = (roi.width if roi else optics.width) // binning
        height = (roi.height if roi else optics.height) // binning
        # A sub-frame looks at a different patch of sky than the sensor
        # centre, so the tangent point has to move with it.
        crop = OpticalTrain(
            focal_length_mm=optics.focal_length_mm,
            pixel_size_um=optics.pixel_size_um * binning,
            width=max(width, 1),
            height=max(height, 1),
            rotation_deg=optics.rotation_deg,
        )

        catalog = self._config.catalog
        if catalog is not None:
            catalog = visible_catalog(catalog, center, crop.field_radius_deg * 1.3)

        hfd = self._focuser.hfd_px if self._focuser is not None else 3.0
        return render(
            center,
            crop,
            exposure_s=request.duration_s,
            hfd_px=hfd / binning,
            gain=self._gain,
            bit_depth=self._config.bit_depth,
            catalog=catalog,
            rng=self._rng,
        )

    async def abort_exposure(self) -> None:
        self._state = CameraState.IDLE
        self._exposure_started = None
        self._publish_state()

    async def set_cooling(self, enabled: bool, target_c: float | None = None) -> None:
        if not self._config.has_cooling:
            raise CapabilityError(f"{self._config.name} has no cooler")
        self._cooling_on = enabled
        self._cooling_target = target_c
        self._cooling_changed_at = time.time()
        self._publish_state()

    # -------------------------------------------------------------- controls

    async def controls(self) -> list[ControlSpec]:
        """The ASI2600's control set, minus the ones a simulator cannot fake.

        Names and ranges follow `ASI_CONTROL_TYPE` so the real adapter is a
        lookup table rather than a rewrite: `gain` is `ASI_GAIN`,
        `offset` is `ASI_OFFSET`, `usb_bandwidth` is
        `ASI_BANDWIDTHOVERLOAD`, and the two read-only entries are
        `ASI_TEMPERATURE` (which the SDK reports in tenths of a degree) and
        `ASI_COOLER_POWER_PERC`.
        """
        cooling = self._cooling_status()
        specs = [
            ControlSpec(
                name="gain",
                label="Gain",
                value=float(self._gain),
                writable=True,
                minimum=0,
                maximum=500,
                default=float(self._config.default_gain),
                step=10,
                unit="0.1 dB",
                description=(
                    "Sensor amplification, in tenths of a decibel as the driver "
                    "counts it. On this sensor 100 is both unity gain and where "
                    "the high-conversion-gain mode switches in."
                ),
            ),
            ControlSpec(
                name="offset",
                label="Offset",
                value=float(self._offset),
                writable=True,
                minimum=0,
                maximum=600,
                default=float(self._config.default_offset),
                step=5,
                unit="ADU",
                description=(
                    "Pedestal added before digitising, so read noise is not "
                    "clipped against zero. Raise it if the histogram touches "
                    "the left wall."
                ),
            ),
            ControlSpec(
                name="usb_bandwidth",
                label="USB bandwidth",
                value=float(self._usb_bandwidth),
                writable=True,
                minimum=40,
                maximum=100,
                default=80,
                step=5,
                unit="%",
                supports_auto=True,
                description=(
                    "Share of the USB link this camera may use. Lower it when "
                    "frames arrive corrupted on a Pi with a loaded bus."
                ),
            ),
            ControlSpec(
                name="high_speed_mode",
                label="High speed readout",
                value=float(self._high_speed),
                writable=True,
                kind=ControlKind.BOOLEAN,
                default=0,
                description="Faster download at 10 bits instead of 16. For focus, not for lights.",
            ),
        ]

        if self._config.has_cooling:
            specs += [
                ControlSpec(
                    name="cooler_on",
                    label="Cooler",
                    value=float(cooling.enabled),
                    writable=True,
                    kind=ControlKind.BOOLEAN,
                    default=0,
                ),
                ControlSpec(
                    name="target_temp",
                    label="Target",
                    value=cooling.target_c,
                    writable=True,
                    minimum=-40,
                    maximum=30,
                    default=-10,
                    unit="\u00b0C",
                    description="Whole degrees, as the driver takes it.",
                ),
                # Read-only, and exposed for exactly that reason: these two
                # are how you tell a cooler that is holding from one that
                # is flat out and about to lose the setpoint.
                ControlSpec(
                    name="sensor_temp",
                    label="Sensor",
                    value=cooling.sensor_c,
                    writable=False,
                    unit="\u00b0C",
                    step=0.1,
                    description="ASI_TEMPERATURE, reported in tenths of a degree.",
                ),
                ControlSpec(
                    name="cooler_power",
                    label="Power",
                    value=cooling.power_percent,
                    writable=False,
                    minimum=0,
                    maximum=100,
                    unit="%",
                    description=(
                        "Duty cycle. Sustained near 100% means no headroom "
                        "left - ease the setpoint up."
                    ),
                ),
            ]

        if self._config.has_dew_heater:
            specs.append(
                ControlSpec(
                    name="dew_heater",
                    label="Dew heater",
                    value=float(self._dew_heater),
                    writable=True,
                    kind=ControlKind.BOOLEAN,
                    default=0,
                    description=(
                        "Warms the sensor window. Frost on the glass at -20\u00b0C "
                        "ends a night as surely as cloud."
                    ),
                )
            )

        return specs

    async def set_control(self, name: str, value: float) -> ControlSpec:
        specs = {spec.name: spec for spec in await self.controls()}
        spec = specs.get(name)
        if spec is None:
            raise CapabilityError(f"{self._config.name} has no control {name!r}")
        if not spec.writable:
            raise CapabilityError(f"{name} is read-only")

        clamped = value
        if spec.minimum is not None:
            clamped = max(spec.minimum, clamped)
        if spec.maximum is not None:
            clamped = min(spec.maximum, clamped)

        if name == "gain":
            self._gain = int(clamped)
        elif name == "offset":
            self._offset = int(clamped)
        elif name == "usb_bandwidth":
            self._usb_bandwidth = int(clamped)
        elif name == "high_speed_mode":
            self._high_speed = bool(clamped)
        elif name == "dew_heater":
            self._dew_heater = bool(clamped)
        elif name == "cooler_on":
            await self.set_cooling(bool(clamped), self._cooling_target)
        elif name == "target_temp":
            # Changing the setpoint while the cooler runs is a retarget,
            # not a switch-on: passing `self._cooling_on` keeps it as it is.
            await self.set_cooling(self._cooling_on, float(clamped))

        self._publish_state()
        return next(spec for spec in await self.controls() if spec.name == name)

    def _cooling_status(self) -> CoolingStatus:
        if not self._config.has_cooling:
            return CoolingStatus(supported=False)

        # First-order thermal lag toward the setpoint, so a sequence that
        # waits for the sensor to reach temperature actually has to wait.
        target = self._cooling_target if self._cooling_on and self._cooling_target is not None else None
        goal = target if target is not None else self._config.ambient_c
        elapsed = time.time() - self._cooling_changed_at
        self._sensor_c += (goal - self._sensor_c) * min(1.0, elapsed / 45.0)
        self._cooling_changed_at = time.time()

        delta = max(0.0, self._config.ambient_c - self._sensor_c)
        return CoolingStatus(
            supported=True,
            enabled=self._cooling_on,
            target_c=self._cooling_target,
            sensor_c=round(self._sensor_c, 2),
            power_percent=round(min(100.0, delta * 3.2), 1) if self._cooling_on else 0.0,
            dew_heater=self._dew_heater if self._config.has_dew_heater else None,
        )

    def _publish_state(self) -> None:
        cooling = self._cooling_status()
        self._events.publish(
            Topic.CAMERA_STATE,
            role=str(self._config.role),
            state=str(self._state),
            kind=None if self._exposure_kind is None else str(self._exposure_kind),
            # The exposure's length and start, so a client can count down
            # locally. Streaming progress from here instead would put a
            # message per second per viewer on the wire for something the
            # browser can work out from two numbers.
            exposure_s=self._exposure_duration if self._exposure_started else None,
            exposure_started_at=self._exposure_started,
            gain=self._gain,
            offset=self._offset,
            binning=self._binning,
            sensor_c=cooling.sensor_c,
            cooling_enabled=cooling.enabled,
            cooling_target_c=cooling.target_c,
            cooling_power=cooling.power_percent,
            dew_heater=cooling.dew_heater,
        )
