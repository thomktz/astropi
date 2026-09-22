"""Device inventory and connection control."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from astropi.api.deps import ObservatoryDep
from astropi.config import MountDriver
from astropi.core.errors import AstropiError
from astropi.devices import Device, DeviceRole

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
            "connection": str(
                observatory.registry.get(role, Device).connection_state
            ),
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
