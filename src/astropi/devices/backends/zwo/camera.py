"""A ZWO ASI camera, driven through ZWO's own SDK.

One instance per sensor. The ASI2600MC Duo presents its imaging and guide
sensors to the SDK as two cameras on one USB connection, so the rig gets
two of these - one registered as `CAMERA`, one as `GUIDE_CAMERA` - opened
from the same library.

The SDK is blocking and not async-aware, so every call into it runs on a
worker thread. Exposures are started and then polled rather than made
with the SDK's own blocking capture, which is what lets a cancelled task
actually stop the sensor instead of waiting out a five-minute sub.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from astropi.core.errors import CapabilityError, DeviceBusyError, DeviceError, NotConnectedError
from astropi.core.events import EventBus, Topic
from astropi.devices.backends.zwo.sdk import Sdk, SdkCamera, ZwoSdk, choose
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

logger = logging.getLogger(__name__)

# Values of the SDK's enums. Fixed by ZWO's header, so written out here
# rather than read off `zwoasi` - which would make this module need the
# SDK installed just to be imported.
IMG_RAW16 = 2
EXP_IDLE, EXP_WORKING, EXP_SUCCESS, EXP_FAILED = 0, 1, 2, 3
BAYER_PATTERNS = {0: "RGGB", 1: "BGGR", 2: "GRBG", 3: "GBRG"}


@dataclass(frozen=True, slots=True)
class _Control:
    """How one of our control names maps onto the SDK's."""

    sdk_name: str
    label: str
    kind: ControlKind = ControlKind.NUMBER
    unit: str | None = None
    #: The SDK reports `Temperature` in tenths of a degree.
    scale: float = 1.0
    step: float = 1.0
    description: str | None = None


#: Ours to theirs. Anything the camera reports that is not in here is left
#: out of the panel; most of the rest (white balance, auto-exposure
#: targets, flip) is for video use and means nothing to raw astro frames.
CONTROLS: dict[str, _Control] = {
    "gain": _Control(
        "Gain",
        "Gain",
        unit="0.1 dB",
        step=10,
        description="Sensor amplification. On the 2600 sensor, 100 is unity and where HCG mode switches in.",
    ),
    "offset": _Control(
        "Offset",
        "Offset",
        unit="ADU",
        step=5,
        description="Pedestal added before digitising, so read noise is not clipped against zero.",
    ),
    "usb_bandwidth": _Control(
        "BandWidth",
        "USB bandwidth",
        unit="%",
        step=5,
        description="Share of the USB link this camera may use. Lower it if frames arrive corrupted.",
    ),
    "high_speed_mode": _Control(
        "HighSpeedMode",
        "High speed readout",
        kind=ControlKind.BOOLEAN,
        description="Faster download at fewer bits. For focus, not for lights.",
    ),
    "cooler_on": _Control("CoolerOn", "Cooler", kind=ControlKind.BOOLEAN),
    "target_temp": _Control(
        "TargetTemp", "Target", unit="°C", description="Whole degrees, as the driver takes it."
    ),
    "sensor_temp": _Control("Temperature", "Sensor", unit="°C", scale=0.1, step=0.1),
    "cooler_power": _Control(
        # Sic: the 2600 Duo calls it this, not `CoolerPowerPerc`.
        "CoolPowerPerc",
        "Power",
        unit="%",
        description="Duty cycle. Sustained near 100% means no headroom left - ease the setpoint up.",
    ),
    "dew_heater": _Control(
        "AntiDewHeater",
        "Dew heater",
        kind=ControlKind.BOOLEAN,
        description="Warms the sensor window, which frosts over at low setpoints.",
    ),
}


@dataclass(slots=True)
class ZwoCameraConfig:
    role: DeviceRole = DeviceRole.CAMERA
    #: A piece of the camera's name to pick it by. Empty picks by size:
    #: the biggest sensor for imaging, the smallest other one for guiding.
    match: str | None = None
    #: Optics, for the plate scale written into each frame's metadata.
    focal_length_mm: float = 400.0
    rotation_deg: float = 0.0
    #: Applied at connect. ZWO's own defaults are tuned for video - the
    #: 2600 Duo comes up at gain 200 and offset 1. `None` keeps the SDK's.
    default_gain: int | None = 100
    default_offset: int | None = 30
    usb_bandwidth: int = 80
    #: How fast the cooler setpoint is walked toward its target. Driving
    #: straight there runs the cooler flat out and invites condensation.
    cooling_ramp_c_per_min: float = 3.0
    #: Extra wait beyond the exposure for readout and transfer before an
    #: exposure is given up on. A full 2600 frame takes a few seconds on a
    #: Pi's USB 2 hub, so this is generous.
    readout_timeout_s: float = 30.0
    poll_interval_s: float = 0.05
    #: The narrowest region the sensor is asked to read, in binned pixels.
    #: The Duo's guide sensor fails every exposure narrower than about
    #: 160 pixels, whatever its height; a guide box narrower than this is
    #: read wider and cut down here.
    min_read_width: int = 256
    #: How often sensor temperature and cooler power are read and pushed
    #: to the dashboard. Nothing else would announce a temperature change.
    telemetry_interval_s: float = 5.0


class ZwoCamera:
    def __init__(
        self,
        events: EventBus,
        config: ZwoCameraConfig | None = None,
        *,
        sdk: Sdk | None = None,
        claimed: set[int] | None = None,
    ) -> None:
        self._events = events
        self._config = config or ZwoCameraConfig()
        self._sdk = sdk or ZwoSdk()
        #: Indices already taken by another `ZwoCamera` in this process, so
        #: the guide role does not open the imaging sensor a second time.
        self._claimed = claimed if claimed is not None else set()

        self._camera: SdkCamera | None = None
        #: What was last sent to the sensor, so it is not sent again.
        self._applied: dict[str, object] = {}
        self._index: int | None = None
        self._props: dict[str, Any] = {}
        self._caps: dict[str, dict[str, Any]] = {}
        self._connection = ConnectionState.DISCONNECTED
        self._state = CameraState.IDLE
        # Serialises SDK calls on this sensor. The library is thread-safe
        # across cameras but a status poll racing a control write on the
        # same one is asking for a confused firmware.
        self._io = threading.Lock()
        self._lock = asyncio.Lock()

        self._gain = self._config.default_gain
        self._offset = self._config.default_offset
        self._binning = 1
        self._exposure_started: float | None = None
        self._exposure_duration = 0.0
        self._exposure_kind: FrameKind | None = None

        self._cooling_target: float | None = None
        self._cooling_on = False
        self._ramp_task: asyncio.Task | None = None
        self._telemetry_task: asyncio.Task | None = None
        self._cooling = CoolingStatus(supported=False)

    # ---------------------------------------------------------------- device

    @property
    def descriptor(self) -> DeviceDescriptor:
        name = str(self._props.get("Name", "ZWO camera (not connected)"))
        capabilities = {Capability.GAIN, Capability.BINNING, Capability.SUBFRAME, Capability.VIDEO}
        if "Offset" in self._caps:
            capabilities.add(Capability.OFFSET)
        if self._props.get("IsCoolerCam"):
            capabilities.add(Capability.COOLING)
        if self._props.get("IsColorCam"):
            capabilities.add(Capability.BAYER)
        details = {}
        if self._props:
            details = {
                "sdk_index": str(self._index),
                "resolution": f"{self._props['MaxWidth']}x{self._props['MaxHeight']}",
                "bit_depth": str(self._props.get("BitDepth")),
                "usb3": "yes" if self._props.get("IsUSB3Camera") else "no",
                "usb3_host": "yes" if self._props.get("IsUSB3Host") else "no",
            }
        return DeviceDescriptor(
            id=f"zwo-{self._config.role}",
            role=self._config.role,
            name=name.removeprefix("ZWO ") if self._props else name,
            driver="zwo-asi",
            capabilities=frozenset(capabilities),
            details=details,
        )

    @property
    def connection_state(self) -> ConnectionState:
        return self._connection

    async def connect(self) -> None:
        if self._connection is ConnectionState.CONNECTED:
            return
        self._connection = ConnectionState.CONNECTING
        try:
            await asyncio.to_thread(self._open)
        except Exception:
            self._connection = ConnectionState.ERROR
            raise
        self._connection = ConnectionState.CONNECTED
        logger.info("connected %s as %s", self._props.get("Name"), self._config.role)
        if self._props.get("IsCoolerCam"):
            self._telemetry_task = asyncio.create_task(self._watch_cooling())
        self._publish_state()

    def _open(self) -> None:
        found = self._sdk.cameras()
        imaging = self._config.role is DeviceRole.CAMERA
        exclude = next(iter(self._claimed), None) if not imaging else None
        chosen = choose(found, self._config.match, largest=imaging, exclude=exclude)
        camera = self._sdk.open(chosen.index)
        self._applied = {}
        try:
            self._props = camera.get_camera_property()
            self._caps = camera.get_controls()
            camera.set_roi(bins=1, image_type=IMG_RAW16)
            for name, value in (("Gain", self._gain), ("Offset", self._offset)):
                if value is not None:
                    self._write(camera, name, value)
            self._gain = self._read(camera, "Gain")
            self._offset = self._read(camera, "Offset")
            self._write(camera, "BandWidth", self._config.usb_bandwidth)
            if self._props.get("IsCoolerCam"):
                # What the camera is already doing, rather than assuming
                # off: a restart mid-night should not lose the setpoint.
                self._cooling_on = bool(self._read(camera, "CoolerOn"))
                self._cooling_target = self._read(camera, "TargetTemp")
        except Exception:
            camera.close()
            raise
        self._camera = camera
        self._index = chosen.index
        self._claimed.add(chosen.index)

    async def disconnect(self) -> None:
        for task in (self._ramp_task, self._telemetry_task):
            if task is not None:
                task.cancel()
        self._ramp_task = self._telemetry_task = None
        camera, self._camera = self._camera, None
        if camera is not None:
            # The cooler is left as it is. Switching it off here would undo
            # a setpoint over a service restart; switching it off at the end
            # of the night is the operator's call, and the camera warms
            # slowly on its own once power goes.
            await asyncio.to_thread(self._close, camera)
        if self._index is not None:
            self._claimed.discard(self._index)
        self._connection = ConnectionState.DISCONNECTED
        self._publish_state()

    def _close(self, camera: SdkCamera) -> None:
        with self._io:
            with contextlib.suppress(Exception):
                camera.stop_exposure()
            camera.close()

    # ---------------------------------------------------------------- camera

    @property
    def sensor(self) -> SensorInfo:
        props = self._props
        if not props:
            return SensorInfo(width=0, height=0, pixel_size_um=0.0, bit_depth=16)
        is_color = bool(props.get("IsColorCam"))
        bins = [int(b) for b in props.get("SupportedBins", [1]) if b]
        return SensorInfo(
            width=int(props["MaxWidth"]),
            height=int(props["MaxHeight"]),
            pixel_size_um=float(props["PixelSize"]),
            # RAW16 is always scaled to the full 16 bits, whatever the ADC:
            # the guide sensor's 12-bit values arrive multiplied by 16. Its
            # real depth is in the descriptor's details.
            bit_depth=16,
            has_color_filter_array=is_color,
            bayer_pattern=BAYER_PATTERNS.get(int(props.get("BayerPattern", 0))) if is_color else None,
            max_binning=max(bins) if bins else 1,
        )

    def _require(self) -> SdkCamera:
        if self._camera is None:
            raise NotConnectedError(f"{self._config.role} is not connected")
        return self._camera

    async def status(self) -> CameraStatus:
        progress = None
        if self._exposure_started is not None and self._exposure_duration > 0:
            elapsed = time.time() - self._exposure_started
            progress = max(0.0, min(1.0, elapsed / self._exposure_duration))
        return CameraStatus(
            state=self._state,
            sensor=self.sensor,
            cooling=await self._cooling_status(),
            gain=self._gain,
            offset=self._offset,
            binning=self._binning,
            exposure_progress=progress,
        )

    async def expose(self, request: ExposureRequest) -> Frame:
        camera = self._require()
        if self._lock.locked():
            raise DeviceBusyError("an exposure is already in progress")

        async with self._lock:
            gain = request.gain if request.gain is not None else self._gain
            offset = request.offset if request.offset is not None else self._offset
            binning = max(1, min(request.binning, self.sensor.max_binning))
            read, crop = await asyncio.to_thread(self._prepare, camera, request, gain, offset, binning)
            self._gain, self._offset, self._binning = gain, offset, binning

            self._state = CameraState.EXPOSING
            self._exposure_kind = request.kind
            self._exposure_started = time.time()
            self._exposure_duration = max(request.duration_s, 1e-6)
            self._publish_state()
            try:
                is_dark = request.kind in (FrameKind.DARK, FrameKind.BIAS)
                await asyncio.to_thread(self._call, camera.start_exposure, is_dark)
                await self._wait_for_exposure(camera, request.duration_s)

                self._state = CameraState.DOWNLOADING
                self._publish_state()
                raw = await asyncio.to_thread(self._call, camera.get_data_after_exposure)
            except BaseException:
                # Cancellation included: a task that is stopped must stop
                # the sensor too, or the next exposure finds it busy.
                await asyncio.to_thread(self._stop_quietly, camera)
                raise
            finally:
                self._exposure_started = None
                self._exposure_kind = None
                self._state = CameraState.IDLE
                self._publish_state()

            read_width, read_height = read
            left, top, width, height = crop
            data = np.frombuffer(raw, dtype="<u2").reshape(read_height, read_width)
            data = data[top : top + height, left : left + width].copy()
            sensor_c = None
            if "Temperature" in self._caps:
                sensor_c = await asyncio.to_thread(self._read, camera, "Temperature")
            frame = Frame(
                data=data,
                request=request,
                sensor=self.sensor,
                metadata={
                    "gain": gain,
                    "offset": offset,
                    "binning": binning,
                    "sensor_temp_c": None if sensor_c is None else sensor_c / 10.0,
                    "pixel_scale_arcsec": 206.264806
                    * self.sensor.pixel_size_um
                    * binning
                    / self._config.focal_length_mm,
                    "rotation_deg": self._config.rotation_deg,
                    "camera": self.descriptor.name,
                },
            )
            self._events.publish(
                Topic.CAMERA_FRAME,
                role=str(self._config.role),
                width=width,
                height=height,
                duration_s=request.duration_s,
                kind=str(request.kind),
            )
            return frame

    async def stream(self, request: ExposureRequest) -> AsyncIterator[Frame]:
        """Frames back to back, in the SDK's video mode, until the caller stops.

        A single exposure re-arms the sensor every time - about 570 ms on
        the 2600 whatever the exposure - which is what made a live view a
        slideshow. Video mode keeps it running: about six full frames a
        second at short exposures, limited by the sensor's own readout.

        The camera is held for as long as the stream is iterated. Close the
        iterator (leave the `async for`, or `aclose()` it) to give it back.
        """
        camera = self._require()
        if self._lock.locked():
            raise DeviceBusyError("an exposure is already in progress")

        async with self._lock:
            gain = request.gain if request.gain is not None else self._gain
            offset = request.offset if request.offset is not None else self._offset
            binning = max(1, min(request.binning, self.sensor.max_binning))
            read, crop = await asyncio.to_thread(self._prepare, camera, request, gain, offset, binning)
            self._gain, self._offset, self._binning = gain, offset, binning
            read_width, read_height = read
            left, top, width, height = crop
            buffer = bytearray(read_width * read_height * 2)
            # Twice the exposure, plus room for the first frame, which
            # takes far longer than the rest while the sensor spins up.
            timeout_ms = int(request.duration_s * 2000) + 3000

            self._state = CameraState.EXPOSING
            self._exposure_kind = request.kind
            self._exposure_started = None
            self._publish_state()
            await asyncio.to_thread(self._call, camera.start_video_capture)
            try:
                while True:
                    started = time.time()
                    # Not under `_io`: this blocks for a whole exposure, and
                    # the cooler readings must not wait behind it. The SDK
                    # allows control reads alongside a video read.
                    await asyncio.to_thread(camera.get_video_data, timeout_ms, buffer)
                    data = np.frombuffer(buffer, dtype="<u2").reshape(read_height, read_width)
                    yield Frame(
                        data=data[top : top + height, left : left + width].copy(),
                        request=request,
                        sensor=self.sensor,
                        started_at=started,
                        metadata={
                            "gain": gain,
                            "offset": offset,
                            "binning": binning,
                            "sensor_temp_c": self._cooling.sensor_c,
                            "pixel_scale_arcsec": 206.264806
                            * self.sensor.pixel_size_um
                            * binning
                            / self._config.focal_length_mm,
                            "rotation_deg": self._config.rotation_deg,
                            "camera": self.descriptor.name,
                            "video": True,
                        },
                    )
            finally:
                await asyncio.to_thread(self._stop_video_quietly, camera)
                self._exposure_kind = None
                self._state = CameraState.IDLE
                self._publish_state()

    def _stop_video_quietly(self, camera: SdkCamera) -> None:
        try:
            self._call(camera.stop_video_capture)
        except Exception:
            logger.warning("could not stop video capture on %s", self.descriptor.name, exc_info=True)

    def _prepare(
        self, camera: SdkCamera, request: ExposureRequest, gain: int, offset: int, binning: int
    ) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
        """Set gain, offset, exposure and region.

        Returns the size the sensor will read, and where in that the
        requested region sits - the same thing unless the request was
        narrower than the sensor will read.
        """
        sensor = self.sensor
        roi = request.roi
        x, y = (roi.x, roi.y) if roi else (0, 0)
        width = (roi.width if roi else sensor.width) // binning
        height = (roi.height if roi else sensor.height) // binning
        # The SDK wants widths in multiples of 8 and heights in multiples
        # of 2, in binned pixels. Rounding down keeps the region inside
        # the sensor; the guide loop asks for whatever box it likes.
        width = max(8, width - width % 8)
        height = max(2, height - height % 2)
        full_width, full_height = sensor.width // binning, sensor.height // binning
        start_x = max(0, min(x // binning, full_width - width))
        start_y = max(0, min(y // binning, full_height - height))

        read_width = min(max(width, self._config.min_read_width), full_width - full_width % 8)
        # Widen around the box, then pull back inside the sensor.
        read_x = max(0, min(start_x - (read_width - width) // 2, full_width - read_width))

        # Only what changed is sent. Re-sending an identical region before
        # every exposure made the Duo's guide sensor fail every frame after
        # the first: it has no frame buffer, and a region write between
        # exposures leaves it out of step with the transfer.
        wanted = {
            "roi": (read_x, start_y, read_width, height, binning),
            "Gain": gain,
            "Offset": offset if "Offset" in self._caps else None,
            "Exposure": round(request.duration_s * 1_000_000),
        }
        with self._io:
            if self._applied.get("roi") != wanted["roi"]:
                camera.set_roi(
                    start_x=read_x,
                    start_y=start_y,
                    width=read_width,
                    height=height,
                    bins=binning,
                    image_type=IMG_RAW16,
                )
            for name in ("Gain", "Offset", "Exposure"):
                if wanted[name] is not None and self._applied.get(name) != wanted[name]:
                    self._write(camera, name, wanted[name])
            self._applied.update(wanted)
        return (read_width, height), (start_x - read_x, 0, width, height)

    async def _wait_for_exposure(self, camera: SdkCamera, duration_s: float) -> None:
        # Sleep through most of it without touching the bus, then poll.
        # Polling a long exposure every 50 ms buys nothing.
        if duration_s > 1.0:
            await asyncio.sleep(duration_s - 0.5)
        deadline = time.monotonic() + duration_s + self._config.readout_timeout_s
        while True:
            status = await asyncio.to_thread(self._call, camera.get_exposure_status)
            if status == EXP_SUCCESS:
                return
            if status == EXP_FAILED:
                raise DeviceError(f"{self.descriptor.name}: the exposure failed")
            if status == EXP_IDLE and time.time() - (self._exposure_started or 0) > duration_s + 1.0:
                raise DeviceError(f"{self.descriptor.name}: the exposure stopped without a frame")
            if time.monotonic() > deadline:
                raise DeviceError(
                    f"{self.descriptor.name}: no frame {self._config.readout_timeout_s:.0f} s "
                    "after the exposure should have ended"
                )
            await asyncio.sleep(self._config.poll_interval_s)

    def _stop_quietly(self, camera: SdkCamera) -> None:
        try:
            self._call(camera.stop_exposure)
        except Exception:
            logger.warning("could not stop the exposure on %s", self.descriptor.name, exc_info=True)

    async def abort_exposure(self) -> None:
        if self._camera is not None:
            await asyncio.to_thread(self._stop_quietly, self._camera)
        self._state = CameraState.IDLE
        self._exposure_started = None
        self._publish_state()

    # --------------------------------------------------------------- cooling

    async def set_cooling(self, enabled: bool, target_c: float | None = None) -> None:
        camera = self._require()
        if not self._props.get("IsCoolerCam"):
            raise CapabilityError(f"{self.descriptor.name} has no cooler")
        if self._ramp_task is not None:
            self._ramp_task.cancel()
            self._ramp_task = None

        self._cooling_on = enabled
        if target_c is not None:
            self._cooling_target = round(target_c)
        await asyncio.to_thread(self._write_locked, camera, "CoolerOn", int(enabled))
        if enabled and self._cooling_target is not None:
            self._ramp_task = asyncio.create_task(self._ramp(camera, self._cooling_target))
        self._publish_state()

    async def _ramp(self, camera: SdkCamera, target: float) -> None:
        """Walk the setpoint to `target` a degree at a time.

        Starts from the sensor as it is now, so switching on at 15 degrees
        with a -10 target takes about eight minutes rather than asking the
        cooler for 25 degrees at once.
        """
        rate = self._config.cooling_ramp_c_per_min
        try:
            current = await asyncio.to_thread(self._read_locked, camera, "Temperature") / 10.0
            setpoint = round(current)
            step = -1 if target < setpoint else 1
            while setpoint != target:
                setpoint += step
                await asyncio.to_thread(self._write_locked, camera, "TargetTemp", int(setpoint))
                self._publish_state()
                if setpoint != target:
                    await asyncio.sleep(60.0 / rate if rate > 0 else 0)
            await asyncio.to_thread(self._write_locked, camera, "TargetTemp", int(target))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cooling ramp on %s failed", self.descriptor.name)

    async def _watch_cooling(self) -> None:
        while True:
            await asyncio.sleep(self._config.telemetry_interval_s)
            try:
                await self._cooling_status()
            except Exception:
                logger.warning("could not read the cooler on %s", self.descriptor.name, exc_info=True)
                continue
            self._publish_state()

    async def _cooling_status(self) -> CoolingStatus:
        if not self._props.get("IsCoolerCam") or self._camera is None:
            return CoolingStatus(supported=False)
        values = await asyncio.to_thread(self._read_many, ("Temperature", "CoolPowerPerc", "AntiDewHeater"))
        self._cooling = CoolingStatus(
            supported=True,
            enabled=self._cooling_on,
            target_c=self._cooling_target,
            sensor_c=None if values["Temperature"] is None else values["Temperature"] / 10.0,
            power_percent=values["CoolPowerPerc"],
            dew_heater=None if values["AntiDewHeater"] is None else bool(values["AntiDewHeater"]),
        )
        return self._cooling

    # -------------------------------------------------------------- controls

    async def controls(self) -> list[ControlSpec]:
        self._require()
        names = [name for name, control in CONTROLS.items() if control.sdk_name in self._caps]
        values = await asyncio.to_thread(self._read_many, tuple(CONTROLS[n].sdk_name for n in names))
        specs = []
        for name in names:
            control = CONTROLS[name]
            caps = self._caps[control.sdk_name]
            value = values[control.sdk_name]
            if name == "target_temp" and self._cooling_target is not None:
                # The setpoint we are heading for, not the ramp's current step.
                value = self._cooling_target
            specs.append(
                ControlSpec(
                    name=name,
                    label=control.label,
                    value=None if value is None else value * control.scale,
                    writable=bool(caps.get("IsWritable")),
                    kind=control.kind,
                    minimum=None if control.kind is ControlKind.BOOLEAN else caps["MinValue"] * control.scale,
                    maximum=None if control.kind is ControlKind.BOOLEAN else caps["MaxValue"] * control.scale,
                    default=caps["DefaultValue"] * control.scale,
                    step=control.step,
                    unit=control.unit,
                    supports_auto=bool(caps.get("IsAutoSupported")),
                    description=control.description or caps.get("Description"),
                )
            )
        return specs

    async def set_control(self, name: str, value: float) -> ControlSpec:
        camera = self._require()
        control = CONTROLS.get(name)
        if control is None or control.sdk_name not in self._caps:
            raise CapabilityError(f"{self.descriptor.name} has no control {name!r}")
        caps = self._caps[control.sdk_name]
        if not caps.get("IsWritable"):
            raise CapabilityError(f"{name} is read-only")

        raw = round(value / control.scale)
        raw = max(int(caps["MinValue"]), min(int(caps["MaxValue"]), raw))

        if name == "cooler_on":
            await self.set_cooling(bool(raw), self._cooling_target)
        elif name == "target_temp":
            # A retarget, not a switch-on: the cooler stays as it is.
            if self._cooling_on:
                await self.set_cooling(True, raw)
            else:
                self._cooling_target = raw
                await asyncio.to_thread(self._write_locked, camera, "TargetTemp", raw)
        else:
            await asyncio.to_thread(self._write_locked, camera, control.sdk_name, raw)
            self._applied.pop(control.sdk_name, None)
            if name == "gain":
                self._gain = raw
            elif name == "offset":
                self._offset = raw

        self._publish_state()
        return next(spec for spec in await self.controls() if spec.name == name)

    # ------------------------------------------------------------ SDK access

    def _control_type(self, sdk_name: str) -> int:
        return int(self._caps[sdk_name]["ControlType"])

    def _write(self, camera: SdkCamera, sdk_name: str, value: int) -> None:
        """Write a control. Caller holds `_io`, or is the only user yet."""
        caps = self._caps.get(sdk_name)
        if caps is not None:
            value = max(int(caps["MinValue"]), min(int(caps["MaxValue"]), int(value)))
            camera.set_control_value(self._control_type(sdk_name), value)

    def _read(self, camera: SdkCamera, sdk_name: str) -> int | None:
        if sdk_name not in self._caps:
            return None
        return int(camera.get_control_value(self._control_type(sdk_name))[0])

    def _write_locked(self, camera: SdkCamera, sdk_name: str, value: int) -> None:
        with self._io:
            self._write(camera, sdk_name, value)

    def _read_locked(self, camera: SdkCamera, sdk_name: str) -> int | None:
        with self._io:
            return self._read(camera, sdk_name)

    def _read_many(self, sdk_names: tuple[str, ...]) -> dict[str, int | None]:
        camera = self._camera
        if camera is None:
            return dict.fromkeys(sdk_names)
        with self._io:
            return {name: self._read(camera, name) for name in sdk_names}

    def _call(self, method, *args):
        with self._io:
            return method(*args)

    def _publish_state(self) -> None:
        self._events.publish(
            Topic.CAMERA_STATE,
            role=str(self._config.role),
            state=str(self._state),
            kind=None if self._exposure_kind is None else str(self._exposure_kind),
            exposure_s=self._exposure_duration if self._exposure_started else None,
            exposure_started_at=self._exposure_started,
            gain=self._gain,
            offset=self._offset,
            binning=self._binning,
            # The last reading, not a fresh one: this is called from places
            # that must not wait on the USB bus. `_watch_cooling` keeps it
            # current.
            sensor_c=self._cooling.sensor_c,
            cooling_enabled=self._cooling_on,
            cooling_target_c=self._cooling_target,
            cooling_power=self._cooling.power_percent,
            dew_heater=self._cooling.dew_heater,
        )
