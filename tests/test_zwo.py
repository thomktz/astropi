"""The ZWO backend, against an SDK that answers like an ASI2600MC Duo.

Property and control records are shaped exactly as `zwoasi` returns them,
so the translation into our control set and sensor info is exercised on
the field names the real library uses.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from astropi.core.errors import CapabilityError, DeviceNotFoundError
from astropi.core.events import EventBus
from astropi.devices.backends.zwo.camera import EXP_SUCCESS, EXP_WORKING, ZwoCamera, ZwoCameraConfig
from astropi.devices.backends.zwo.sdk import FoundCamera, choose
from astropi.devices.base import DeviceRole
from astropi.devices.camera import ExposureRequest, FrameKind, Roi


def _cap(name, control_type, lo, hi, default, writable=True):
    return {
        "Name": name,
        "ControlType": control_type,
        "MinValue": lo,
        "MaxValue": hi,
        "DefaultValue": default,
        "IsWritable": writable,
        "IsAutoSupported": False,
        "Description": name,
    }


MAIN = {
    "Name": "ZWO ASI2600MC Duo",
    "MaxWidth": 6248,
    "MaxHeight": 4176,
    "IsColorCam": True,
    "BayerPattern": 0,
    "SupportedBins": [1, 2, 3, 4],
    "PixelSize": 3.76,
    "IsCoolerCam": True,
    "BitDepth": 16,
    "IsUSB3Camera": True,
    "IsUSB3Host": False,
}
GUIDE = {
    "Name": "ZWO ASI220MM Mini",
    "MaxWidth": 1920,
    "MaxHeight": 1080,
    "IsColorCam": False,
    "BayerPattern": 2,
    "SupportedBins": [1, 2],
    "PixelSize": 4.0,
    "IsCoolerCam": False,
    "BitDepth": 12,
}
# As the camera on the Pi reported them, 5 October 2026.
MAIN_CAPS = {
    "Gain": _cap("Gain", 0, -25, 700, 200),
    "Exposure": _cap("Exposure", 1, 32, 2_000_000_000, 10_000),
    "Offset": _cap("Offset", 5, 0, 240, 1),
    "BandWidth": _cap("BandWidth", 6, 40, 100, 50),
    "Temperature": _cap("Temperature", 8, -500, 1000, 20, writable=False),
    "CoolPowerPerc": _cap("CoolPowerPerc", 15, 0, 100, 0, writable=False),
    "TargetTemp": _cap("TargetTemp", 16, -40, 30, 0),
    "CoolerOn": _cap("CoolerOn", 17, 0, 1, 0),
    "AntiDewHeater": _cap("AntiDewHeater", 21, 0, 1, 0),
    "WB_R": _cap("WB_R", 3, 1, 99, 52),
}
GUIDE_CAPS = {
    "Gain": _cap("Gain", 0, 0, 600, 100),
    "Exposure": _cap("Exposure", 1, 32, 10_000_000, 10_000),
    "Offset": _cap("Offset", 5, 0, 1500, 100),
    "BandWidth": _cap("BandWidth", 6, 40, 100, 50),
    "Temperature": _cap("Temperature", 8, -500, 1000, 20, writable=False),
}


class FakeCamera:
    def __init__(self, props, caps):
        self.props, self.caps = props, caps
        self.values = {cap["ControlType"]: cap["DefaultValue"] for cap in caps.values()}
        self.values[8] = 150  # 15.0 C
        self.roi = (0, 0, props["MaxWidth"], props["MaxHeight"], 1)
        self.exposing_until: float | None = None
        self.started_dark: bool | None = None
        self.stopped = 0
        self.closed = False

    def get_camera_property(self):
        return dict(self.props)

    def get_controls(self):
        return dict(self.caps)

    def get_control_value(self, control_type):
        return [self.values[control_type], False]

    def set_control_value(self, control_type, value, auto=False):
        self.values[control_type] = value

    def set_roi(self, start_x=None, start_y=None, width=None, height=None, bins=None, image_type=None):
        # Unset arguments keep their current value, as in `zwoasi`.
        _, _, w0, h0, b0 = self.roi
        bins = bins or b0
        width, height = width or w0 // bins, height or h0 // bins
        start_x, start_y = start_x or 0, start_y or 0
        assert width % 8 == 0 and height % 2 == 0
        assert start_x + width <= self.props["MaxWidth"] // bins
        self.roi = (start_x, start_y, width, height, bins)

    def start_exposure(self, is_dark=False):
        self.started_dark = is_dark
        self.exposing_until = time.monotonic() + self.values[1] / 1e6

    def get_exposure_status(self):
        return EXP_SUCCESS if time.monotonic() >= self.exposing_until else EXP_WORKING

    def get_data_after_exposure(self, buffer_=None):
        _, _, width, height, _ = self.roi
        return bytearray(np.full((height, width), 1000, dtype="<u2").tobytes())

    def stop_exposure(self):
        self.stopped += 1

    def start_video_capture(self):
        self.video = True
        self.video_frames = 0

    def stop_video_capture(self):
        self.video = False

    def get_video_data(self, timeout=None, buffer_=None):
        assert self.video, "video read outside video mode"
        time.sleep(self.values[1] / 1e6)
        self.video_frames += 1
        _, _, width, height, _ = self.roi
        buffer_[:] = np.full((height, width), self.video_frames, dtype="<u2").tobytes()
        return buffer_

    def close(self):
        self.closed = True


class FakeSdk:
    def __init__(self):
        self.devices = [FakeCamera(GUIDE, GUIDE_CAPS), FakeCamera(MAIN, MAIN_CAPS)]

    def cameras(self):
        return [
            FoundCamera(i, d.props["Name"], d.props["MaxWidth"], d.props["MaxHeight"])
            for i, d in enumerate(self.devices)
        ]

    def open(self, index):
        return self.devices[index]


async def _duo(sdk=None):
    sdk = sdk or FakeSdk()
    claimed: set[int] = set()
    events = EventBus()
    main = ZwoCamera(events, ZwoCameraConfig(poll_interval_s=0.001), sdk=sdk, claimed=claimed)
    guide = ZwoCamera(
        events,
        ZwoCameraConfig(
            role=DeviceRole.GUIDE_CAMERA, poll_interval_s=0.001, default_gain=None, default_offset=None
        ),
        sdk=sdk,
        claimed=claimed,
    )
    await main.connect()
    await guide.connect()
    return sdk, main, guide


async def test_a_duo_splits_into_imaging_and_guide_sensors():
    sdk, main, guide = await _duo()
    assert main.descriptor.name == "ASI2600MC Duo"
    assert guide.descriptor.name == "ASI220MM Mini"
    assert main.sensor.bayer_pattern == "RGGB"
    assert guide.sensor.bayer_pattern is None
    # 12-bit sensor, but RAW16 frames come back scaled to 16 bits.
    assert guide.sensor.bit_depth == 16
    # Ours replace ZWO's video defaults on the 2600; the guide keeps its own.
    assert (sdk.devices[1].values[0], sdk.devices[1].values[5]) == (100, 30)
    assert (sdk.devices[0].values[0], sdk.devices[0].values[5]) == (100, 100)
    assert (await guide.status()).offset == 100
    await main.disconnect()
    await guide.disconnect()
    assert sdk.devices[0].closed and sdk.devices[1].closed


def test_choosing_by_name_reports_what_is_there():
    found = [FoundCamera(0, "ZWO ASI220MM Mini", 1920, 1080)]
    with pytest.raises(DeviceNotFoundError, match="ASI220MM Mini"):
        choose(found, "2600", largest=True)


async def test_a_dark_frame_is_raw_16_bit_and_keeps_the_shutter_shut():
    sdk, main, _ = await _duo()
    frame = await main.expose(ExposureRequest(duration_s=0.01, kind=FrameKind.DARK))
    assert frame.data.dtype == np.uint16
    assert frame.shape == (4176, 6248)
    assert sdk.devices[1].started_dark is True
    assert frame.metadata["sensor_temp_c"] == 15.0
    assert frame.metadata["pixel_scale_arcsec"] == pytest.approx(1.939, abs=1e-3)


async def test_a_guide_box_is_rounded_to_what_the_sdk_accepts():
    sdk, _, guide = await _duo()
    frame = await guide.expose(
        ExposureRequest(duration_s=0.01, roi=Roi(x=1900, y=10, width=101, height=51), binning=1)
    )
    # 101 wide is 96 to the SDK, pushed back inside the sensor - and read
    # 256 wide, since the guide sensor fails anything much narrower.
    assert frame.shape == (50, 96)
    assert sdk.devices[0].roi == (1920 - 256, 10, 256, 50, 1)


async def test_a_narrow_box_is_cut_from_a_wider_read():
    sdk, _, guide = await _duo()
    device = sdk.devices[0]
    marker = np.arange(256, dtype="<u2")
    device.get_data_after_exposure = lambda buffer_=None: bytearray(np.tile(marker, (40, 1)).tobytes())
    frame = await guide.expose(ExposureRequest(duration_s=0.01, roi=Roi(x=800, y=400, width=64, height=40)))
    assert device.roi == (800 - 96, 400, 256, 40, 1)
    # The pixels handed back are the ones asked for, not the read's edge.
    assert frame.shape == (40, 64)
    assert frame.data[0, 0] == 96 and frame.data[0, -1] == 96 + 63


async def test_cancelling_an_exposure_stops_the_sensor():
    sdk, main, _ = await _duo()
    task = asyncio.create_task(main.expose(ExposureRequest(duration_s=30)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk.devices[1].stopped == 1
    assert (await main.status()).state == "idle"


async def test_controls_translate_units_and_read_only_entries():
    _, main, guide = await _duo()
    specs = {spec.name: spec for spec in await main.controls()}
    assert specs["sensor_temp"].value == 15.0
    assert specs["cooler_power"].value == 0
    assert not specs["sensor_temp"].writable
    assert specs["dew_heater"].writable
    assert "WB_R" not in specs
    assert {spec.name for spec in await guide.controls()} == {
        "gain",
        "offset",
        "usb_bandwidth",
        "sensor_temp",
    }

    spec = await main.set_control("gain", 9999)
    assert spec.value == 700
    with pytest.raises(CapabilityError):
        await main.set_control("cooler_power", 50)
    with pytest.raises(CapabilityError):
        await guide.set_cooling(True, -10)


async def test_cooling_walks_the_setpoint_down_from_the_sensor():
    sdk, main, _ = await _duo()
    main._config.cooling_ramp_c_per_min = 6000  # a step per 10 ms
    await main.set_cooling(True, 10)
    device = sdk.devices[1]
    assert device.values[17] == 1
    # Starts at the sensor (15 C), not at the target.
    await asyncio.sleep(0.005)
    assert 10 < device.values[16] <= 15
    await main._ramp_task
    assert device.values[16] == 10
    assert (await main.status()).cooling.target_c == 10


async def test_the_rig_switches_cameras_live_and_back(tmp_path, monkeypatch):
    from astropi.config import CameraDriver, Settings
    from astropi.runtime import Observatory

    sdk = FakeSdk()
    observatory = await Observatory.build(Settings(data_dir=tmp_path, camera_width=800, camera_height=600))
    try:
        assert observatory.camera_driver is CameraDriver.SIMULATOR
        observatory.preview.update_config(exposure_s=3.5)

        def zwo_cameras():
            claimed: set[int] = set()
            return (
                ZwoCamera(observatory.events, ZwoCameraConfig(), sdk=sdk, claimed=claimed),
                ZwoCamera(
                    observatory.events,
                    ZwoCameraConfig(role=DeviceRole.GUIDE_CAMERA, default_gain=None, default_offset=None),
                    sdk=sdk,
                    claimed=claimed,
                ),
            )

        monkeypatch.setattr(observatory, "_zwo_cameras", zwo_cameras)
        camera = await observatory.switch_camera(CameraDriver.ZWO)
        assert isinstance(camera, ZwoCamera)
        assert observatory.guide_camera().descriptor.name == "ASI220MM Mini"
        # The loops follow the new sensors, and the live view keeps its settings.
        assert observatory.preview.config.exposure_s == 3.5
        assert observatory.state.get("camera") == {"driver": "zwo"}

        await observatory.switch_camera(CameraDriver.SIMULATOR)
        assert observatory.camera().descriptor.driver == "simulator"
        # The real sensors were let go on the way out.
        assert sdk.devices[0].closed and sdk.devices[1].closed
    finally:
        await observatory.shutdown()


async def test_streaming_holds_the_sensor_in_video_mode_until_closed():
    sdk, main, _ = await _duo()
    device = sdk.devices[1]
    frames = main.stream(ExposureRequest(duration_s=0.001, binning=2, kind=FrameKind.PREVIEW))
    seen = []
    async for frame in frames:
        seen.append(int(frame.data[0, 0]))
        if len(seen) == 3:
            break
    await frames.aclose()
    # Each frame is its own copy, not a view of a buffer the next one reuses.
    assert seen == [1, 2, 3]
    assert frame.shape == (2088, 3120)
    assert device.video is False
    # And the sensor is free for a single exposure again.
    shot = await main.expose(ExposureRequest(duration_s=0.001))
    assert shot.shape == (4176, 6248)
