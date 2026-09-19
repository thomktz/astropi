"""Device inventory and connection control."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from astropi.api.deps import ObservatoryDep
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
