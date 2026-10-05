"""Device inventory and connection control."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from astropi.api.deps import ObservatoryDep
from astropi.config import CameraDriver, MountDriver
from astropi.core.errors import AstropiError
from astropi.devices import Device, DeviceRole
from astropi.devices.backends.simulator.errors import sky_errors_out

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("")
async def list_devices(observatory: ObservatoryDep) -> dict:
    return {
        str(role): {
            "id": descriptor.id,
            "name": descriptor.name,
            "driver": descriptor.driver,
            "capabilities": sorted(str(c) for c in descriptor.capabilities),
            "details": descriptor.details,
            "connection": str(observatory.registry.get(role, Device).connection_state),
        }
        for role, descriptor in observatory.registry.descriptors().items()
    }


def _role(name: str) -> DeviceRole:
    try:
        return DeviceRole(name)
    except ValueError as error:
        valid = ", ".join(str(r) for r in DeviceRole)
        raise HTTPException(
            status_code=404, detail=f"unknown role {name!r}; expected one of {valid}"
        ) from error


@router.post("/{role}/connect")
async def connect(role: str, observatory: ObservatoryDep) -> dict:
    device = observatory.registry.get(_role(role), Device)
    await device.connect()
    return {"role": role, "connection": str(device.connection_state)}


@router.post("/{role}/disconnect")
async def disconnect(role: str, observatory: ObservatoryDep) -> dict:
    device = observatory.registry.get(_role(role), Device)
    await device.disconnect()
    return {"role": role, "connection": str(device.connection_state)}


def serial_ports() -> list[str]:
    """Serial ports that might have a mount on the end of them.

    The by-id links first, because "usb-STMicroelectronics_..." says what
    is plugged in while "ttyACM0" only says how many things were plugged
    in before it. Both are offered: the stable name survives a reboot,
    the short one is what people recognise.
    """
    found: list[str] = []
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        found.extend(sorted(str(entry) for entry in by_id.iterdir()))
    for pattern in ("ttyACM*", "ttyUSB*", "cu.usbserial*", "cu.usbmodem*"):
        found.extend(sorted(str(entry) for entry in Path("/dev").glob(pattern)))
    return found


class MountDriverIn(BaseModel):
    driver: MountDriver
    port: str | None = None


class SlewRateIn(BaseModel):
    """Goto speed, in multiples of sidereal.

    Capped at 1000 because these controllers top out around 800 - about
    3.3 degrees a second - and a motor asked for more than it has skips
    steps instead of turning, which leaves the mount's counts describing
    a position it never reached.
    """

    multiplier: float = Field(ge=1.0, le=1000.0)


def _mount_driver_out(observatory) -> dict:
    """What the setup panel shows: the choice, and what came of it."""
    mount = (
        observatory.registry.get(DeviceRole.MOUNT, Device)
        if observatory.registry.has(DeviceRole.MOUNT)
        else None
    )
    return {
        "driver": str(observatory.mount_driver),
        "port": observatory.mount_port,
        "available": [str(driver) for driver in MountDriver],
        "ports": serial_ports(),
        "connection": str(mount.connection_state) if mount else "disconnected",
        "name": mount.descriptor.name if mount else None,
        "details": mount.descriptor.details if mount else {},
        "slew_rate": observatory.mount_slew_rate,
        # 15.041 arcseconds a second is sidereal; the rest is arithmetic,
        # done here so the panel shows degrees per second rather than a
        # multiple nobody can picture.
        "slew_deg_per_s": round(observatory.mount_slew_rate * 15.0410686 / 3600.0, 3),
    }


@router.get("/mount/driver")
async def mount_driver(observatory: ObservatoryDep) -> dict:
    return _mount_driver_out(observatory)


@router.put("/mount/driver")
async def set_mount_driver(payload: MountDriverIn, observatory: ObservatoryDep) -> dict:
    """Swap between the real mount and the simulated one, live.

    The simulator is not a lesser mode to be escaped from: it is how this
    gets worked on indoors, and how a session can be rehearsed before a
    clear night is spent on it. Switching either way is one call, and the
    rig keeps running on the mount it already had if the new one does not
    answer.
    """
    try:
        await observatory.switch_mount(payload.driver, payload.port)
    except AstropiError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:  # a serial port that is not there, mostly
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _mount_driver_out(observatory)


class CameraDriverIn(BaseModel):
    driver: CameraDriver


def _camera_driver_out(observatory) -> dict:
    """The choice, and which sensors it came to."""
    sensors = {}
    for role in (DeviceRole.CAMERA, DeviceRole.GUIDE_CAMERA):
        if not observatory.registry.has(role):
            sensors[str(role)] = None
            continue
        device = observatory.registry.get(role, Device)
        sensors[str(role)] = {
            "name": device.descriptor.name,
            "connection": str(device.connection_state),
            "details": device.descriptor.details,
        }
    return {
        "driver": str(observatory.camera_driver),
        "available": [str(driver) for driver in CameraDriver],
        "camera": sensors[str(DeviceRole.CAMERA)],
        "guide_camera": sensors[str(DeviceRole.GUIDE_CAMERA)],
    }


@router.get("/camera/driver")
async def camera_driver(observatory: ObservatoryDep) -> dict:
    return _camera_driver_out(observatory)


@router.put("/camera/driver")
async def set_camera_driver(payload: CameraDriverIn, observatory: ObservatoryDep) -> dict:
    """Swap between the real camera and the simulated one, live.

    Both sensors move together: a real imaging sensor beside a simulated
    guide sensor would guide on a sky that is not the one being imaged.
    """
    try:
        await observatory.switch_camera(payload.driver)
    except Exception as error:  # no SDK, no camera on the bus, mostly
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _camera_driver_out(observatory)


@router.get("/mount/report")
async def mount_report(observatory: ObservatoryDep) -> dict:
    """Whatever the mount will tell us about itself.

    Only the real backend has anything to say here; the simulator answers
    with what it is pretending to be.
    """
    mount = observatory.registry.get(DeviceRole.MOUNT, Device)
    reporter = getattr(mount, "report", None)
    if reporter is None:
        return {"driver": str(observatory.mount_driver), "report": None}
    return {"driver": str(observatory.mount_driver), "report": await reporter()}


class SkyErrorsIn(BaseModel):
    enabled: bool | None = None
    polar_alt_error_arcmin: float | None = Field(default=None, ge=-600, le=600)
    polar_az_error_arcmin: float | None = Field(default=None, ge=-600, le=600)
    periodic_error_arcsec: float | None = Field(default=None, ge=0, le=300)
    periodic_error_period_s: float | None = Field(default=None, gt=10, le=3600)
    seeing_arcsec: float | None = Field(default=None, ge=0, le=10)


@router.get("/simulator/sky")
async def get_sky_errors(observatory: ObservatoryDep) -> dict:
    """What the simulated camera adds on top of a real mount."""
    return sky_errors_out(observatory.sky_errors.config)


@router.put("/simulator/sky")
async def set_sky_errors(payload: SkyErrorsIn, observatory: ObservatoryDep) -> dict:
    """Polar misalignment, worm error and seeing for the simulated sky.

    Only the camera's picture changes: the corrections a guider sends in
    answer go to the real mount, which is the point.
    """
    return sky_errors_out(observatory.set_sky_errors(**payload.model_dump(exclude_none=True)))


@router.put("/mount/slew-rate")
async def set_slew_rate(payload: SlewRateIn, observatory: ObservatoryDep) -> dict:
    """How fast a goto runs.

    Tuned against the rig rather than chosen once in code: the speed a
    mount can hold depends on its payload, its balance and how cold the
    grease is, and the symptom of asking for too much is a graunching
    noise and a pointing model quietly going wrong.
    """
    observatory.set_slew_rate(payload.multiplier)
    return _mount_driver_out(observatory)
